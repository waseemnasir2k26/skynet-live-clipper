"""Skynet Live Clipper Studio server. stdlib only. 127.0.0.1:8790.
Default reads the real engine output at C:\\clipper-data\\out. CLIPPER_FIXTURES=1 reads studio/fixtures/data
and shows a DEMO DATA watermark. Never posts anything: publishing goes through publish/ and is dry-run only here."""
from __future__ import annotations
import json, mimetypes, os, random, re, sys, threading, time, math
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from publish import paths, core                      # noqa: E402
from publish.adapters import ADAPTERS, get_adapter    # noqa: E402

STATIC = Path(__file__).resolve().parent / "static"
HOST, PORT = "127.0.0.1", int(os.environ.get("STUDIO_PORT", "8790"))
ENGINE_FRESH_S = 20


def jread(p, default):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return default


def jtail(p, n):
    """Last n JSON lines of an append-only file; tolerant of partial/corrupt lines."""
    try:
        with open(p, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 400_000))
            lines = f.read().decode("utf-8", "ignore").splitlines()
    except FileNotFoundError:
        return []
    out = []
    for ln in lines[-n:]:
        try:
            out.append(json.loads(ln))
        except Exception:
            pass
    return out


def parse_ts(s):
    """Engine timestamps carry a UTC offset (+08:00); compare as local naive times."""
    try:
        d = datetime.fromisoformat(s)
        return d.astimezone().replace(tzinfo=None) if d.tzinfo else d
    except Exception:
        return None


def decisions():
    return jread(paths.DECISIONS, {})


def clips():
    out = []
    if paths.CLIPS.exists():
        for d in sorted(paths.CLIPS.iterdir()):
            m = jread(d / "meta.json", None)
            if not m or not (d / "clip.mp4").exists():
                continue
            out.append(m)
    dec = decisions()
    for m in out:
        dd = dec.get(m["id"], {})
        m["decision"] = dd.get("status", "pending")
        for k in ("title", "caption"):
            if dd.get(k):
                m[k] = dd[k]
        m["has_cover"] = (paths.CLIPS / m["id"] / "cover.jpg").exists()
    out.sort(key=lambda m: m.get("created_at", ""), reverse=True)
    return out


_TOT = {"key": None, "val": None}
_HB = re.compile(r"t=(\d+(?:\.\d+)?)s chat=(\d+) cands=(\d+) clips=(\d+) llm=(\d+) asr=(\d+(?:\.\d+)?)s")


def stream_totals():
    """Per-stream totals from events.jsonl. state.json counts per engine run (per replay leg); a stream
    replayed in several legs needs the sum of each run's last heartbeat. Hours = sum of (last t - first asr)."""
    p = Path(paths.EVENTS)
    try:
        key = (p.stat().st_mtime, p.stat().st_size)
    except OSError:
        return None
    if _TOT["key"] == key:
        return _TOT["val"]
    runs, cur, rejected, cands, judged = [], None, 0, 0, 0
    try:
        lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return None
    for ln in lines:
        try:
            e = json.loads(ln)
        except Exception:
            continue
        t, msg = e.get("type"), e.get("msg", "")
        if t == "info" and msg.startswith("engine start"):
            cur = {"from": None, "t": 0.0, "chat": 0, "llm": 0}
            runs.append(cur)
        elif t == "heartbeat":
            m = _HB.search(msg)
            if not m:
                continue
            if cur is None:
                cur = {"from": None, "t": 0.0, "chat": 0, "llm": 0}
                runs.append(cur)
            tt, chat, _c, _k, llm, asr = m.groups()
            if cur["from"] is None:
                cur["from"] = float(asr)
            cur.update(t=float(tt), chat=int(chat), llm=int(llm))
        elif t == "candidate":
            cands += 1
        elif t == "judged":
            judged += 1
        elif t == "info" and msg.startswith("rejected as unsafe"):
            rejected += 1
    secs = sum(max(0.0, r["t"] - (r["from"] or 0.0)) for r in runs if r["t"] > 0)
    val = {"runs": len(runs), "watched_seconds": round(secs, 1), "chat_messages": sum(r["chat"] for r in runs),
           "candidates": cands, "judged": judged, "rejected_unsafe": rejected}
    _TOT.update(key=key, val=val)
    return val


def api_state():
    st = jread(paths.STATE, None)
    now = datetime.now()
    hb = parse_ts((st or {}).get("last_heartbeat", ""))
    age = (now - hb).total_seconds() if hb else None
    online = age is not None and age < ENGINE_FRESH_S
    cl = clips()
    return {"state": st, "engine_online": online, "heartbeat_age_s": age, "fixtures": paths.FIXTURES,
            "stopped": core.stopped(), "now": now.isoformat(timespec="seconds"),
            "clips_total": len(cl), "totals": stream_totals(), "clips_pending": sum(1 for c in cl if c["decision"] == "pending"),
            "data_dir": str(paths.OUT)}


