"""Ingest: rolling 10 s MPEG-TS segments + live chat, from a live URL or a (growing) file.

Stream time t = seconds since capture start. Every segment gets an absolute [start, end) in t.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from .signals import ChatMsg, parse_chat_line
from .util import BELOW_NORMAL, FFMPEG, FFPROBE, YTDLP, log


class SegmentIndex:
    def __init__(self, seg_dir: Path):
        self.dir = seg_dir
        self.items: list[tuple[str, float, float]] = []  # (path, t0, t1)
        self._lock = threading.Lock()

    def add(self, path: str, t0: float, t1: float) -> None:
        with self._lock:
            self.items.append((path, t0, t1))

    @property
    def end(self) -> float:
        with self._lock:
            return self.items[-1][2] if self.items else 0.0

    def covering(self, t0: float, t1: float) -> list[tuple[str, float, float]]:
        with self._lock:
            return [it for it in self.items if it[2] > t0 and it[1] < t1 and os.path.exists(it[0])]

    def prune(self, keep_after_t: float) -> int:
        n = 0
        with self._lock:
            for p, a, b in self.items:
                if b < keep_after_t and os.path.exists(p):
                    try:
                        os.remove(p)
                        n += 1
                    except OSError:
                        pass
        return n


class Ingest:
    """Runs (and restarts) the capture/segmenter process; tails its segment list."""

    def __init__(self, cfg: dict, stream_dir: Path):
        self.cfg = cfg
        src = cfg["source"]
        self.mode = src["mode"]
        self.url = src["url"]
        self.seg_dir = stream_dir / "seg"
        self.seg_dir.mkdir(parents=True, exist_ok=True)
        self.index = SegmentIndex(self.seg_dir)
        self.procs: list[subprocess.Popen] = []
        self.chat_proc: subprocess.Popen | None = None
        self.run_no = 0
        self.run_offset = 0.0
        self._list_pos = 0
        self._list_path: Path | None = None
        self.restarts = 0
        self.ended = False
        self.disk_stopped = False
        self.last_growth = time.time()
        if self.mode == "file":
            fp = src["file"]
            m = re.search(r"_(\d{9,11})", Path(fp).name)
            self.epoch = float(src.get("start_epoch") or (m.group(1) if m else os.path.getmtime(fp)))
            self.chat_path = Path(src["chat_file"]) if src.get("chat_file") else None
        else:
            self.epoch = float(src.get("start_epoch") or time.time())
            self.chat_path = None
        self._chat_pos = 0
        self._chat_buf = b""

    # ---- capture ----------------------------------------------------------------
    def start(self) -> None:
        self.run_no += 1
        seg_s = str(self.cfg["ingest"]["segment_seconds"])
        tag = f"{self.mode[0]}{int(self.epoch)}_{int(time.time())}_{self.run_no:03d}"
        ext = "ts"
        if self.mode == "file":  # VP9/AV1 VODs cannot go into MPEG-TS
            pr = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name",
                                 "-of", "csv=p=0", self.cfg["source"]["file"]], stdout=subprocess.PIPE, timeout=60)
            if pr.stdout.decode().strip() not in ("h264", "hevc", ""):
                ext = "mkv"
        self._list_path = self.seg_dir / f"list_{tag}.csv"
        self._list_pos = 0
        start_no = len(self.index.items)
        seg_args = ["-map", "0:v:0", "-map", "0:a:0", "-c", "copy", "-f", "segment", "-segment_time", seg_s,
                    "-segment_list", str(self._list_path), "-segment_list_type", "csv",
                    "-segment_start_number", str(start_no), "-reset_timestamps", "1",
                    str(self.seg_dir / f"{tag}_%06d.{ext}")]
        rp = self.cfg["source"].get("replay") or {}
        self.replay = bool(rp.get("enabled"))
        if self.mode == "file" and self.replay and not self.index.items:
            # REPLAY: feed a recording through the real-time path at `rate` x speed, starting at `from_s`
            self.following = False
            self.run_offset = float(rp.get("from_s", 0))
            self.replay_rate = float(rp.get("rate", 1.0))
            self.replay_wall0 = time.time()
            cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-readrate", str(self.replay_rate),
                   "-ss", f"{self.run_offset:.2f}", "-i", "file:" + self.cfg["source"]["file"]] +                 (["-t", f"{float(rp['to_s']) - self.run_offset:.2f}"] if rp.get("to_s") else []) + seg_args
            self.procs = [subprocess.Popen(cmd, creationflags=BELOW_NORMAL, stdin=subprocess.DEVNULL)]
        elif self.mode == "file":
            self.run_offset = 0.0 if not self.index.items else self.index.end
            # -follow only while the file is still growing: on a finished file ffmpeg's probe waits forever at EOF
            growing = self.cfg["source"].get("follow", "auto") != "never" and (time.time() - os.path.getmtime(self.cfg["source"]["file"]) < 30)
            self.following = growing
            cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y"] + (["-follow", "1"] if growing else []) +                 ["-i", "file:" + self.cfg["source"]["file"]] + seg_args
            if self.index.items:  # resume from where we were
                cmd[cmd.index("-i"):cmd.index("-i")] = ["-ss", f"{self.index.end:.2f}"]
            self.procs = [subprocess.Popen(cmd, creationflags=BELOW_NORMAL, stdin=subprocess.DEVNULL)]
        else:
            self.run_offset = time.time() - self.epoch
            # YouTube live serves split video/audio HLS; resolve fresh URLs (they expire) and let ffmpeg read them
            g = subprocess.run([YTDLP, "--no-warnings", "-g", "-f", self.cfg["source"].get("live_format", "best"),
                                self.url], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
            urls = [u for u in g.stdout.decode().split() if u.startswith("http")]
            if not urls:
                log(f"yt-dlp -g failed: {g.stderr.decode('utf-8', 'replace')[-300:]}")
                self.procs = []
                return
            inp = []
            for u in urls[:2]:
                inp += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "10", "-i", u]
            amap = "1:a:0" if len(urls) > 1 else "0:a:0"
            seg = list(seg_args)
            seg[seg.index("0:a:0")] = amap
            p2 = subprocess.Popen([FFMPEG, "-hide_banner", "-loglevel", "error", "-y"] + inp + seg,
                                  stdin=subprocess.DEVNULL, creationflags=BELOW_NORMAL)
            self.procs = [p2]
            if self.chat_proc is None or self.chat_proc.poll() is not None:
                base = self.seg_dir.parent / f"chat_{int(time.time())}"
                self.chat_proc = subprocess.Popen(
                    [YTDLP, "-q", "--no-warnings", "--skip-download", "--write-subs", "--sub-langs", "live_chat",
                     "-o", str(base), self.url], creationflags=BELOW_NORMAL, stdin=subprocess.DEVNULL)
                self.chat_path = Path(str(base) + ".live_chat.json")
                self._chat_pos = 0
        log(f"ingest run {self.run_no} started ({self.mode}), offset {self.run_offset:.1f}s")

    def alive(self) -> bool:
        return any(p.poll() is None for p in self.procs)

    def stop(self) -> None:
        for p in self.procs + ([self.chat_proc] if self.chat_proc else []):
            if p and p.poll() is None:
                p.terminate()
                try:
                    p.wait(10)
                except subprocess.TimeoutExpired:
                    p.kill()

    def poll_segments(self) -> list[tuple[str, float, float]]:
        new = []
        if not self._list_path or not self._list_path.exists():
            return new
        with open(self._list_path, "rb") as f:
            f.seek(self._list_pos)
            data = f.read()
        lines = data.split(b"\n")
        for raw in lines[:-1]:
            self._list_pos += len(raw) + 1
            parts = raw.decode().strip().split(",")
            if len(parts) < 3:
                continue
            name, a, b = parts[0], float(parts[1]), float(parts[2])
            a, b = a + self.run_offset, b + self.run_offset
            item = (str(self.seg_dir / name), a, b)
            self.index.add(*item)
            new.append(item)
        if new:
            self.last_growth = time.time()
        return new

    def file_growing(self) -> bool:
        if self.mode != "file":
            return True
        if not getattr(self, "following", True):
            return False  # finished file: ffmpeg read it without -follow
        try:
            st = os.path.getmtime(self.cfg["source"]["file"])
        except OSError:
            return False
        return time.time() - st < self.cfg["source"]["stall_end_seconds"]

    def disk_ok(self) -> bool:
        free_gb = shutil.disk_usage(str(self.seg_dir)).free / 1e9
        return free_gb >= self.cfg["ingest"]["min_free_gb"]

    # ---- chat -----------------------------------------------------------------
    def poll_chat(self) -> list[ChatMsg]:
        path = self.chat_path
        if path is None:
            return []
        if not path.exists():
            sp = str(path)
            alt = Path(sp[:-5]) if sp.endswith(".part") else Path(sp + ".part")
            if alt.exists():
                path = alt
            else:
                return []
        try:
            size = path.stat().st_size
        except OSError:
            return []
        if size < self._chat_pos:  # file rotated (.part -> final)
            self._chat_pos = 0
        with open(path, "rb") as f:
            f.seek(self._chat_pos)
            data = f.read()
        self._chat_pos += len(data)
        data = self._chat_buf + data
        lines = data.split(b"\n")
        self._chat_buf = lines[-1]
        out = []
        for raw in lines[:-1]:
            try:
                out.extend(parse_chat_line(json.loads(raw)))
            except Exception:
                continue
        return out
