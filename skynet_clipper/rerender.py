"""Re-render finished clips with the current design code (same moment, same meta and cover; new clip.mp4).

  py -3.11 -m skynet_clipper.rerender STREAM_DATA_DIR OUT_ROOT [--offsets 3600,9700,10420] [ids...]

Segment lists are matched to their run offsets in creation order (replay legs start at their from_s).
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import yaml

from .asr import Word
from .contract import atomic_write_json
from .layout import Box, Layout
from .render import measure_loudness, render_clip
from .util import FFMPEG, run_ff

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    args = sys.argv[1:]
    data, out = Path(args.pop(0)), Path(args.pop(0))
    offsets = [0.0]
    if "--offsets" in args:
        i = args.index("--offsets")
        offsets = [float(x) for x in args[i + 1].split(",")]
        del args[i:i + 2]
    cfg = yaml.safe_load(open(ROOT / "config.yaml", encoding="utf-8"))
    lists = sorted((data / "seg").glob("list_*.csv"), key=lambda p: int(p.stem.split("_")[2]))
    segs = []
    for n, lp in enumerate(lists):
        off = offsets[min(n, len(offsets) - 1)]
        for line in lp.read_text().splitlines():
            name, a, b = line.split(",")[:3]
            segs.append((str(data / "seg" / name), float(a) + off, float(b) + off))
    words = []
    for line in (data / "transcript.jsonl").read_text(encoding="utf-8").splitlines():
        d = json.loads(line)
        words.append(Word(d["t0"], d["t1"], d["w"], d["r"]))
    lj = json.loads((data / "layout.json").read_text())
    lay = Layout(lj["kind"], *(Box(*lj[k]) if lj.get(k) else None for k in ("cam", "game", "blur")),
                 lj["cam_h"], lj["game_h"], Box(*lj["face"]) if lj.get("face") else None)
    for d in sorted((out / "clips").iterdir()):
        if args and d.name not in args:
            continue
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        t0, t1 = meta["t_start"], meta["t_end"]
        cover_seg = [s for s in segs if s[2] > t0 - 1 and s[1] < t1 + 1]
        ws = [w for w in words if w.t1 > t0 and w.t0 < t1]
        work = data / "rework" / d.name
        r = render_clip(work, cover_seg, t0, t1, lay, ws, meta["title"], cfg["source"]["credit"],
                        cfg["clips"]["watermark"], cfg, cfg["render"]["encoder"])
        lufs = measure_loudness(r["path"])
        if lufs is not None and abs(lufs - cfg["render"]["loudness_lufs"]) > 1.0:
            tmp = r["path"].with_name("g.mp4")
            run_ff([FFMPEG, "-hide_banner", "-y", "-i", str(r["path"]), "-c:v", "copy", "-af",
                    f"volume={cfg['render']['loudness_lufs'] - lufs:.2f}dB,alimiter=limit=0.85:level=false",
                    "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(tmp)])
            shutil.move(tmp, r["path"])
            lufs = measure_loudness(r["path"])
        shutil.copy(r["path"], d / "clip.mp4")
        rj = json.loads((d / "render.json").read_text(encoding="utf-8")) if (d / "render.json").exists() else {}
        rj.update(lufs=lufs, rerendered=True, encoder=r["encoder"])
        atomic_write_json(d / "render.json", rj)
        print(d.name, r["encoder"], lufs, flush=True)


if __name__ == "__main__":
    main()
