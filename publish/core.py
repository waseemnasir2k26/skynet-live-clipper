"""Queue, ledger, breaker, scheduler. Pure stdlib, injectable clock, file-backed."""
from __future__ import annotations
import json, os, threading
from datetime import datetime, timedelta
from . import paths

DESTS = ["youtube", "tiktok", "instagram", "facebook", "x"]
LABELS = {"youtube": "YouTube Shorts", "tiktok": "TikTok", "instagram": "Instagram Reels",
          "facebook": "Facebook Reels", "x": "X"}

DEFAULT_CONFIG = {
    "auto_publish": False,                    # master switch; OFF = DRY RUN
    "max_per_day": {d: 3 for d in DESTS},     # hard cap per destination per local day
    "total_cap_per_day": 10,                  # hard cap across all destinations per local day
    "min_spacing_min": 90,                    # min minutes between posts on one destination
    "window": {"start": "09:00", "end": "22:00"},   # wall-clock posting window (local)
    "breaker_failures": 2,                    # consecutive failures per destination -> breaker opens
    "clips_per_hour": 6,                      # engine cap (engine enforces; studio edits)
    "llm_calls_per_hour": 60,
}
_lock = threading.RLock()


def _read(p, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write(p, data):
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    for k, v in _read(paths.SETTINGS, {}).items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k].update(v)
        elif k in cfg:
            cfg[k] = v
    return cfg


def save_config(patch: dict) -> dict:
    with _lock:
        cur = _read(paths.SETTINGS, {})
        for k, v in patch.items():
            if k not in DEFAULT_CONFIG:
                continue
            if isinstance(v, dict) and isinstance(cur.get(k), dict):
                cur[k].update(v)
            else:
                cur[k] = v
        _write(paths.SETTINGS, cur)
        return load_config()


# ---------- queue ----------
def load_queue() -> list[dict]:
    return _read(paths.QUEUE, {"items": []}).get("items", [])


def save_queue(items):
    _write(paths.QUEUE, {"items": items, "updated": datetime.now().isoformat(timespec="seconds")})


def qid(clip_id, dest):
    return f"{clip_id}:{dest}"


def enqueue(clip_id: str, dests=None, caption: str = "") -> list[dict]:
    """Approved clip -> one draft row per destination. Idempotent per (clip, dest)."""
    with _lock:
        items = load_queue()
        have = {i["id"] for i in items}
        added = []
        for d in (dests or DESTS):
            if d not in DESTS or qid(clip_id, d) in have:
                continue
            row = {"id": qid(clip_id, d), "clip_id": clip_id, "dest": d, "status": "draft",
                   "scheduled_at": None, "attempts": 0, "error": None, "caption": caption,
                   "created": datetime.now().isoformat(timespec="seconds")}
            items.append(row)
            added.append(row)
        save_queue(items)
        return added


def set_status(item_id, **kw):
    with _lock:
        items = load_queue()
        for i in items:
            if i["id"] == item_id:
                i.update(kw)
        save_queue(items)


def remove_clip(clip_id):
    """Drop unposted rows for a clip (e.g. on Reject). Posted/dry-run rows stay as history."""
    with _lock:
        save_queue([i for i in load_queue()
                    if i["clip_id"] != clip_id or i["status"] in ("posted", "dry_run_ok")])


# ---------- ledger (idempotency) ----------
def ledger() -> list[dict]:
    out = []
    try:
        for ln in paths.LEDGER.read_text(encoding="utf-8").splitlines():
            if ln.strip():
                try:
                    out.append(json.loads(ln))
                except Exception:
                    pass
    except FileNotFoundError:
        pass
    return out


def ledger_has(clip_id, dest, mode):
    for e in ledger():
        if e["clip_id"] == clip_id and e["dest"] == dest and e["mode"] == mode and e["status"] == "ok":
            return e
    return None


