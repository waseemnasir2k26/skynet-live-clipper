"""Supervisor: python run.py [--config config.yaml] [--live URL] [--file PATH --chat PATH]

Stop cleanly: create the file named in config paths.stop_file (default C:/clipper-data/STOP).
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
    ap.add_argument("--profile", choices=["full", "light"], default=None, help="light = VPS / no GPU profile")
    a = ap.parse_args()
    cfg = yaml.safe_load(open(a.config, encoding="utf-8"))
    cfg["_config_path"] = a.config
    if a.live:
        cfg["source"].update(mode="live", url=a.live)
    if a.file:
        cfg["source"].update(mode="file", file=a.file, chat_file=a.chat or "")
    if a.profile == "light":
        lm = cfg["safety"]["light_mode"]
        cfg["asr"].update(model=lm["asr_model"], device=lm["asr_device"], compute_type="int8", beam_size=1)
        cfg["render"]["encoder"] = lm["encoder"]
        cfg["clips"]["max_per_hour"] = lm["max_per_hour"]
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