def api_queue():
    cmap = {c["id"]: c for c in clips()}
    rows = []
    for i in core.load_queue():
        c = cmap.get(i["clip_id"], {})
        rows.append({**i, "label": core.LABELS.get(i["dest"], i["dest"]), "title": c.get("title", i["clip_id"]),
                     "duration": c.get("duration")})
    rows.sort(key=lambda r: (r.get("scheduled_at") or "9", r["id"]))
    return {"items": rows, "config": core.load_config(), "breaker": core.breaker_state(),
            "stopped": core.stopped(), "ledger": core.ledger()[-12:][::-1],
            "dests": [{"key": d, "label": core.LABELS[d]} for d in core.DESTS]}


def api_connections():
    cons = []
    for d, cls in ADAPTERS.items():
        a = cls()
        cons.append({"key": d, "label": a.label, "connected": a.connected(), "lane_script": a.script,
                     "lane_script_present": a.lane_script_present(),
                     "limits": f"{int(a.max_duration)} s max, caption {a.max_caption} chars",
                     "kind": "official API" if d in ("youtube", "instagram", "facebook") else "cookie session"})
    cmds, ev = [], jtail(paths.EVENTS, 400)
    st = jread(paths.STATE, {}) or {}
    for c in jtail(paths.COMMANDS, 20)[::-1]:
        url = c.get("url", "")
        ts = parse_ts(c.get("ts", ""))
        ack = (st.get("stream_url") == url) or any(
            url and url in json.dumps(e) and (parse_ts(e.get("ts", "")) or datetime.min) >= (ts or datetime.min)
            for e in ev if e.get("type") in ("info", "heartbeat", "error"))
        cmds.append({**c, "picked_up": bool(ack)})
    return {"destinations": cons, "commands": cmds}


class H(BaseHTTPRequestHandler):
    server_version = "ClipperStudio/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):
        pass

    # ---- helpers ----
    def _send(self, code, body: bytes, ctype="application/json", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _host_ok(self):
        h = (self.headers.get("Host") or "").split(":")[0]
        if h not in ("127.0.0.1", "localhost"):
            return False
        o = self.headers.get("Origin")
        return not o or urlparse(o).hostname in ("127.0.0.1", "localhost")

    def _file(self, path: Path):
        if not path.is_file():
            return self._send(404, b'{"error":"not found"}')
        size = path.stat().st_size
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        rng = self.headers.get("Range")
        start, end, code = 0, size - 1, 200
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m:
                if m.group(1):
                    start = int(m.group(1))
                    if m.group(2):
                        end = min(int(m.group(2)), size - 1)
                elif m.group(2):
                    start = max(0, size - int(m.group(2)))
                if start > end or start >= size:
                    return self._send(416, b"", extra={"Content-Range": f"bytes */{size}"})
                code = 206
        length = end - start + 1
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-cache")
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        try:
            with open(path, "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(65536, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    # ---- routing ----
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, b"forbidden")
        u = urlparse(self.path)
        p, q = unquote(u.path), parse_qs(u.query)
        try:
            if p in ("/", "/index.html"):
                return self._file(STATIC / "index.html")
            if p == "/favicon.ico":
                return self._send(200, b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" rx="6" fill="#14130f"/><rect x="11" y="5" width="9" height="22" rx="2" fill="#c66b3f"/></svg>', "image/svg+xml")
            if p.startswith("/static/"):
                f = (STATIC / p[8:]).resolve()
                if STATIC.resolve() not in f.parents:
                    return self._send(403, b"forbidden")
                return self._file(f)
            m = re.fullmatch(r"/clips/([\w.-]+)/(clip\.mp4|cover\.jpg)", p)
            if m:
                return self._file(paths.CLIPS / m.group(1) / m.group(2))
            if p == "/api/state":
                return self._json(api_state())
            if p == "/api/signals":
                return self._json(jtail(paths.SIGNALS, int(q.get("n", ["240"])[0])))
            if p == "/api/events":
                return self._json(jtail(paths.EVENTS, int(q.get("n", ["40"])[0]))[::-1])
            if p == "/api/clips":
                return self._json(clips())
            if p == "/api/queue":
                return self._json(api_queue())
            if p == "/api/connections":
                return self._json(api_connections())
            if p == "/api/settings":
                return self._json({"config": core.load_config(), "stopped": core.stopped(),
                                   "stop_file": str(paths.STOP)})
            return self._send(404, b'{"error":"not found"}')
        except Exception as e:
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def do_POST(self):
        if not self._host_ok():
            return self._send(403, b"forbidden")
        p, b = unquote(urlparse(self.path).path), self._body()
        try:
            m = re.fullmatch(r"/api/clips/([\w.-]+)/(approve|reject|edit)", p)
            if m:
                return self._json(self.decide(m.group(1), m.group(2), b))
            if p == "/api/queue/schedule":
                return self._json({"scheduled": core.schedule_approved()})
            if p == "/api/queue/dryrun":
                it = next((i for i in core.load_queue() if i["id"] == b.get("id")), None)
                if not it:
                    return self._json({"error": "unknown row"}, 404)
                return self._json(core.publish_one(it, get_adapter(it["dest"]), dry_run=True))
            if p == "/api/queue/retry":
                core.breaker_reset(b.get("dest"))
                core.set_status(b.get("id"), status="scheduled", error=None)
                return self._json({"ok": True})
            if p == "/api/watch":
                url = str(b.get("url", "")).strip()
                if not re.match(r"^https?://\S+$", url):
                    return self._json({"error": "enter a full http(s) stream URL"}, 400)
                paths.COMMANDS.parent.mkdir(parents=True, exist_ok=True)
                with open(paths.COMMANDS, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"ts": datetime.now().isoformat(timespec="seconds"), "cmd": "watch", "url": url}) + "\n")
                return self._json({"ok": True})
            if p == "/api/settings":
                return self._json({"config": core.save_config(b)})
            if p == "/api/breaker":
                if b.get("on"):
                    paths.STOP.parent.mkdir(parents=True, exist_ok=True)
                    paths.STOP.write_text(f"stopped from studio {datetime.now().isoformat(timespec='seconds')}\n")
                else:
                    paths.STOP.unlink(missing_ok=True)
                return self._json({"stopped": core.stopped()})
            return self._send(404, b'{"error":"not found"}')
        except Exception as e:
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def decide(self, cid, action, b):
        dec = decisions()
        cur = dec.get(cid, {})
        if action == "edit":
            for k in ("title", "caption"):
                if isinstance(b.get(k), str):
                    cur[k] = b[k].strip()[:2000]
        else:
            cur["status"] = "approved" if action == "approve" else "rejected"
            cur["at"] = datetime.now().isoformat(timespec="seconds")
        dec[cid] = cur
        paths.DECISIONS.parent.mkdir(parents=True, exist_ok=True)
        paths.DECISIONS.write_text(json.dumps(dec, indent=1), encoding="utf-8")
        if action == "approve":
            core.enqueue(cid, caption=cur.get("caption", ""))
            core.schedule_approved()
        elif action == "reject":
            core.remove_clip(cid)
        return {"ok": True, "decision": cur}