def ledger_add(entry: dict):
    with _lock:
        paths.LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with open(paths.LEDGER, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ---------- breaker ----------
def breaker_state() -> dict:
    return _read(paths.BREAKER, {})


def breaker_open(dest, cfg=None) -> bool:
    cfg = cfg or load_config()
    return breaker_state().get(dest, {}).get("consecutive_failures", 0) >= cfg["breaker_failures"]


def breaker_record(dest, ok: bool, err: str = ""):
    with _lock:
        b = breaker_state()
        s = b.setdefault(dest, {"consecutive_failures": 0})
        if ok:
            s["consecutive_failures"] = 0
        else:
            s["consecutive_failures"] += 1
            s["last_error"] = err
            s["last_failure"] = datetime.now().isoformat(timespec="seconds")
        _write(paths.BREAKER, b)


def breaker_reset(dest=None):
    with _lock:
        b = breaker_state()
        for d in ([dest] if dest else list(b)):
            if d in b:
                b[d]["consecutive_failures"] = 0
        _write(paths.BREAKER, b)


def stopped() -> bool:
    return paths.STOP.exists()


# ---------- scheduling rules ----------
def _hm(s):
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def in_window(dt: datetime, cfg) -> bool:
    m = dt.hour * 60 + dt.minute
    return _hm(cfg["window"]["start"]) <= m <= _hm(cfg["window"]["end"])


def _posted_times(dest=None):
    """Successful posts (live or dry-run), so dry runs exercise exactly the same caps."""
    return [datetime.fromisoformat(e["ts"]) for e in ledger()
            if e["status"] == "ok" and (dest is None or e["dest"] == dest)]


def check_allowed(dest, now: datetime, cfg=None):
    """Hard gates evaluated at post time. Returns (ok, reason)."""
    cfg = cfg or load_config()
    if stopped():
        return False, "STOP file present"
    if breaker_open(dest, cfg):
        return False, f"breaker open ({cfg['breaker_failures']} consecutive failures)"
    if not in_window(now, cfg):
        return False, f"outside window {cfg['window']['start']}-{cfg['window']['end']}"
    allp = _posted_times()
    if len([t for t in allp if t.date() == now.date()]) >= cfg["total_cap_per_day"]:
        return False, f"total cap {cfg['total_cap_per_day']}/day reached"
    dp = _posted_times(dest)
    if len([t for t in dp if t.date() == now.date()]) >= cfg["max_per_day"].get(dest, 0):
        return False, f"{dest} cap {cfg['max_per_day'].get(dest, 0)}/day reached"
    if dp and now - max(dp) < timedelta(minutes=cfg["min_spacing_min"]):
        return False, f"spacing: {cfg['min_spacing_min']} min not elapsed"
    return True, "ok"


def next_slot(dest, after: datetime, cfg, taken):
    """Earliest time >= after that respects window, spacing, per-dest cap and total cap, given
    already-scheduled `taken` [(datetime, dest)] plus ledger posts."""
    sp = timedelta(minutes=cfg["min_spacing_min"])
    t = after.replace(second=0, microsecond=0)
    t += timedelta(minutes=(5 - t.minute % 5) % 5)   # snap to the 5-minute grid
    led_all, led_dest = _posted_times(), _posted_times(dest)
    for _ in range(14 * 24 * 12):  # scan 14 days in 5-minute steps
        if not in_window(t, cfg):
            t += timedelta(minutes=5)
            continue
        day = t.date()
        n_all = sum(1 for x in led_all if x.date() == day) + sum(1 for x, _ in taken if x.date() == day)
        n_dest = (sum(1 for x in led_dest if x.date() == day)
                  + sum(1 for x, d in taken if d == dest and x.date() == day))
        near = led_dest + [x for x, d in taken if d == dest]
        if (n_all < cfg["total_cap_per_day"] and n_dest < cfg["max_per_day"].get(dest, 0)
                and all(abs(t - x) >= sp for x in near)):
            return t
        t += timedelta(minutes=5)
    return None


def schedule_approved(now: datetime | None = None) -> list[dict]:
    """Give every unscheduled draft row the earliest legal slot."""
    now = now or datetime.now()
    cfg = load_config()
    with _lock:
        items = load_queue()
        taken = [(datetime.fromisoformat(i["scheduled_at"]), i["dest"]) for i in items
                 if i["status"] == "scheduled" and i.get("scheduled_at")]
        changed = []
        for i in items:
            if i["status"] in ("draft", "queued") and not i.get("scheduled_at"):
                slot = next_slot(i["dest"], now, cfg, taken)
                if slot:
                    i["scheduled_at"] = slot.isoformat(timespec="minutes")
                    i["status"] = "scheduled"
                    taken.append((slot, i["dest"]))
                    changed.append(i)
        save_queue(items)
        return changed


def live_allowed(cfg=None, live_flag=False) -> bool:
    cfg = cfg or load_config()
    return bool(live_flag and cfg["auto_publish"] and os.environ.get("AUTO_PUBLISH", "").lower() == "true")


def tick(now: datetime | None = None, live: bool = False, adapters=None) -> list[dict]:
    """Process due rows. Real posting needs cfg.auto_publish AND live=True AND env AUTO_PUBLISH=true;
    otherwise every adapter runs dry_run=True."""
    from .adapters import get_adapter
    now = now or datetime.now()
    cfg = load_config()
    dry = not live_allowed(cfg, live)
    out = []
    for i in sorted(load_queue(), key=lambda x: x.get("scheduled_at") or ""):
        if i["status"] != "scheduled" or not i.get("scheduled_at"):
            continue
        if datetime.fromisoformat(i["scheduled_at"]) > now:
            continue
        ok, why = check_allowed(i["dest"], now, cfg)
        if not ok:
            out.append({"id": i["id"], "result": "held", "reason": why})
            continue
        ad = (adapters or {}).get(i["dest"]) or get_adapter(i["dest"])
        out.append(publish_one(i, ad, dry_run=dry, now=now))
    return out


def publish_one(item: dict, adapter, dry_run: bool = True, now: datetime | None = None) -> dict:
    """Idempotent publish of one row: a clip never posts twice per destination."""
    now = now or datetime.now()
    mode = "dry_run" if dry_run else "live"
    if ledger_has(item["clip_id"], item["dest"], "live"):
        return {"id": item["id"], "result": "skipped", "reason": "already posted live (ledger)"}
    if ledger_has(item["clip_id"], item["dest"], mode):
        return {"id": item["id"], "result": "skipped", "reason": f"already recorded in ledger ({mode})"}
    if stopped():
        return {"id": item["id"], "result": "held", "reason": "STOP file present"}
    if breaker_open(item["dest"]):
        return {"id": item["id"], "result": "held", "reason": "breaker open"}
    try:
        res = adapter.publish(item, dry_run=dry_run)
        ok, err = res["ok"], res.get("error", "")
    except Exception as e:  # an adapter crash counts as a failure
        err = f"{type(e).__name__}: {e}"
        res, ok = {"ok": False, "error": err}, False
    ledger_add({"ts": now.isoformat(timespec="seconds"), "clip_id": item["clip_id"], "dest": item["dest"],
                "mode": mode, "status": "ok" if ok else "failed", "detail": res})
    att = item.get("attempts", 0) + 1
    if ok:
        set_status(item["id"], status="posted" if not dry_run else "dry_run_ok", error=None,
                   posted_at=now.isoformat(timespec="seconds"), attempts=att)
        breaker_record(item["dest"], True)
    else:
        set_status(item["id"], status="failed", error=err, attempts=att)
        breaker_record(item["dest"], False, err)
    return {"id": item["id"], "result": "ok" if ok else "failed", "mode": mode, "detail": res}
