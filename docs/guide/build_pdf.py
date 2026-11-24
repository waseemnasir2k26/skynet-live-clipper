"""Build docs/Live-Stream-Clipping-Starter-Guide.pdf from docs/guide/guide.html with headless Chrome (Playwright).

  pip install playwright   (uses the system Chrome: channel="chrome")
  python docs/guide/build_pdf.py
"""
from pathlib import Path
from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
OUT = HERE.parent / "Live-Stream-Clipping-Starter-Guide.pdf"

with sync_playwright() as p:
    b = p.chromium.launch(channel="chrome", headless=True)
    pg = b.new_page()
    pg.goto((HERE / "guide.html").as_uri())
    pg.evaluate("document.fonts.ready")
    pg.wait_for_timeout(800)
    pg.pdf(path=str(OUT), format="A4", print_background=True, margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
           prefer_css_page_size=True)
    b.close()
print("wrote", OUT)
