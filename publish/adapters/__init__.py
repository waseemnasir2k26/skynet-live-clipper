"""Five destination adapters behind one interface, wired to the existing social-publish lanes.
Lane scripts live in ~/.secrets/youtube (RUNBOOK: ~/.claude/skills/social-publish/RUNBOOK.md).
Nothing here posts unless AUTO_PUBLISH is on AND --live is given; the default and only tested mode is dry run."""
import sys
from pathlib import Path
from .base import Adapter, CREDIT, clip_dir
from .. import paths

LANES = paths.SECRETS / "youtube"
PY = sys.executable


class YouTubeShorts(Adapter):
    name, label = "youtube", "YouTube Shorts"
    max_duration, max_size_mb, max_caption = 180.0, 256, 5000   # description limit
    script = "yt_upload.py"
    credential = "youtube/token.json"       # OAuth token written by the YouTube Data API lane

    def job_spec(self, item, facts):
        from .base import load_meta
        m = load_meta(item["clip_id"])
        title = (m.get("title") or "Live moment")[:88]
        return {"file": str(clip_dir(item["clip_id"]) / "clip.mp4"), "title": f"{title} #Shorts",
                "description": facts["caption"], "privacy": "public", "publishAt": None,
                "thumbnail": str(clip_dir(item["clip_id"]) / "cover.jpg"), "category": 24,
                "made_for_kids": False}

    def live_command(self, key):
        return [PY, str(LANES / self.script), "jobs.json", key]


class TikTok(Adapter):
    name, label = "tiktok", "TikTok"
    max_duration, max_size_mb, max_caption = 180.0, 287, 2200
    script = "social/tt_schedule.py"        # cookie lane (TikTok Studio); never click "Post now" early
    credential = "cookies-tiktok.json"

    def job_spec(self, item, facts):
        return {"video": str(clip_dir(item["clip_id"]) / "clip.mp4"), "caption": facts["caption"], "now": True}

    def write_job(self, jobs_dir, key, spec):
        import json
        (jobs_dir / "tt_jobs.json").write_text(json.dumps({key: spec}, indent=1), encoding="utf-8")

    def live_command(self, key):
        return [PY, str(LANES / self.script), key, "--go"]


class InstagramReels(Adapter):
    name, label = "instagram", "Instagram Reels"
    max_duration, max_size_mb, max_caption = 90.0, 250, 2200
    script = "ig_reel.py"                   # Meta Graph content publishing
    credential = "meta-ig.token"

    def job_spec(self, item, facts):
        return {"file": str(clip_dir(item["clip_id"]) / "clip.mp4"), "caption": facts["caption"],
                "comment": f"Full stream: credit {CREDIT}"}

    def write_job(self, jobs_dir, key, spec):
        import json
        (jobs_dir / "ig_jobs.json").write_text(json.dumps({key: spec}, indent=1), encoding="utf-8")

    def live_command(self, key):
        return [PY, str(LANES / self.script), "publish", key]


class FacebookReels(Adapter):
    name, label = "facebook", "Facebook Reels"
    max_duration, max_size_mb, max_caption = 90.0, 250, 2200
    script = "fb_reel.py"                   # Meta Graph video_reels (Skynetlabs Page)
    credential = "meta-page-skynetlabs.token"

    def job_spec(self, item, facts):
        return {"file": str(clip_dir(item["clip_id"]) / "clip.mp4"), "caption": facts["caption"], "now": True,
                "token": "~/.secrets/meta-page-skynetlabs.token"}

    def live_command(self, key):
        return [PY, str(LANES / self.script), "jobs.json", key]


class XPost(Adapter):
    name, label = "x", "X"
    max_duration, max_size_mb, max_caption = 140.0, 512, 280
    script = "social/x_post.py"             # cookie lane (X native composer); API lane is pay-per-use
    credential = "cookies-x.json"

    def build_caption(self, meta, override=""):
        # X: 280 chars, credit must survive truncation. Hashtags limited to 2.
        base = (override or meta.get("title") or meta.get("caption") or "").strip()
        tags = " ".join(h if h.startswith("#") else "#" + h for h in meta.get("hashtags", [])[:2])
        tail = f"\n{tags}\n{CREDIT}".rstrip() if tags else f"\n{CREDIT}"
        room = 280 - len(tail)
        if len(base) > room:
            base = base[: room - 1].rstrip() + "…"
        return base + tail

    def caption_ok(self, cap):
        return len(cap) <= 280

    def job_spec(self, item, facts):
        return {"text": facts["caption"], "media": [str(clip_dir(item["clip_id"]) / "clip.mp4")], "when": None}

    def write_job(self, jobs_dir, key, spec):
        import json
        (jobs_dir / "x_posts.json").write_text(json.dumps({key: spec}, indent=1), encoding="utf-8")

    def live_command(self, key):
        return [PY, str(LANES / self.script), key, "--go"]


ADAPTERS = {c.name: c for c in (YouTubeShorts, TikTok, InstagramReels, FacebookReels, XPost)}


def get_adapter(dest: str) -> Adapter:
    return ADAPTERS[dest]()
