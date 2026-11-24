"""Channel watcher: poll a channel cheaply and start the engine in live mode when it goes live.

  py -3.11 watch.py --channel https://www.youtube.com/@SomeChannel [--every 300]

Each poll = one `yt-dlp --print live_status <channel>/live` call (no download). When it reports is_live, the
watcher runs `run.py --live <watch URL>` (same caps, STOP breaker, light mode after the full-power window) and
resumes polling after the engine exits. It never starts a second engine while one is running.
Stop: create data/STOP (stops the engine too) or data/WATCH-STOP (watcher only).
Caps: --max-polls (default 4000), --max-hours (default 168), engine starts per day (default 6).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent


def log(path: Path, msg: str) -> None:
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + msg
    print(line, flush=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], stdout=subprocess.PIPE).stdout.decode("utf-8", "replace")
        return str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def poll(channel: str) -> tuple[str, str]:
    """-> (live_status, watch_url). Cheap: metadata only."""
    url = channel.rstrip("/")
    for suffix in ("/streams", "/live", "/featured"):
        if url.endswith(suffix):
            url = url[: -len(suffix)]
    url += "/live"
    try:
        p = subprocess.run(["yt-dlp", "--no-warnings", "--no-playlist", "--print", "%(live_status)s %(id)s", url],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
    except subprocess.TimeoutExpired:
        return "timeout", ""
    out = p.stdout.decode().strip().split()
    if len(out) >= 2:
        return out[0], f"https://www.youtube.com/watch?v={out[1]}"
    err = p.stderr.decode("utf-8", "replace").strip().splitlines()
    return "not_live" if any("not currently live" in e or "will begin" in e or "Premieres" in e for e in err) \
        else ("error: " + (err[-1][:160] if err else "no output")), ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", required=True, help="channel URL, e.g. https://www.youtube.com/@SomeChannel")
    ap.add_argument("--every", type=int, default=300, help="seconds between polls")
    ap.add_argument("--max-polls", type=int, default=4000)
    ap.add_argument("--max-hours", type=float, default=168)
    ap.add_argument("--max-starts-per-day", type=int, default=6)
    ap.add_argument("--config", default=str(HERE / "config.yaml"))
    a = ap.parse_args()
    cfg = yaml.safe_load(open(a.config, encoding="utf-8"))
    for k, v in cfg["paths"].items():
        if not os.path.isabs(v):
            cfg["paths"][k] = str(HERE / v)
    root = Path(cfg["paths"]["data_root"])
    root.mkdir(parents=True, exist_ok=True)
    logp = root / "watch.log"
    stop, wstop = Path(cfg["paths"]["stop_file"]), root / "WATCH-STOP"
    (root / "watch.pid").write_text(str(os.getpid()))
    t0, polls, starts = time.time(), 0, []
    log(logp, f"watcher start pid={os.getpid()} channel={a.channel} every={a.every}s")
    while True:
        if stop.exists() or wstop.exists():
            log(logp, "STOP / WATCH-STOP present; watcher exiting")
            return 0
        if polls >= a.max_polls or time.time() - t0 > a.max_hours * 3600:
            log(logp, "poll / wall-clock cap reached; watcher exiting")
            return 0
        polls += 1
        status, url = poll(a.channel)
        log(logp, f"poll #{polls}: {status} {url}")
        try:
            epid = int((root / "engine.pid").read_text().strip())
        except Exception:
            epid = 0
        if status == "is_live" and url:
            starts = [s for s in starts if time.time() - s < 86400]
            if pid_alive(epid):
                log(logp, f"stream live but an engine (pid {epid}) is already running; not starting another")
            elif len(starts) >= a.max_starts_per_day:
                log(logp, "engine start cap per day reached; not starting")
            else:
                starts.append(time.time())
                log(logp, f"LIVE -> starting engine: run.py --live {url}")
                rc = subprocess.run([sys.executable, str(HERE / "run.py"), "--config", a.config, "--live", url],
                                    cwd=str(HERE)).returncode
                log(logp, f"engine exited rc={rc}; back to polling")
                continue
        for _ in range(a.every):
            if stop.exists() or wstop.exists():
                break
            time.sleep(1)


if __name__ == "__main__":
    sys.exit(main())
