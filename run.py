"""Supervisor: python run.py [--config config.yaml] [--live URL] [--file PATH --chat PATH]

Stop cleanly: create the file named in config paths.stop_file (default data/STOP).
"""
from __future__ import annotations

import argparse
import copy
import os
import sys
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))
from skynet_clipper.engine import Engine  # noqa: E402
from skynet_clipper.util import be_polite, log  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(Path(__file__).parent / "config.yaml"))
    ap.add_argument("--live", help="live URL (YouTube / Twitch / Kick); overrides config source")
    ap.add_argument("--file", help="follow an existing capture file (growing or finished VOD)")
    ap.add_argument("--chat", help="yt-dlp live_chat json for --file mode")
    ap.add_argument("--start-epoch", type=float, default=None,
                    help="wall-clock epoch of t=0 in --file (maps wall-timed live chat onto a VOD)")
    ap.add_argument("--replay", action="store_true",
                    help="replay --file through the real-time path (state.json mode=replay, never 'live')")
    ap.add_argument("--replay-from", type=float, default=0.0, help="replay start second")
    ap.add_argument("--replay-to", type=float, default=0.0, help="replay end second (0 = end of file)")
    ap.add_argument("--replay-rate", type=float, default=1.0, help="replay speed (1.0 = real time)")
    ap.add_argument("--vod", action="store_true", help="--file is a finished VOD: chat timed by video offset")
    ap.add_argument("--profile", choices=["full", "light"], default=None, help="light = VPS / no GPU profile")
    a = ap.parse_args()
    cfg = yaml.safe_load(open(a.config, encoding="utf-8"))
    cfg["_config_path"] = a.config
    for k, v in cfg["paths"].items():      # relative paths are relative to the repo folder, not the shell's cwd
        if not os.path.isabs(v):
            cfg["paths"][k] = str(Path(__file__).resolve().parent / v)
    if a.live:
        cfg["source"].update(mode="live", url=a.live, start_epoch=0)
        try:  # real title/channel of this stream for meta.json
            import subprocess
            t = subprocess.run(["yt-dlp", "--no-warnings", "--print", "%(title)s", a.live], stdout=subprocess.PIPE,
                               timeout=90).stdout.decode("utf-8", "replace").strip()
            if t:
                cfg["source"]["stream_title"] = t.splitlines()[0][:150]
        except Exception:
            pass
    if a.file:
        cfg["source"].update(mode="file", file=a.file, chat_file=a.chat or "",
                             chat_time="offset" if a.vod else cfg["source"].get("chat_time", "wall"))
    if a.replay:
        cfg["source"]["replay"] = {"enabled": True, "from_s": a.replay_from, "rate": a.replay_rate,
                                   "to_s": a.replay_to}
    if a.start_epoch:
        cfg["source"]["start_epoch"] = a.start_epoch
    if a.profile == "light":
        lm = cfg["safety"]["light_mode"]
        cfg["asr"].update(model=lm["asr_model"], device=lm["asr_device"], compute_type="int8", beam_size=1)
        cfg["render"]["encoder"] = lm["encoder"]
        cfg["clips"]["max_per_hour"] = lm["max_per_hour"]
    log("SkynetLabs Clipping System | AI that watches live streams and clips the best moments | www.skynetlabsai.com")
    be_polite()
    os.makedirs(cfg["paths"]["data_root"], exist_ok=True)
    (Path(cfg["paths"]["data_root"]) / "engine.pid").write_text(str(os.getpid()))
    while True:
        eng = Engine(copy.deepcopy(cfg))
        reason = eng.run()
        if reason == "switch" and eng.switch_url:
            same = eng.switch_url == cfg["source"]["url"]
            cfg["source"].update(mode="live", url=eng.switch_url,
                                 start_epoch=eng.ingest.epoch if same else 0)
            log(f"switching to {eng.switch_url}")
            continue
        log(f"supervisor exit: {reason}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
