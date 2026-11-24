"""Pure signal logic: rolling robust z-scores, chat parsing/scoring, spike + dedupe.
No I/O here so it is unit-testable."""
from __future__ import annotations

import bisect
import re
from collections import deque
from dataclasses import dataclass, field

import numpy as np


class RollingZ:
    """Robust z-score of a value against a rolling window (median / MAD)."""

    def __init__(self, window: int, min_samples: int = 30, floor: float = 0.5):
        self.buf: deque[float] = deque(maxlen=window)
        self.min_samples = min_samples
        self.floor = floor  # MAD floor so a flat baseline does not explode z

    def push(self, x: float) -> float:
        z = self.z(x)
        self.buf.append(x)
        return z

    def z(self, x: float) -> float:
        if len(self.buf) < self.min_samples:
            return 0.0
        arr = np.fromiter(self.buf, float)
        med = float(np.median(arr))
        mad = float(np.median(np.abs(arr - med))) * 1.4826
        return (x - med) / max(mad, self.floor)


# --- chat -------------------------------------------------------------------
HYPE_PATTERNS = [
    (re.compile(r"(😂|🤣|😭|💀|😆|🤯|😱|🔥|☠️|😹)"), 1.0),
    (re.compile(r"\b(l+o+l+|lmao+|lmfao|rofl|kekw|xd+|haha+h*|hehe+|hahaha\w*)\b", re.I), 1.0),
    (re.compile(r"\b(omg|wtf|bruh+|op+|goat|legend|insane|gg|w+|rip|noo+|yoo+|letsgo+|let'?s go+)\b", re.I), 0.7),
    (re.compile(r"\b(bhai+|bhau|bc|mc|gajab|kya baat|maza|mazaa|hasi|has has|pagal|bawal|op bolte)\b", re.I), 0.6),
    (re.compile(r"(हा+हा|हाहा|भाई|मज़ा|मजा)"), 0.6),
    (re.compile(r"[!?]{3,}"), 0.4),
    (re.compile(r":_?\w*(lol|haha|cry|laugh|rofl|kekw|dead|skull|omg|shock|pog|hype|lmao|kill)\w*:", re.I), 0.8),
    (re.compile(r"(^|\s)(l+|shit|damn|f+|ded|dead|💀+)($|\s)", re.I), 0.5),
]


def hype_score(text: str) -> float:
    s = 0.0
    for pat, w in HYPE_PATTERNS:
        n = len(pat.findall(text))
        s += min(n, 3) * w
    if len(text) >= 4 and text.isupper() and any(c.isalpha() for c in text):
        s += 0.5
    return min(s, 4.0)


@dataclass
class ChatMsg:
    ts: float          # wall-clock epoch seconds
    author: str
    text: str
    amount: str = ""   # superchat amount text, "" if none
    offset: float | None = None  # seconds into the video (replay chat / VOD), if provided

    @property
    def is_super(self) -> bool:
        return bool(self.amount)


def _runs_text(runs: list) -> str:
    out = []
    for r in runs or []:
        if "text" in r:
            out.append(r["text"])
        elif "emoji" in r:
            e = r["emoji"]
            sc = (e.get("shortcuts") or [""])[0]
            eid = e.get("emojiId", "")
            out.append(eid if len(eid) <= 4 else sc)
    return "".join(out)


def parse_chat_line(obj: dict) -> list[ChatMsg]:
    """Parse one yt-dlp live_chat JSONL line into messages (YouTube format)."""
    rep = obj.get("replayChatItemAction", {})
    acts = rep.get("actions", [])
    try:
        off = int(rep["videoOffsetTimeMsec"]) / 1000.0
    except (KeyError, TypeError, ValueError):
        off = None
    out = []
    for a in acts:
        item = a.get("addChatItemAction", {}).get("item", {})
        for key in ("liveChatTextMessageRenderer", "liveChatPaidMessageRenderer"):
            r = item.get(key)
            if not r:
                continue
            try:
                ts = int(r.get("timestampUsec", "0")) / 1e6
            except ValueError:
                continue
            text = _runs_text(r.get("message", {}).get("runs", []))
            author = r.get("authorName", {}).get("simpleText", "")
            amt = r.get("purchaseAmountText", {}).get("simpleText", "") if key == "liveChatPaidMessageRenderer" else ""
            out.append(ChatMsg(ts, author, text, amt, off))
    return out


