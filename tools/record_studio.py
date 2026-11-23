"""Record the Studio dashboard to mp4 (1920x1080, 30 fps, NVENC) with a scripted click/scroll sequence.

  python tools/record_studio.py --url http://127.0.0.1:8790/ --seconds 20 --out sample.mp4 [--script steps.json]

Capture = Playwright (system Chrome, headless) video at 1920x1080, then ffmpeg re-encode to 30 fps h264_nvenc
(falls back to libx264 if NVENC is missing). One recording at a time; nothing heavy.
Script = JSON list of steps, each executed in order (timeline is padded/trimmed to --seconds):
  {"wait": 2}  {"click": "nav a[data-v=clips]"}  {"scroll": 600}  {"goto": "#queue"}
  {"type": ["#watchUrl", "https://..."]}  {"key": "Escape"}  {"hover": "selector"}
"""
import argparse, json, shutil, subprocess, sys, tempfile, time
from pathlib import Path

DEFAULT_STEPS = [
    {"wait": 3}, {"click": "nav a[data-v=clips]"}, {"wait": 2}, {"scroll": 500}, {"wait": 1.5}, {"scroll": -500},
    {"click": ".clip"}, {"wait": 3}, {"key": "Escape"}, {"click": "nav a[data-v=queue]"}, {"wait": 2.5},
    {"click": "nav a[data-v=conn]"}, {"wait": 2}, {"click": "nav a[data-v=live]"}, {"wait": 4},
]


def ffmpeg():
    return shutil.which("ffmpeg") or r"C:\Users\info\ffmpeg-nvenc\bin\ffmpeg.exe"


def run_steps(pg, steps, seconds):
    t0 = time.time()
    for st in steps:
        if time.time() - t0 >= seconds:
            break
        if "wait" in st:
            pg.wait_for_timeout(int(st["wait"] * 1000))
        elif "click" in st:
            try:
                pg.hover(st["click"], timeout=3000); pg.wait_for_timeout(250)
                pg.click(st["click"], timeout=3000)
            except Exception as e:
                print("step failed:", st, e, file=sys.stderr)
        elif "hover" in st:
            pg.hover(st["hover"])
        elif "scroll" in st:
            pg.mouse.wheel(0, st["scroll"]); pg.wait_for_timeout(600)
        elif "goto" in st:
            pg.evaluate("h => location.hash = h", st["goto"].lstrip("#")); pg.wait_for_timeout(500)
        elif "type" in st:
            pg.fill(st["type"][0], st["type"][1])
        elif "key" in st:
            pg.keyboard.press(st["key"])
    left = seconds - (time.time() - t0)
    if left > 0:
        pg.wait_for_timeout(int(left * 1000))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8790/")
    ap.add_argument("--seconds", type=float, default=20)
    ap.add_argument("--out", default="studio-recording.mp4")
    ap.add_argument("--script", help="JSON file with steps (default: tour of every section)")
    a = ap.parse_args()
    steps = json.loads(Path(a.script).read_text(encoding="utf-8")) if a.script else DEFAULT_STEPS
    from playwright.sync_api import sync_playwright
    tmp = Path(tempfile.mkdtemp(prefix="rec_"))
    with sync_playwright() as p:
        b = p.chromium.launch(channel="chrome", headless=True)
        ctx = b.new_context(viewport={"width": 1920, "height": 1080},
                            record_video_dir=str(tmp), record_video_size={"width": 1920, "height": 1080})
        pg = ctx.new_page()
        pg.goto(a.url); pg.wait_for_timeout(1200)
        run_steps(pg, steps, a.seconds)
        ctx.close(); b.close()
    webm = next(tmp.glob("*.webm"))
    out = Path(a.out).resolve(); out.parent.mkdir(parents=True, exist_ok=True)
    base = [ffmpeg(), "-y", "-v", "error", "-i", str(webm), "-t", str(a.seconds), "-vf", "fps=30,scale=1920:1080:flags=lanczos,format=yuv420p"]
    r = subprocess.run(base + ["-c:v", "h264_nvenc", "-preset", "p5", "-cq", "21", "-b:v", "0", "-movflags", "+faststart", str(out)])
    if r.returncode:
        print("NVENC failed, falling back to libx264", file=sys.stderr)
        subprocess.run(base + ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-movflags", "+faststart", str(out)], check=True)
    shutil.rmtree(tmp, ignore_errors=True)
    print(out)


if __name__ == "__main__":
    main()
