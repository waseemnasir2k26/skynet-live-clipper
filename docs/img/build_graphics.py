"""Rebuild the README graphics in docs/img from docs/img/src/graphics.html (headless Chrome via Playwright).

  python docs/img/build_graphics.py

Dashboard screenshots + the GIF come from a demo Studio instance (synthetic data):
  CLIPPER_FIXTURE_DIR=/tmp/fx python studio/fixtures/make_fixtures.py
  CLIPPER_FIXTURES=1 CLIPPER_ROOT=/tmp/fx STUDIO_PORT=8792 python studio/server.py
"""
from pathlib import Path
from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
OUT = HERE
SHOTS = {"hero": "social-preview.png", "arch": "architecture.png", "score": "how-a-moment-is-scored.png",
         "modes": "full-vs-light-mode.png", "frame": "sample-clip-layout.png"}

with sync_playwright() as p:
    b = p.chromium.launch(channel="chrome", headless=True)
    pg = b.new_page(viewport={"width": 1400, "height": 1000})
    pg.goto((HERE / "src" / "graphics.html").as_uri())
    pg.evaluate("document.fonts.ready")
    pg.wait_for_timeout(800)
    for el, name in SHOTS.items():
        pg.locator("#" + el).screenshot(path=str(OUT / name))
        print("wrote", name)
    b.close()
