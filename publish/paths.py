"""Data locations. Fixture mode (CLIPPER_FIXTURES=1) points everything at studio/fixtures/data."""
import os
from pathlib import Path

FIXTURES = os.environ.get("CLIPPER_FIXTURES", "").lower() in ("1", "true", "yes")
REPO = Path(__file__).resolve().parent.parent
if FIXTURES:
    ROOT = Path(os.environ.get("CLIPPER_ROOT", REPO / "studio" / "fixtures" / "data"))
    OUT = ROOT / "out"
else:
    ROOT = Path(os.environ.get("CLIPPER_ROOT", r"C:\clipper-data"))
    OUT = Path(os.environ.get("CLIPPER_OUT", ROOT / "out"))

CLIPS = OUT / "clips"
STATE = OUT / "state.json"
EVENTS = OUT / "events.jsonl"
SIGNALS = OUT / "signals.jsonl"
COMMANDS = OUT / "commands.jsonl"
QUEUE = OUT / "queue.json"
LEDGER = OUT / "ledger.jsonl"
BREAKER = OUT / "breaker.json"
SETTINGS = OUT / "settings.json"
DECISIONS = OUT / "decisions.json"   # studio-owned approve/reject/edit per clip (engine files stay read-only)
STOP = ROOT / "STOP"
SECRETS = Path.home() / ".secrets"
