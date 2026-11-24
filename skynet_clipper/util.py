from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

IS_WIN = os.name == "nt"
BELOW_NORMAL = 0x00004000 if IS_WIN else 0
FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"
YTDLP = shutil.which("yt-dlp") or "yt-dlp"

# politeness: leave ~25% of the CPU to whoever is using the machine
RENDER_THREADS = max(2, (os.cpu_count() or 4) * 3 // 4)


def be_polite(pid=None):
    """Lower this process priority (BelowNormal on Windows, nice 5 elsewhere)."""
    try:
        if IS_WIN:
            import ctypes
            ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), BELOW_NORMAL)
        else:
            os.nice(5)
    except Exception:
        pass

_LOG_FILE: Path | None = None
BAD_STDERR = ("Stray %", "Cannot find", "Unable to parse", "No such filter", "Invalid argument",
              "Error initializing filter", "Error opening", "Unable to open")


def set_log(path: Path) -> None:
    global _LOG_FILE
    _LOG_FILE = path
    path.parent.mkdir(parents=True, exist_ok=True)


def log(msg: str) -> None:
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + msg
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        print(line.encode("ascii", "replace").decode(), flush=True)
    if _LOG_FILE:
        with open(_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def run_ff(args: list[str], cwd: str | None = None, timeout: float = 900) -> subprocess.CompletedProcess:
    """Run ffmpeg BelowNormal with capped threads; fail on rc!=0 or known silent-failure stderr."""
    if "-threads" not in args:
        args = args[:1] + ["-threads", str(RENDER_THREADS)] + args[1:]
    p = subprocess.run(args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       creationflags=BELOW_NORMAL, timeout=timeout)
    err = p.stderr.decode("utf-8", "replace")
    if p.returncode != 0 or any(b in err for b in BAD_STDERR):
        raise RuntimeError(f"ffmpeg rc={p.returncode}: {err[-800:]}")
    p.stderr_text = err  # type: ignore[attr-defined]
    return p