# --- per-second timeline ------------------------------------------------------
class Timeline:
    """Per-second buckets keyed by stream time (seconds since capture start)."""

    def __init__(self):
        self.chat_n: dict[int, int] = {}
        self.chat_hype: dict[int, float] = {}
        self.chat_msgs: dict[int, list[str]] = {}
        self.audio_db: dict[int, float] = {}

    def add_chat(self, t: float, text: str, superchat: bool = False) -> None:
        s = int(t)
        self.chat_n[s] = self.chat_n.get(s, 0) + 1
        self.chat_hype[s] = self.chat_hype.get(s, 0.0) + hype_score(text) + (5.0 if superchat else 0.0)
        self.chat_msgs.setdefault(s, []).append(text[:120])

    def chat_rate(self, t: int, win: int) -> tuple[float, float]:
        n = sum(self.chat_n.get(s, 0) for s in range(t - win + 1, t + 1))
        h = sum(self.chat_hype.get(s, 0.0) for s in range(t - win + 1, t + 1))
        return n / win, h / win

    def messages(self, t0: float, t1: float, limit: int = 40) -> list[str]:
        out = []
        for s in range(int(t0), int(t1) + 1):
            out.extend(self.chat_msgs.get(s, []))
        if len(out) > limit:  # keep an even spread
            step = len(out) / limit
            out = [out[int(i * step)] for i in range(limit)]
        return out


@dataclass
class Peak:
    t: float
    score: float
    chat_z: float = 0.0
    audio_z: float = 0.0
    kind: str = "combined"
    reasons: list[str] = field(default_factory=list)


def combined_score(chat_z: float, hype_z: float, audio_z: float, hype_weight: float = 0.6) -> float:
    """Chat and audio both count; agreement is rewarded (sqrt of the product)."""
    c = max(chat_z, 0.0) + hype_weight * max(hype_z, 0.0)
    a = max(audio_z, 0.0)
    agree = (c * a) ** 0.5
    return max(c, a * 0.95) + 0.5 * agree


class SpikeDetector:
    """Emits one Peak per excursion above threshold (local max inside the excursion)."""

    def __init__(self, threshold: float, min_gap: float):
        self.threshold = threshold
        self.min_gap = min_gap
        self._cur: Peak | None = None
        self._last_emit_t = -1e9

    def update(self, peak: Peak) -> Peak | None:
        if peak.score >= self.threshold:
            if self._cur is None or peak.score > self._cur.score:
                self._cur = peak
            return None
        # below threshold: close excursion
        if self._cur is not None:
            done, self._cur = self._cur, None
            if done.t - self._last_emit_t >= self.min_gap:
                self._last_emit_t = done.t
                return done
        return None


def dedupe(cands: list[dict], min_gap: float) -> list[dict]:
    """Keep the best-scored candidate among any overlapping or near ones.
    cands: dicts with t_start, t_end, score."""
    kept: list[dict] = []
    for c in sorted(cands, key=lambda c: -c["score"]):
        ok = True
        for k in kept:
            overlap = min(c["t_end"], k["t_end"]) - max(c["t_start"], k["t_start"])
            if overlap > 0 or abs((c["t_start"] + c["t_end"]) / 2 - (k["t_start"] + k["t_end"]) / 2) < min_gap:
                ok = False
                break
        if ok:
            kept.append(c)
    return sorted(kept, key=lambda c: c["t_start"])


def overlaps_any(t0: float, t1: float, ranges: list[tuple[float, float]], pad: float = 5.0) -> bool:
    return any(min(t1, b) - max(t0, a) > -pad for a, b in ranges)


class HourBudget:
    """Sliding one-hour cap (for clips per hour, LLM calls per hour)."""

    def __init__(self, per_hour: int, total: int | None = None):
        self.per_hour = per_hour
        self.total = total
        self.stamps: list[float] = []
        self.count = 0

    def allow(self, now: float) -> bool:
        i = bisect.bisect_left(self.stamps, now - 3600)
        self.stamps = self.stamps[i:]
        if self.total is not None and self.count >= self.total:
            return False
        return len(self.stamps) < self.per_hour

    def take(self, now: float) -> None:
        self.stamps.append(now)
        self.count += 1


KEYWORDS = [  # transcript cues (Devanagari + roman)
    "अरे", "भाई", "क्या", "मर गया", "मार", "बचाओ", "भागो", "पागल", "गजब", "हाहा", "ओ भाई", "ओ तेरी",
    "पुलिस", "गोली", "बम", "क्रैश", "एक्सप्लो", "उड़", "जल्दी", "चल चल", "बस बस", "अबे",
    "what", "oh my god", "bro", "no no", "run", "police", "wtf", "crash", "boom", "lol",
]


def keyword_hits(text: str) -> int:
    t = text.lower()
    return sum(t.count(k.lower()) for k in KEYWORDS)
