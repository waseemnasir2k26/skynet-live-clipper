"""Cut + design a 9:16 clip: layout, word-timed captions, hook title, credit, watermark, loudness, cover."""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from .asr import Word
from .layout import Layout, filtergraph, make_badge, text_zones
from .util import BELOW_NORMAL, FFMPEG, run_ff

FONT_DIR = Path(__file__).parent / "assets" / "fonts"
FONT = "Montserrat ExtraBold"

ASS_HEAD = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{f},86,&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,-1,0,0,0,100,100,1,0,1,8,3,5,40,40,0,1
Style: Hook,{f},64,&H00111111,&H00111111,&H0000D4FF,&H0000D4FF,-1,0,0,0,100,100,0,0,3,22,0,5,60,60,0,1
Style: Small,{f},34,&H00FFFFFF,&H00FFFFFF,&H00000000,&H78000000,-1,0,0,0,100,100,1,0,3,10,0,5,20,20,0,1
Style: Mark,{f},30,&H50FFFFFF,&H50FFFFFF,&H64000000,&H00000000,-1,0,0,0,100,100,1,0,1,2,0,5,20,20,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
HILITE = r"{\c&H00D4FF&\fscx108\fscy108}"
NORMAL = r"{\c&HFFFFFF&\fscx100\fscy100}"


