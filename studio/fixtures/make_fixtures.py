"""Generate DEMO fixtures under studio/fixtures/data/out (engine contract). Run: python make_fixtures.py
Everything here is synthetic: abstract covers, no real people, no real stream footage."""
import json, math, random, shutil, subprocess, sys
from datetime import datetime, timedelta
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
import os
OUT = Path(os.environ.get("CLIPPER_FIXTURE_DIR", HERE / "data")) / "out"
FONTS = HERE.parent / "static" / "fonts"
FF = shutil.which("ffmpeg") or "ffmpeg"
INK, CREAM, RUST = (26, 26, 26), (242, 239, 230), (198, 107, 63)
rnd = random.Random(24)

CLIPS = [
    ("Chat explodes as the clutch lands", 94, 41, ["Chat spike 4.2x baseline", "Audio peak +9 dB", "AI judge: clean payoff"], "chat went wild the second the last round flipped"),
    ("The plan nobody saw coming", 88, 33, ["Audio peak +7 dB", "AI judge: surprise reversal"], "okay so here is the thing, we do not shoot, we wait"),
    ("Rage quit then instant comeback", 91, 27, ["Chat spike 3.6x baseline", "AI judge: strong emotional arc"], "I am done, I am so done. wait. wait wait wait."),
    ("Streamer reads the worst take of the night", 79, 36, ["Chat spike 2.9x baseline", "AI judge: funny, self-contained"], "somebody in chat just said pineapple is a tier one topping"),
    ("One-tap from across the map", 86, 22, ["Audio peak +8 dB", "Chat spike 3.1x baseline"], "no way. no way that went in. clip it. clip that."),
    ("Chat predicts the ending perfectly", 83, 31, ["Chat spike 3.3x baseline", "AI judge: audience payoff"], "chat called it three minutes ago and I laughed at them"),
    ("Silent two minutes, then total chaos", 72, 44, ["Audio peak +6 dB", "AI judge: needs tighter intro"], "and then everything just happened at once"),
    ("Giveaway reaction", 68, 19, ["Chat spike 2.4x baseline"], "wait, who just won? is that actually a real name?"),
]


def font(name, size):
    return ImageFont.truetype(str(FONTS / name), size)


def wrap(d, text, f, width):
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if d.textlength(t, font=f) <= width:
            cur = t
        else:
            lines.append(cur)
            cur = w
    return lines + [cur]


def cover(path, title, seed, score):
    r = random.Random(seed)
    im = Image.new("RGB", (1080, 1920), INK)
    d = ImageDraw.Draw(im)
    # abstract waveform bars: the "moment" detected
    n, base = 54, 1020
    peak = r.randint(20, 34)
    for i in range(n):
        h = 40 + 520 * math.exp(-((i - peak) ** 2) / 60) * (0.6 + 0.4 * r.random()) + 60 * r.random()
        x = 40 + i * 19
        col = RUST if abs(i - peak) < 6 else (74, 70, 64)
        d.rounded_rectangle([x, base - h, x + 11, base + h * 0.35], radius=5, fill=col)
    d.rectangle([0, 0, 1080, 8], fill=RUST)
    d.text((64, 1500), "DEMO FRAME", font=font("IBMPlexMono-600.ttf", 34), fill=RUST)
    im.save(path, quality=88)


