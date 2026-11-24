"""QA + numbers: python -m skynet_clipper.qa [out_root] [stream_data_dir]

Per clip: dimensions, audio stream, loudness, black-bar check, start/mid/end frames + cover in one sheet
(out/qa/<id>.jpg). Then the run numbers straight from the engine's own files."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np

from .util import FFMPEG, FFPROBE


def probe(p: Path) -> dict:
    r = subprocess.run([FFPROBE, "-v", "error", "-show_entries", "stream=codec_type,width,height,duration:format=duration",
                        "-of", "json", str(p)], stdout=subprocess.PIPE)
    return json.loads(r.stdout)


def frame(p: Path, t: float, w: int = 405, h: int = 720) -> np.ndarray:
    r = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", str(p), "-frames:v", "1",
                        "-vf", f"scale={w}:{h}", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"], stdout=subprocess.PIPE)
    return np.frombuffer(r.stdout, np.uint8).reshape(h, w, 3)


def black_bars(img: np.ndarray) -> bool:
    luma = img.mean(axis=2)
    rows = luma.mean(axis=1)
    edge = max(4, img.shape[0] // 40)
    return bool(rows[:edge].max() < 10 or rows[-edge:].max() < 10)


def check_clip(d: Path, qa_dir: Path) -> dict:
    from PIL import Image
    clip = d / "clip.mp4"
    pr = probe(clip)
    dur = float(pr["format"]["duration"])
    v = [s for s in pr["streams"] if s["codec_type"] == "video"][0]
    aud = [s for s in pr["streams"] if s["codec_type"] == "audio"]
    has_audio = bool(aud) and abs(float(aud[0].get("duration", 0)) - dur) < 1.0  # full-length audio, not a stub
    frames = [frame(clip, t) for t in (1.0, dur / 2, max(0.5, dur - 1.5))]
    bars = any(black_bars(f) for f in frames)
    cover = np.asarray(Image.open(d / "cover.jpg").convert("RGB").resize((405, 720)))
    sheet = np.concatenate(frames + [cover], axis=1)
    qa_dir.mkdir(parents=True, exist_ok=True)
    Image.fromarray(sheet).save(qa_dir / f"{d.name}.jpg", quality=85)
    rj = json.loads((d / "render.json").read_text(encoding="utf-8")) if (d / "render.json").exists() else {}
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    return {"id": d.name, "w": v["width"], "h": v["height"], "duration": round(dur, 1), "audio": has_audio,
            "lufs": rj.get("lufs"), "black_bars": bars, "score": meta["score"], "title": meta["title"],
            "reason": meta["reasons"][0] if meta["reasons"] else "",
            "copyright": next((r for r in meta["reasons"] if r.startswith("copyright gate:")), "not run"),
            "moment_to_clip_s": rj.get("moment_to_clip_s"), "render_s": rj.get("render_seconds")}


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "data/out")
    sdir = Path(sys.argv[2]) if len(sys.argv) > 2 else None
    rows = [check_clip(d, out / "qa") for d in sorted((out / "clips").iterdir()) if (d / "clip.mp4").exists()]
    for r in rows:
        print(json.dumps(r, ensure_ascii=False))
    ev = [json.loads(l) for l in (out / "events.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    judged = [e for e in ev if e["type"] == "judged"]
    llm = sum(1 for e in judged if e["data"].get("llm"))
    summary = {"clips": len(rows), "candidates": sum(1 for e in ev if e["type"] == "candidate"),
               "judged": len(judged), "llm_judged": llm, "errors": sum(1 for e in ev if e["type"] == "error")}
    if sdir and (sdir / "stats.json").exists():
        st = json.loads((sdir / "stats.json").read_text(encoding="utf-8"))
        lat = st.get("moment_to_clip_latencies_s") or []
        summary.update(captured_hours=round(st["captured_seconds"] / 3600, 2), chat_messages=st["chat_messages"],
                       llm_calls=st["llm_calls"], llm_failures=st["llm_failures"],
                       avg_moment_to_clip_s=round(sum(lat) / len(lat), 1) if lat else None, live_clips_timed=len(lat))
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