def ts(t: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


PROFANITY = re.compile(r"(chod|chut|madar|bhosd|bosd|gaand|gand(u|oo)|lund|lauda|loda|randi|bsdk|harami|"
                       r"kamina|fuck|shit|bitch|^mc$|^bc$|bahanch|behanch|bahench|behench|bahansh|behansh)", re.I)


def censor(w: str) -> str:
    """Brand-safe captions: keep the first letter, star the rest of a slur / gaali."""
    if PROFANITY.search(w):
        return w[0] + "*" * max(2, len(w) - 1)
    return w


def clean_word(w: str) -> str:
    w = re.sub(r"[\"“”‘’`,;:()\[\]{}\\]", "", w)
    w = w.replace("।", "").strip()
    return censor(w).upper()


def chunk_words(words: list[Word], max_words: int = 3, max_chars: int = 16, gap: float = 0.5) -> list[list[Word]]:
    chunks: list[list[Word]] = []
    cur: list[Word] = []
    for w in words:
        if not clean_word(w.roman):
            continue
        if cur and (len(cur) >= max_words or w.t0 - cur[-1].t1 > gap
                    or sum(len(clean_word(x.roman)) + 1 for x in cur) + len(clean_word(w.roman)) > max_chars):
            chunks.append(cur)
            cur = []
        cur.append(w)
    if cur:
        chunks.append(cur)
    return chunks


def ass_escape(s: str) -> str:
    return s.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def wrap_title(title: str, max_chars: int = 24) -> str:
    words, lines, cur = title.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > max_chars:
            lines.append(cur)
            cur = w
        else:
            cur = (cur + " " + w).strip()
    if cur:
        lines.append(cur)
    return r"\N".join(lines[:3])  # never silently drop words (a 3rd line beats a cut-off hook)


def build_ass(words: list[Word], t0: float, dur: float, title: str, credit: str, watermark: str,
              zones: dict, hook_s: float) -> str:
    ev = []
    hook_end = hook_s
    if title:
        ev.append(f"Dialogue: 5,{ts(0)},{ts(hook_end)},Hook,,0,0,0,,"
                  f"{{\\pos(540,{zones['hook']})\\fad(0,200)\\t(0,180,\\fscx104\\fscy104)\\t(180,320,\\fscx100\\fscy100)}}"
                  f"{wrap_title(ass_escape(title.upper()))}")
    cy = zones["caption"]
    for ch in chunk_words([w for w in words if w.t1 > t0 and w.t0 < t0 + dur]):
        c0 = max(0.0, ch[0].t0 - t0)
        c1 = min(dur, ch[-1].t1 - t0 + 0.15)
        texts = [ass_escape(clean_word(w.roman)) for w in ch]
        for i, w in enumerate(ch):
            a = c0 if i == 0 else max(c0, w.t0 - t0)
            b = c1 if i == len(ch) - 1 else max(a + 0.05, ch[i + 1].t0 - t0)
            if b <= a:
                continue
            body = " ".join((HILITE + tx + NORMAL) if j == i else tx for j, tx in enumerate(texts))
            ev.append(f"Dialogue: 2,{ts(a)},{ts(b)},Cap,,0,0,0,,{{\\pos(540,{cy})}}{body}")
    return ASS_HEAD.format(f=FONT) + "\n".join(ev) + "\n"


def concat_list(segs: list[tuple[str, float, float]], path: Path) -> float:
    path.write_text("".join(f"file '{Path(p).as_posix()}'\n" for p, _, _ in segs), encoding="utf-8")
    return segs[0][1]


def render_clip(work: Path, segs: list[tuple[str, float, float]], t0: float, t1: float, lay: Layout,
                words: list[Word], title: str, credit: str, watermark: str, cfg: dict, encoder: str) -> dict:
    work.mkdir(parents=True, exist_ok=True)
    fdir = work / "fonts"
    fdir.mkdir(exist_ok=True)
    for f in FONT_DIR.glob("*.ttf"):
        if not (fdir / f.name).exists():
            shutil.copy(f, fdir / f.name)
    base = concat_list(segs, work / "list.txt")
    dur = round(t1 - t0, 2)
    zones = text_zones(lay)
    (work / "cap.ass").write_text(build_ass(words, t0, dur, title, credit, watermark, zones,
                                            cfg["clips"]["hook_seconds"]), encoding="utf-8")
    rc = cfg["render"]
    fps = int(rc.get("fps", 60))
    make_badge(lay, work / "badge.png", credit, watermark, str(FONT_DIR / "Montserrat-ExtraBold.ttf"))
    fg = filtergraph(lay, fps, "cap.ass", start=t0 - base)  # trim in-graph: input -ss on concat breaks VP9
    venc = (["-c:v", "h264_nvenc"] + rc["nvenc_args"]) if encoder == "h264_nvenc" else (["-c:v", "libx264"] + rc["x264_args"])
    # CUDA decode only for H.264/HEVC TS segments; the VP9 cuvid path crashed on VOD segments ("Internal bug")
    hw = ["-hwaccel", "cuda"] if encoder == "h264_nvenc" and segs[0][0].endswith(".ts") else []
    if encoder == "nvenc_swdec":
        venc = ["-c:v", "h264_nvenc"] + rc["nvenc_args"]
    cmd = [FFMPEG, "-hide_banner", "-y"] + hw + ["-f", "concat", "-safe", "0", 
                                                 "-i", "list.txt", "-i", "badge.png", "-t", f"{dur:.3f}", "-filter_complex", fg,
                                                 "-map", "[v]", "-map", "0:a:0"] + venc + [
        "-af", f"atrim=start={t0 - base:.3f},asetpts=PTS-STARTPTS,loudnorm=I={rc['loudness_lufs']}:TP=-1.5:LRA=11,aresample=48000", "-c:a", "aac", "-b:a", "160k",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", "clip.mp4"]
    try:
        run_ff(cmd, cwd=str(work))
    except RuntimeError:
        nxt = {"h264_nvenc": "nvenc_swdec", "nvenc_swdec": "libx264"}.get(encoder)
        if not nxt:
            raise
        return render_clip(work, segs, t0, t1, lay, words, title, credit, watermark, cfg, nxt)
    return {"path": work / "clip.mp4", "encoder": encoder, "duration": dur, "filtergraph": fg, "base": base}


def render_cover(work: Path, t_rel: float, base_off: float, lay: Layout, title: str, credit: str) -> Path:
    """Best frame without captions + big title, credit."""
    from PIL import Image, ImageDraw, ImageFont
    fg = filtergraph(lay, 30, None, start=base_off + t_rel)
    run_ff([FFMPEG, "-hide_banner", "-y", "-f", "concat", "-safe", "0", "-i", "list.txt",
            "-i", "badge.png", "-filter_complex", fg, "-map", "[v]", "-frames:v", "1", "cover_raw.png"], cwd=str(work))
    im = Image.open(work / "cover_raw.png").convert("RGB")
    d = ImageDraw.Draw(im)
    font_path = str(FONT_DIR / "Montserrat-ExtraBold.ttf").replace("\\", "/")
    text = (title or "").upper()
    zones = text_zones(lay)
    size = 92
    lines: list[str] = []
    while size >= 48:
        font = ImageFont.truetype(font_path, size)  # let it throw on a bad path
        lines, cur = [], ""
        for w in text.split():
            cand = (cur + " " + w).strip()
            if d.textlength(cand, font=font) > 960 and cur:
                lines.append(cur)
                cur = w
            else:
                cur = cand
        if cur:
            lines.append(cur)
        if len(lines) <= 3:
            break
        size -= 6
    lh = int(size * 1.15)
    total = lh * len(lines)
    y = zones["hook"] - total // 2
    for ln in lines:
        tw = d.textlength(ln, font=font)
        x = (1080 - tw) / 2
        d.rounded_rectangle([x - 26, y - 10, x + tw + 26, y + lh - 4], radius=18, fill=(255, 212, 0))
        d.text((x, y), ln, font=font, fill=(17, 17, 17))
        y += lh
    out = work / "cover.jpg"
    im.save(out, quality=90)
    return out


def measure_loudness(path: Path) -> float | None:
    p = subprocess.run([FFMPEG, "-hide_banner", "-nostats", "-i", str(path), "-af", "ebur128=framelog=quiet", "-f", "null", "-"],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=BELOW_NORMAL, timeout=300)
    m = re.findall(r"I:\s+(-?[\d.]+) LUFS", p.stderr.decode("utf-8", "replace"))
    return float(m[-1]) if m else None