# ---------- fixture-mode simulator: makes the DEMO graph move. Never runs on real data. ----------
def simulate():
    r = random.Random()
    spike_left = 0
    while True:
        time.sleep(2)
        st = jread(paths.STATE, None)
        if not st:
            continue
        now = datetime.now()
        started = parse_ts(st["started_at"]) or now
        if spike_left <= 0 and r.random() < 0.03:
            spike_left = 6
        s = 1.0 if spike_left > 0 else 0.0
        spike_left -= 1
        chat = 15 + 5 * r.random() + 62 * s * (0.7 + 0.3 * r.random())
        audio = -34 + 3 * r.random() + 20 * s
        sig = {"ts": now.isoformat(timespec="seconds"), "t": round((now - started).total_seconds(), 1),
               "chat_rate": round(chat, 1), "chat_z": round((chat - 17) / 6, 2),
               "audio_db": round(audio, 1), "audio_z": round((audio + 33) / 3.2, 2)}
        with open(paths.SIGNALS, "a", encoding="utf-8") as f:
            f.write(json.dumps(sig) + "\n")
        if s and spike_left == 5:
            ev = {"ts": sig["ts"], "type": "candidate", "msg": f"Chat spike {max(2.4, sig['chat_z']):.1f}x baseline", "data": {}}
            with open(paths.EVENTS, "a", encoding="utf-8") as f:
                f.write(json.dumps(ev) + "\n")
            st["candidates_seen"] += 1
        st["chat_messages"] += int(chat * 2 / 5 * 5)
        st["captured_seconds"] = int((now - started).total_seconds())
        st["last_heartbeat"] = now.isoformat(timespec="seconds")
        st["current_signals"] = {k: sig[k] for k in ("chat_rate", "chat_z", "audio_db", "audio_z")}
        tmp = paths.STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(st), encoding="utf-8")
        os.replace(tmp, paths.STATE)


def background_tick():
    """Only while the master toggle is ON. Server can never go live: tick(live=False) => dry run."""
    while True:
        time.sleep(30)
        try:
            if core.load_config()["auto_publish"]:
                core.tick(live=False)
        except Exception:
            pass


def main():
    if paths.FIXTURES and not paths.STATE.exists():
        import subprocess
        subprocess.run([sys.executable, str(Path(__file__).parent / "fixtures" / "make_fixtures.py")], check=True)
    if paths.FIXTURES:
        threading.Thread(target=simulate, daemon=True).start()
    threading.Thread(target=background_tick, daemon=True).start()
    srv = ThreadingHTTPServer((HOST, PORT), H)
    srv.daemon_threads = True
    print(f"studio on http://{HOST}:{PORT} data={paths.OUT} fixtures={paths.FIXTURES} pid={os.getpid()}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