def make():
    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT / "clips").mkdir(parents=True)
    now = datetime.now().replace(microsecond=0)
    started = now - timedelta(hours=3, minutes=42, seconds=17)
    for i, (title, score, dur, reasons, tr) in enumerate(CLIPS):
        cid = f"clip_{now:%Y%m%d}_{i+1:02d}"
        d = OUT / "clips" / cid
        d.mkdir()
        cover(d / "cover.jpg", title, i, score)
        t0 = 1200 + i * 1500 + rnd.randint(0, 300)
        meta = {"id": cid, "stream_url": "https://www.twitch.tv/demo_streamer", "streamer": "DemoStreamer",
                "stream_title": "DEMO: ranked grind, chat picks the loadout", "t_start": t0, "t_end": t0 + dur,
                "duration": dur, "created_at": (now - timedelta(minutes=(len(CLIPS) - i) * 22)).isoformat(timespec="seconds"),
                "score": score, "reasons": reasons,
                "signals": {"chat_z": round(2.0 + score / 40, 1), "audio_z": round(1.5 + score / 55, 1), "llm_score": score},
                "transcript": tr, "title": title,
                "caption": f"{title}. Watch it live with @DemoStreamer.",
                "hashtags": ["#shorts", "#livestream", "#gaming", "#clutch"], "status": "ready"}
        (d / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        subprocess.run([FF, "-y", "-v", "error", "-loop", "1", "-framerate", "30", "-i", str(d / "cover.jpg"),
                        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", str(min(dur, 10)),
                        "-vf", "scale=1080:1920,zoompan=z='1+0.0006*on':d=1:s=1080x1920:fps=30,format=yuv420p",
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "30", "-c:a", "aac", "-b:a", "48k",
                        "-shortest", str(d / "clip.mp4")], check=True)
    # signals: 1 sample / 5 s for the last 20 minutes, baked with 5 spikes
    sig = []
    t_end = now
    n = 240
    spikes = [30, 78, 121, 170, 214]
    for k in range(n):
        ts = t_end - timedelta(seconds=(n - k) * 5)
        s = sum(math.exp(-((k - sp) ** 2) / 14) for sp in spikes)
        chat = 14 + 6 * rnd.random() + 70 * s
        audio = -34 + 4 * rnd.random() + 22 * s
        sig.append({"ts": ts.isoformat(timespec="seconds"), "t": round((ts - started).total_seconds(), 1),
                    "chat_rate": round(chat, 1), "chat_z": round((chat - 17) / 6, 2),
                    "audio_db": round(audio, 1), "audio_z": round((audio + 33) / 3.2, 2)})
    (OUT / "signals.jsonl").write_text("\n".join(json.dumps(x) for x in sig) + "\n", encoding="utf-8")
    ev = [{"ts": started.isoformat(timespec="seconds"), "type": "info", "msg": "Watching twitch.tv/demo_streamer", "data": {}}]
    for k, sp in enumerate(spikes):
        ts = t_end - timedelta(seconds=(n - sp) * 5)
        z = round(2.8 + k * 0.4, 1)
        ev += [{"ts": ts.isoformat(timespec="seconds"), "type": "candidate", "msg": f"Chat spike {z}x baseline", "data": {"chat_z": z}},
               {"ts": (ts + timedelta(seconds=14)).isoformat(timespec="seconds"), "type": "judged", "msg": f"AI judge: {CLIPS[k][1]}/100 - {CLIPS[k][3][-1].replace('AI judge: ', '')}", "data": {"score": CLIPS[k][1]}},
               {"ts": (ts + timedelta(seconds=55)).isoformat(timespec="seconds"), "type": "cut", "msg": f"Clip cut in {38 + k * 3} s", "data": {}}]
    (OUT / "events.jsonl").write_text("\n".join(json.dumps(x) for x in ev) + "\n", encoding="utf-8")
    state = {"stream_url": "https://www.twitch.tv/demo_streamer", "streamer": "DemoStreamer",
             "stream_title": "DEMO: ranked grind, chat picks the loadout", "live": True,
             "started_at": started.isoformat(timespec="seconds"), "captured_seconds": int((now - started).total_seconds()),
             "chat_messages": 18420, "candidates_seen": 19, "clips_ready": len(CLIPS),
             "last_heartbeat": now.isoformat(timespec="seconds"),
             "current_signals": {"chat_rate": sig[-1]["chat_rate"], "chat_z": sig[-1]["chat_z"], "audio_db": sig[-1]["audio_db"], "audio_z": sig[-1]["audio_z"]},
             "last_transcript_line": "okay chat, vote now, we are running the shotgun again"}
    (OUT / "state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")
    (OUT / "commands.jsonl").write_text("", encoding="utf-8")
    print("fixtures written to", OUT)


if __name__ == "__main__":
    make()
