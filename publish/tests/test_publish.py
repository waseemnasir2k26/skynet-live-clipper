import json, os, importlib, sys
from datetime import datetime, timedelta
from pathlib import Path
import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPPER_ROOT", str(tmp_path))
    monkeypatch.setenv("CLIPPER_OUT", str(tmp_path / "out"))
    monkeypatch.delenv("CLIPPER_FIXTURES", raising=False)
    monkeypatch.delenv("AUTO_PUBLISH", raising=False)
    from publish import paths
    importlib.reload(paths)
    from publish import core
    importlib.reload(core)
    return core, tmp_path


class OkAdapter:
    def __init__(self): self.calls = []
    def publish(self, item, dry_run=True):
        self.calls.append(dry_run)
        return {"ok": True, "would_post": {}}


class BadAdapter:
    def publish(self, item, dry_run=True):
        return {"ok": False, "error": "boom"}


def item(c, clip="c1", dest="youtube"):
    return {"id": c.qid(clip, dest), "clip_id": clip, "dest": dest, "attempts": 0}


T = datetime(2026, 11, 24, 12, 0)


def test_idempotent_never_posts_twice(env):
    c, _ = env
    a = OkAdapter()
    r1 = c.publish_one(item(c), a, dry_run=True, now=T)
    r2 = c.publish_one(item(c), a, dry_run=True, now=T + timedelta(hours=5))
    assert r1["result"] == "ok" and r2["result"] == "skipped" and len(a.calls) == 1
    # a live post blocks every later attempt, dry or live
    c.publish_one(item(c, "c2"), a, dry_run=False, now=T)
    assert c.publish_one(item(c, "c2"), a, dry_run=False, now=T)["result"] == "skipped"
    assert c.publish_one(item(c, "c2"), a, dry_run=True, now=T)["result"] == "skipped"
    assert len(a.calls) == 2


def test_per_dest_cap(env):
    c, _ = env
    c.save_config({"max_per_day": {"x": 2}, "min_spacing_min": 0})
    a = OkAdapter()
    for n in range(2):
        c.publish_one(item(c, f"k{n}", "x"), a, dry_run=True, now=T + timedelta(minutes=n))
    ok, why = c.check_allowed("x", T + timedelta(minutes=5))
    assert not ok and "cap" in why
    assert c.check_allowed("youtube", T + timedelta(minutes=5))[0]


def test_total_cap(env):
    c, _ = env
    c.save_config({"total_cap_per_day": 2, "min_spacing_min": 0})
    a = OkAdapter()
    c.publish_one(item(c, "a", "x"), a, True, T)
    c.publish_one(item(c, "b", "youtube"), a, True, T)
    ok, why = c.check_allowed("tiktok", T)
    assert not ok and "total cap" in why


def test_next_day_resets_caps(env):
    c, _ = env
    c.save_config({"total_cap_per_day": 1, "min_spacing_min": 0})
    c.publish_one(item(c, "a", "x"), OkAdapter(), True, T)
    assert not c.check_allowed("youtube", T)[0]
    assert c.check_allowed("youtube", T + timedelta(days=1))[0]


def test_spacing(env):
    c, _ = env
    c.save_config({"min_spacing_min": 90})
    c.publish_one(item(c, "a", "x"), OkAdapter(), True, T)
    ok, why = c.check_allowed("x", T + timedelta(minutes=30))
    assert not ok and "spacing" in why
    assert c.check_allowed("x", T + timedelta(minutes=91))[0]


def test_window(env):
    c, _ = env
    ok, why = c.check_allowed("x", datetime(2026, 11, 24, 3, 0))
    assert not ok and "window" in why


def test_scheduler_respects_rules(env):
    c, _ = env
    c.save_config({"min_spacing_min": 90, "max_per_day": {"youtube": 2}})
    for n in range(4):
        c.enqueue(f"clip{n}", ["youtube"])
    c.schedule_approved(now=T)
    slots = sorted(datetime.fromisoformat(i["scheduled_at"]) for i in c.load_queue())
    assert all(b - a >= timedelta(minutes=90) for a, b in zip(slots, slots[1:]))
    per_day = {}
    for s in slots:
        per_day[s.date()] = per_day.get(s.date(), 0) + 1
        assert 9 <= s.hour <= 22
    assert max(per_day.values()) <= 2


def test_breaker_two_failures(env):
    c, _ = env
    bad = BadAdapter()
    c.publish_one(item(c, "a", "tiktok"), bad, True, T)
    assert not c.breaker_open("tiktok")
    c.publish_one(item(c, "b", "tiktok"), bad, True, T + timedelta(hours=2))
    assert c.breaker_open("tiktok")
    ok, why = c.check_allowed("tiktok", T + timedelta(hours=9))
    assert not ok and "breaker" in why
    r = c.publish_one(item(c, "c", "tiktok"), OkAdapter(), True, T + timedelta(hours=9))
    assert r["result"] == "held"
    assert c.check_allowed("youtube", T)[0]          # other destinations unaffected
    c.breaker_reset("tiktok")
    assert not c.breaker_open("tiktok")


def test_success_resets_failure_count(env):
    c, _ = env
    c.publish_one(item(c, "a", "x"), BadAdapter(), True, T)
    c.publish_one(item(c, "b", "x"), OkAdapter(), True, T + timedelta(hours=2))
    assert c.breaker_state()["x"]["consecutive_failures"] == 0


def test_stop_file(env):
    c, root = env
    (root / "STOP").write_text("x")
    assert c.check_allowed("x", T) == (False, "STOP file present")
    a = OkAdapter()
    assert c.publish_one(item(c), a, True, T)["result"] == "held" and not a.calls


def test_live_is_triple_gated(env, monkeypatch):
    c, _ = env
    assert c.live_allowed(live_flag=True) is False             # setting off, env off
    c.save_config({"auto_publish": True})
    assert c.live_allowed(live_flag=True) is False             # env off
    monkeypatch.setenv("AUTO_PUBLISH", "true")
    assert c.live_allowed(live_flag=False) is False            # no --live
    assert c.live_allowed(live_flag=True) is True


def test_tick_is_dry_by_default(env):
    c, _ = env
    c.enqueue("clipT", ["youtube"])
    c.schedule_approved(now=T)
    a = OkAdapter()
    out = c.tick(now=T + timedelta(days=1), live=True, adapters={"youtube": a})   # --live but AUTO_PUBLISH off
    assert out and a.calls == [True]


def test_adapter_live_refuses_without_env(env):
    from publish.adapters import get_adapter
    ad = get_adapter("x")
    with pytest.raises(RuntimeError):
        ad._post_live({"clip_id": "z"}, {}, {})


def test_x_caption_keeps_credit(env):
    from publish.adapters import get_adapter
    cap = get_adapter("x").build_caption({"title": "word " * 100, "hashtags": ["a", "b", "c"]})
    assert len(cap) <= 280 and "@CarryisLive" in cap


def test_enqueue_idempotent(env):
    c, _ = env
    assert len(c.enqueue("q1")) == 5
    assert c.enqueue("q1") == []
