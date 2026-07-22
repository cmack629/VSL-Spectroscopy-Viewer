"""
VSL Scan — Actuator + Power Scan launcher.

Entry point for the merged app. Run from the repo root:

    python run.py            →  http://localhost:5050

Equivalent to `python -m server.scanner`. Auto-detects the attached actuator
controller (BPC301/DRV517 piezo or Newport SMC100/LTA-HS), initialises the
PM400 power meter, then serves the single Scan page.
"""

import os

from server import actuators
from server import detectors
from server.scanner import create_app

PORT = 5050   # 5000 is taken by macOS AirPlay Receiver by default

# Set FLASK_DEBUG=1 for Werkzeug's auto-reloader (picks up code/template
# changes without a restart) — off by default so a normal run behaves the
# same as always; the dev Docker Compose setup turns it on.
DEBUG = os.environ.get("FLASK_DEBUG", "").lower() in ("1", "true", "yes")

# With the reloader on, Werkzeug re-execs this whole script in a child
# process and only actually serves from there — the first pass is a
# throwaway supervisor. Without this guard, detect() would open/probe every
# serial and USB instrument twice on each start.
IS_RELOADER_SUPERVISOR = DEBUG and os.environ.get("WERKZEUG_RUN_MAIN") != "true"


def main() -> None:
    if not IS_RELOADER_SUPERVISOR:
        actuators.manager.detect()   # probe piezo + SMC100, pick the connected one
        detectors.manager.detect()   # probe PM400 + HR4000 + Avantes, pick connected
    create_app().run(host="0.0.0.0", port=PORT, threaded=True, debug=DEBUG)


if __name__ == "__main__":
    main()
