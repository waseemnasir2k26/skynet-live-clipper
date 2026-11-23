"""LLM judge (headless `claude -p --model haiku`) with strict JSON + deterministic fallback."""
from __future__ import annotations

import json
import re
import subprocess

from .util import BELOW_NORMAL, log

PROMPT = """You are the editor of a top YouTube Shorts clipping channel for the Indian streamer {streamer}.
Stream: "{title}". Language: Hindi/Hinglish. Judge whether this window of the live stream contains a
moment worth a standalone Short (funny reaction, rage, fail, shock, clutch play, roast, chat interaction,
story with a punchline). Boring driving, menu time, silence, and generic talk score low. Be strict.

Window: stream seconds {w0:.0f} to {w1:.0f}. Signals: {sig}.
The transcript is automatic speech recognition of a loud gameplay mix, so it is often fragmentary or
garbled: infer what happened from transcript + loudness peaks + chat reactions together, and do not
mark a moment down just because the ASR is messy. Shouting, laughing, panic, chases, crashes, deaths,
roasting chat, and big chat reactions ("L", "😂", emote spam, "W") are what makes a good clip here.
Transcript (stream-second: words, Hindi in Devanagari or roman):
{transcript}

Chat messages during/after the window (viewer reactions, may lag ~10 s):
{chat}

Pick the best self-contained clip inside the window: 20-55 seconds, starts right before the hook so
the first 2 seconds already grab attention, ends after the payoff / complete thought (never mid-sentence).
Reply with ONLY one JSON object, no prose, no code fence:
{{"score": <0-100 viral potential>, "reason": "<one line, English, what happens>",
"start": <stream second>, "end": <stream second>,
"title": "<hook title, max 6 words, Roman Hinglish or English, no emojis, no quotes>",
"caption": "<1-2 line social caption, Roman Hinglish, max 150 chars>",
"hashtags": ["#tag", "..."] }}"""


def build_prompt(streamer: str, title: str, w0: float, w1: float, sig: str, lines: list[str], chat: list[str]) -> str:
    return PROMPT.format(streamer=streamer, title=title, w0=w0, w1=w1, sig=sig,
                         transcript="\n".join(lines) or "(no speech detected)",
                         chat="\n".join(f"- {c}" for c in chat) or "(none)")


def parse_reply(txt: str, w0: float, w1: float, min_len: float, max_len: float) -> dict | None:
    m = re.search(r"\{.*\}", txt, re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    try:
        score = float(d["score"])
        s, e = float(d["start"]), float(d["end"])
    except (KeyError, TypeError, ValueError):
        return None
    s, e = fix_bounds(s, e, w0, w1, min_len, max_len)
    tags = [t if str(t).startswith("#") else "#" + str(t) for t in (d.get("hashtags") or [])][:6]
    return {"score": max(0.0, min(100.0, score)), "reason": str(d.get("reason", ""))[:200], "start": s, "end": e,
            "title": str(d.get("title", "")).strip().strip('"')[:60], "caption": str(d.get("caption", ""))[:220],
            "hashtags": [re.sub(r"\s+", "", str(t)) for t in tags]}


def fix_bounds(s: float, e: float, w0: float, w1: float, min_len: float, max_len: float) -> tuple[float, float]:
    s, e = max(w0, min(s, w1)), max(w0, min(e, w1))
    if e < s:
        s, e = e, s
    if e - s > max_len:
        e = s + max_len
    if e - s < min_len:
        need = min_len - (e - s)
        s = max(w0, s - need * 0.4)
        e = min(w1, s + min_len)
        if e - s < min_len:
            s = max(w0, e - min_len)
    return round(s, 2), round(e, 2)


def call_llm(cmd: list[str], prompt: str, timeout: float, cwd: str) -> str | None:
    try:
        p = subprocess.run(cmd, input=prompt.encode("utf-8"), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=timeout, cwd=cwd, creationflags=BELOW_NORMAL)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        log(f"llm call failed: {e}")
        return None
    if p.returncode != 0:
        log(f"llm rc={p.returncode}: {p.stderr.decode('utf-8', 'replace')[:300]}")
        return None
    return p.stdout.decode("utf-8", "replace")


def fallback_judgement(peak_score: float, kw_hits: int, w0: float, w1: float, center: float,
                       min_len: float, max_len: float, roman_text: str) -> dict:
    """Deterministic path when the LLM is unavailable or capped."""
    score = min(100.0, 30 + peak_score * 9 + min(kw_hits, 6) * 3)
    s, e = fix_bounds(center - 22, center + 12, w0, w1, min_len, max_len)
    words = roman_text.split()
    title = " ".join(words[:5]).upper() if words else "WAIT FOR IT"
    return {"score": round(score, 1), "reason": f"signal spike {peak_score:.1f} (fallback, no LLM)", "start": s,
            "end": e, "title": title[:40], "caption": "Live moment " + " ".join(words[:12]),
            "hashtags": ["#shorts", "#live", "#gaming"]}
