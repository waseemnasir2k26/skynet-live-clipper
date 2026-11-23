"""python -m publish.cli {enqueue CLIP | schedule | tick [--live] | dry-run CLIP | ledger | status}
Real posting needs: settings auto_publish=true AND env AUTO_PUBLISH=true AND --live. Default = dry run."""
import argparse, json, sys
from datetime import datetime
from . import core
from .adapters import ADAPTERS, get_adapter


def main(argv=None):
    ap = argparse.ArgumentParser(prog="publish")
    ap.add_argument("cmd", choices=["enqueue", "schedule", "tick", "dry-run", "ledger", "status"])
    ap.add_argument("clip", nargs="?")
    ap.add_argument("--live", action="store_true", help="allow real posting (also needs AUTO_PUBLISH=true + auto_publish setting)")
    a = ap.parse_args(argv)
    if a.cmd == "enqueue":
        print(json.dumps(core.enqueue(a.clip), indent=1))
    elif a.cmd == "schedule":
        print(json.dumps(core.schedule_approved(), indent=1))
    elif a.cmd == "tick":
        print(json.dumps(core.tick(live=a.live), indent=1))
    elif a.cmd == "dry-run":
        for d in ADAPTERS:
            item = {"id": core.qid(a.clip, d), "clip_id": a.clip, "dest": d, "attempts": 0}
            print(json.dumps(core.publish_one(item, get_adapter(d), dry_run=True), indent=1, ensure_ascii=False))
    elif a.cmd == "ledger":
        for e in core.ledger():
            print(json.dumps(e, ensure_ascii=False))
    elif a.cmd == "status":
        print(json.dumps({"config": core.load_config(), "breaker": core.breaker_state(), "stopped": core.stopped(),
                          "queue": core.load_queue()}, indent=1))


if __name__ == "__main__":
    main()
