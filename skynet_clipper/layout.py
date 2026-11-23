"""Facecam detection (once per stream) + 9:16 layout geometry."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .util import BELOW_NORMAL, FFMPEG, log


@dataclass
class Box:
    x: int
    y: int
    w: int
    h: int

    def as_list(self):
        return [self.x, self.y, self.w, self.h]


def grab_frame(path: str, t: float, w: int = 1920, h: int = 1080) -> np.ndarray | None:
    p = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", path, "-frames:v", "1",
                        "-vf", f"scale={w}:{h}", "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=BELOW_NORMAL, timeout=60)
    if len(p.stdout) != w * h * 3:
        return None
    return np.frombuffer(p.stdout, np.uint8).reshape(h, w, 3)


YUNET = Path(__file__).parent / "assets" / "models" / "face_detection_yunet_2023mar.onnx"


def _faces(img: np.ndarray) -> list[Box]:
    """OpenCV YuNet face detector (works on OpenCV 4.8+ and 5.x)."""
    import cv2
    H, W = img.shape[:2]
    det = cv2.FaceDetectorYN.create(str(YUNET), "", (W, H), 0.7, 0.3, 50)
    _, faces = det.detect(img)
    if faces is None:
        return []
    return [Box(int(f[0]), int(f[1]), int(f[2]), int(f[3])) for f in faces]


def detect_face(frames: list[np.ndarray]) -> Box | None:
    """Most consistent face across frames = the facecam (gameplay NPC faces move around)."""
    hits: list[Box] = []
    for f in frames:
        hits.extend(b for b in _faces(f) if 40 <= b.w <= 600)
    if not hits:
        return None
    best, best_n = None, 0
    for b in hits:
        cx, cy = b.x + b.w / 2, b.y + b.h / 2
        group = [o for o in hits if abs(o.x + o.w / 2 - cx) < b.w * 0.8 and abs(o.y + o.h / 2 - cy) < b.h * 0.8]
        if len(group) > best_n:
            best_n, best = len(group), group
    if best_n < max(2, len(frames) // 4):
        return None
    med = lambda v: int(np.median(v))  # noqa: E731
    box = Box(med([b.x for b in best]), med([b.y for b in best]), med([b.w for b in best]), med([b.h for b in best]))
    log(f"facecam face box {box.as_list()} found in {best_n}/{len(frames)} frames")
    return box


@dataclass
class Layout:
    kind: str                 # stack | center
    cam: Box | None = None    # source crop for the cam panel
    game: Box | None = None   # source crop for the gameplay panel
    blur: Box | None = None   # region inside game crop (game-crop coords) to blur (duplicate facecam)
    cam_h: int = 720
    game_h: int = 1200
    face: Box | None = None


def plan_layout(face: Box | None, src_w: int = 1920, src_h: int = 1080, out_w: int = 1080, out_h: int = 1920,
                cam_h: int = 720) -> Layout:
    if face is None:
        return Layout("center")
    game_h = out_h - cam_h
    # cam panel: face ~38% of panel height, face in the upper-middle
    ch = int(min(src_h, max(face.h * 2.7, 240)))
    cw = int(ch * out_w / cam_h)
    if cw > src_w:
        cw, ch = src_w, int(src_w * cam_h / out_w)
    fcx, fcy = face.x + face.w / 2, face.y + face.h / 2
    cx = int(np.clip(fcx - cw / 2, 0, src_w - cw))
    cy = int(np.clip(fcy - ch * 0.42, 0, src_h - ch))
    cam = Box(cx & ~1, cy & ~1, cw & ~1, ch & ~1)
    # gameplay panel: full height, centred
    gw = int(src_h * out_w / game_h) & ~1
    gx = (src_w - gw) // 2 & ~1
    game = Box(gx, 0, gw, src_h)
    # facecam person region in source -> blur where it falls inside the gameplay crop
    px0, px1 = face.x - face.w * 1.6, face.x + face.w * 2.6
    py0, py1 = face.y - face.h * 0.9, src_h
    ix0, ix1 = max(px0, gx), min(px1, gx + gw)
    iy0, iy1 = max(py0, 0), min(py1, src_h)
    blur = None
    if ix1 - ix0 > 8 and iy1 - iy0 > 8:
        blur = Box(int(ix0 - gx) & ~1, int(iy0) & ~1, int(ix1 - ix0) & ~1, int(iy1 - iy0) & ~1)
    return Layout("stack", cam=cam, game=game, blur=blur, cam_h=cam_h, game_h=game_h, face=face)


def filtergraph(lay: Layout, fps: int, ass: str | None, out_w: int = 1080, out_h: int = 1920,
                badge: bool = True, start: float = 0.0) -> str:
    """Input 0 = video, input 1 = badge.png (RGBA overlay with credit + watermark card)."""
    tail = (",[1:v]overlay=0:0:format=auto" if badge else "") + ",format=yuv420p" +         (f",ass=filename={ass}:fontsdir=fonts" if ass else "")
    tail = tail.replace(",[1:v]overlay", "[pre];[pre][1:v]overlay", 1)
    if lay.kind == "stack":
        c, g = lay.cam, lay.game
        parts = [f"[0:v]trim=start={start:.3f},setpts=PTS-STARTPTS,fps={fps},split=2[a][b]",
                 f"[a]crop={c.w}:{c.h}:{c.x}:{c.y},scale={out_w}:{lay.cam_h}:flags=lanczos,unsharp=5:5:0.5[cam]"]
        if lay.blur:
            bl = lay.blur
            parts.append(f"[b]crop={g.w}:{g.h}:{g.x}:{g.y},split=2[g1][g2]")
            parts.append(f"[g2]crop={bl.w}:{bl.h}:{bl.x}:{bl.y},boxblur=28:3,eq=brightness=-0.06[bl]")
            parts.append(f"[g1][bl]overlay={bl.x}:{bl.y},scale={out_w}:{lay.game_h}:flags=lanczos[game]")
        else:
            parts.append(f"[b]crop={g.w}:{g.h}:{g.x}:{g.y},scale={out_w}:{lay.game_h}:flags=lanczos[game]")
        parts.append(f"[cam][game]vstack=inputs=2,drawbox=x=0:y={lay.cam_h - 3}:w={out_w}:h=6:"
                     f"color=0xFFD400@1:t=fill,setsar=1{tail}[v]")
        return ";".join(parts)
    # center: blurred fill + full 16:9 frame in the middle, zoomed a bit
    return (f"[0:v]trim=start={start:.3f},setpts=PTS-STARTPTS,fps={fps},split=2[a][b];"
            f"[a]scale=-2:{out_h},crop={out_w}:{out_h},boxblur=30:4,eq=brightness=-0.15[bg];"
            f"[b]scale={out_w + 360}:-2:flags=lanczos,crop={out_w}:ih[fg];"
            f"[bg][fg]overlay=0:(H-h)/2,setsar=1{tail}[v]")


def text_zones(lay: Layout, out_h: int = 1920) -> dict:
    """y positions (centre) for hook title, captions, credit, watermark."""
    if lay.kind == "stack":
        return {"hook": lay.cam_h + 4, "caption": lay.cam_h + int(lay.game_h * 0.62), "credit": 58,
                "watermark": out_h - 50}
    return {"hook": 560, "caption": 1360, "credit": 90, "watermark": out_h - 60}


def badge_rect(lay: Layout, out_w: int = 1080, out_h: int = 1920) -> tuple[int, int, int, int]:
    """Output-pixel rect of the branded corner card. In the stack layout it sits exactly over the
    blurred duplicate facecam, so the blur reads as a designed glass card, not a smudge."""
    if lay.kind == "stack" and lay.blur:
        s = lay.game_h / lay.game.h
        x1 = int(min(out_w * 0.42, lay.blur.x * s + lay.blur.w * s))
        y0 = int(lay.cam_h + lay.blur.y * s)
        return 0, max(y0, out_h - 460), max(x1, 300), out_h
    return 0, out_h - 190, 380, out_h


def make_badge(lay: Layout, path, credit: str, watermark: str, font_path: str, out_w: int = 1080, out_h: int = 1920):
    from PIL import Image, ImageDraw, ImageFont
    im = Image.new("RGBA", (out_w, out_h), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    x0, y0, x1, y1 = badge_rect(lay, out_w, out_h)
    d.rounded_rectangle([x0 - 40, y0, x1, y1 + 40], radius=34, fill=(12, 12, 16, 150), outline=(255, 212, 0, 255),
                        width=5)
    f_live = ImageFont.truetype(font_path, 30)
    f_cred = ImageFont.truetype(font_path, 40 if (x1 - x0) > 330 else 34)
    f_mark = ImageFont.truetype(font_path, 24)
    px, py = x0 + 28, y0 + 30
    d.rounded_rectangle([px, py, px + 118, py + 46], radius=10, fill=(230, 30, 40, 255))
    d.ellipse([px + 12, py + 15, px + 28, py + 31], fill=(255, 255, 255, 255))
    d.text((px + 36, py + 6), "LIVE", font=f_live, fill=(255, 255, 255, 255))
    if credit:
        d.text((px, py + 66), credit, font=f_cred, fill=(255, 255, 255, 255), stroke_width=2, stroke_fill=(0, 0, 0, 255))
    if watermark:
        d.text((px, py + 122), watermark, font=f_mark, fill=(255, 255, 255, 190))
    im.save(path)
    return path
