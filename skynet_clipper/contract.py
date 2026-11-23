"""Output contract shared with the dashboard. Do not change field names."""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

META_FIELDS = {
    "id": str, "stream_url": str, "streamer": str, "stream_title": str,
    "t_start": (int, float), "t_end": (int, float), "duration": (int, float),
    "created_at": str, "score": (int, float), "reasons": list, "signals": dict,
    "transcript": str, "title": str, "caption": str, "hashtags": list, "status": str,
}
SIGNAL_FIELDS = ("chat_z", "audio_z", "llm_score")
STATE_FIELDS = ("stream_url", "streamer", "stream_title", "live", "started_at", "captured_seconds",
                "chat_messages", "candidates_seen", "clips_ready", "last_heartbeat",
                "current_signals", "last_transcript_line")
EVENT_TYPES = {"heartbeat", "candidate", "judged", "cut", "error", "info"}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def validate_meta(meta: dict) -> list[str]:
    errs = []
    for k, typ in META_FIELDS.items():
        if k not in meta:
            errs.append(f"missing {k}")
        elif not isinstance(meta[k], typ) or isinstance(meta[k], bool) and typ != bool:
            errs.append(f"bad type {k}")
    sig = meta.get("signals", {})
    for k in SIGNAL_FIELDS:
        if k not in sig:
            errs.append(f"missing signals.{k}")
    if meta.get("status") not in ("ready", "rendering", "failed"):
        errs.append("bad status")
    if not all(isinstance(h, str) for h in meta.get("hashtags", [])):
        errs.append("hashtags not str")
    return errs


def atomic_write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    for _ in range(20):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:  # reader holding the file on Windows
            time.sleep(0.05)
    os.replace(tmp, path)


class OutStore:
    def __init__(self, out_root: str):
        self.root = Path(out_root)
        (self.root / "clips").mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _append(self, name: str, obj: dict) -> None:
        line = json.dumps(obj, ensure_ascii=False) + "\n"
        with self._lock, open(self.root / name, "a", encoding="utf-8") as f:
            f.write(line)

    def event(self, typ: str, msg: str, data: dict | None = None) -> None:
        assert typ in EVENT_TYPES, typ
        self._append("events.jsonl", {"ts": now_iso(), "type": typ, "msg": msg, "data": data or {}})

    def signal(self, t: float, chat_rate: float, chat_z: float, audio_db: float, audio_z: float) -> None:
        self._append("signals.jsonl", {"ts": now_iso(), "t": round(t, 1), "chat_rate": round(chat_rate, 3),
                                       "chat_z": round(chat_z, 2), "audio_db": round(audio_db, 1),
                                       "audio_z": round(audio_z, 2)})

    def state(self, st: dict) -> None:
        missing = [k for k in STATE_FIELDS if k not in st]
        assert not missing, missing
        atomic_write_json(self.root / "state.json", st)

    def clip_dir(self, clip_id: str) -> Path:
        d = self.root / "clips" / clip_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def write_meta(self, clip_id: str, meta: dict) -> None:
        errs = validate_meta(meta)
        if errs:
            raise ValueError(f"meta contract violation: {errs}")
        atomic_write_json(self.clip_dir(clip_id) / "meta.json", meta)

    def read_commands(self, offset: int) -> tuple[list[dict], int]:
        p = self.root / "commands.jsonl"
        if not p.exists():
            return [], offset
        with open(p, "rb") as f:
            f.seek(offset)
            data = f.read()
        out = []
        consumed = 0
        for raw in data.split(b"\n")[:-1]:
            consumed += len(raw) + 1
            try:
                out.append(json.loads(raw.decode("utf-8")))
            except Exception:
                pass
        return out, offset + consumed
