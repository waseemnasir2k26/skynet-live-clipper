"""Adapter interface + file validation. Real posting is impossible unless ALL of:
cfg.auto_publish true, env AUTO_PUBLISH=true, --live flag (core.live_allowed) and adapter.publish(dry_run=False)."""
from __future__ import annotations
import json, os, shutil, subprocess
from pathlib import Path
from .. import paths

CREDIT = os.environ.get("CLIPPER_CREDIT", "@StreamerHandle")   # the streamer you credit in every caption


def _ffprobe():
    p = shutil.which("ffprobe")
    if p:
        return p
    return None


def probe(path: Path) -> dict:
    fp = _ffprobe()
    if not fp:
        return {}
    try:
        r = subprocess.run([fp, "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=width,height:format=duration,size", "-of", "json", str(path)],
                           capture_output=True, text=True, timeout=30)
        j = json.loads(r.stdout)
        s = j["streams"][0]
        return {"width": s["width"], "height": s["height"],
                "duration": float(j["format"]["duration"]), "size": int(j["format"]["size"])}
    except Exception:
        return {}


def clip_dir(clip_id: str) -> Path:
    return paths.CLIPS / clip_id


def load_meta(clip_id: str) -> dict:
    return json.loads((clip_dir(clip_id) / "meta.json").read_text(encoding="utf-8"))


class Adapter:
    name = "base"
    label = "Base"
    max_duration = 60.0
    max_size_mb = 250
    max_caption = 2200
    script = None          # lane script (under <CLIPPER_SECRETS>/lanes) this adapter is wired to
    credential = None      # file (under CLIPPER_SECRETS) whose existence = "connected"; contents never read

    # -- config / status --
    def connected(self) -> bool:
        if not self.credential:
            return False
        return (paths.SECRETS / self.credential).exists()

    def lane_script_present(self) -> bool:
        return bool(self.script) and (paths.SECRETS / "lanes" / self.script).exists()

    # -- caption --
    def build_caption(self, meta: dict, override: str = "") -> str:
        base = (override or meta.get("caption") or meta.get("title") or "").strip()
        tags = " ".join(h if h.startswith("#") else "#" + h for h in meta.get("hashtags", []))
        cap = f"{base}\n\n{tags}".strip()
        if CREDIT.lower() not in cap.lower():
            cap += f"\n\nClip from {CREDIT}"
        return cap[: self.max_caption] if len(cap) > self.max_caption else cap

    # -- validation --
    def validate(self, item: dict) -> tuple[list[str], dict]:
        """Returns (problems, facts). Empty problems = would pass."""
        problems, facts = [], {}
        cd = clip_dir(item["clip_id"])
        mp4 = cd / "clip.mp4"
        try:
            meta = load_meta(item["clip_id"])
        except Exception as e:
            return [f"meta.json unreadable: {e}"], {}
        if not mp4.exists():
            problems.append("clip.mp4 missing")
        else:
            pr = probe(mp4)
            facts.update(pr)
            if not pr:
                problems.append("ffprobe unavailable or failed; cannot verify 9:16")
            else:
                if (pr["width"], pr["height"]) != (1080, 1920):
                    problems.append(f"not 1080x1920 ({pr['width']}x{pr['height']})")
                if pr["duration"] > self.max_duration:
                    problems.append(f"duration {pr['duration']:.0f}s > {self.max_duration:.0f}s limit")
                if pr["size"] > self.max_size_mb * 1024 * 1024:
                    problems.append(f"size {pr['size']/1e6:.0f} MB > {self.max_size_mb} MB limit")
        cap = self.build_caption(meta, item.get("caption", ""))
        facts["caption"] = cap
        facts["caption_len"] = len(cap)
        if CREDIT.lower() not in cap.lower():
            problems.append(f"credit {CREDIT} missing")
        if not self.caption_ok(cap):
            problems.append(f"caption too long for {self.label}")
        return problems, facts

    def caption_ok(self, cap: str) -> bool:
        return len(cap) <= self.max_caption

    # -- publish --
    def job_spec(self, item: dict, facts: dict) -> dict:
        """What would be handed to the real lane script. Overridden per lane."""
        return {"file": str(clip_dir(item["clip_id"]) / "clip.mp4"), "caption": facts.get("caption", "")}

    def live_command(self, key: str) -> list[str] | None:
        return None

    def publish(self, item: dict, dry_run: bool = True) -> dict:
        problems, facts = self.validate(item)
        if problems:
            return {"ok": False, "dest": self.name, "dry_run": dry_run, "error": "; ".join(problems),
                    "facts": facts}
        spec = self.job_spec(item, facts)
        result = {"ok": True, "dest": self.name, "dry_run": dry_run, "would_post": spec,
                  "lane_script": self.script, "lane_script_present": self.lane_script_present(),
                  "connected": self.connected(), "facts": {k: v for k, v in facts.items() if k != "caption"}}
        if dry_run:
            return result
        return self._post_live(item, spec, result)

    def _post_live(self, item, spec, result):
        """Real post. Guarded twice more here so no caller can reach it by accident."""
        if os.environ.get("AUTO_PUBLISH", "").lower() != "true":
            raise RuntimeError("live post refused: env AUTO_PUBLISH is not 'true'")
        if not self.connected():
            raise RuntimeError(f"{self.name} not connected (credential missing)")
        key = f"CLIP-{item['clip_id']}"
        jobs_dir = paths.OUT / "lane_jobs" / self.name
        jobs_dir.mkdir(parents=True, exist_ok=True)
        self.write_job(jobs_dir, key, spec)
        cmd = self.live_command(key)
        if not cmd:
            raise RuntimeError(f"{self.name}: no live command wired")
        r = subprocess.run(cmd, cwd=jobs_dir, capture_output=True, text=True, timeout=1800,
                           env={**os.environ, "PYTHONUTF8": "1"})
        if r.returncode != 0:
            raise RuntimeError(f"lane script exit {r.returncode}: {(r.stderr or r.stdout)[-300:]}")
        result["lane_output"] = r.stdout[-500:]
        return result

    def write_job(self, jobs_dir: Path, key: str, spec: dict):
        (jobs_dir / "jobs.json").write_text(json.dumps({key: spec}, indent=1), encoding="utf-8")
