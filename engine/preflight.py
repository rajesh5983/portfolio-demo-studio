"""Pre-flight check before running engine/demo_bot.py.

Confirms the Gradio UI, Qdrant dashboard, and OBS WebSocket are all
reachable before the demo bot starts driving the UI and recording.

Usage:
    python engine/preflight.py
"""

import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

sys.stdout.reconfigure(encoding="utf-8")

load_dotenv()

from obs_controller import OBSController

REPO_ROOT = Path(__file__).resolve().parent.parent

GRADIO_URL = os.getenv("GRADIO_URL", "http://localhost:7860")
QDRANT_DASHBOARD_URL = os.getenv("QDRANT_DASHBOARD_URL", "http://localhost:6333/dashboard")
PHOENIX_URL = os.getenv("PHOENIX_URL", "http://localhost:6006")
RAGAS_HTML_PATH = REPO_ROOT / "projects" / "workershield" / "ragas_results.html"


def check_url(url: str) -> bool:
    try:
        response = httpx.get(url, timeout=5.0, follow_redirects=True)
        return response.status_code == 200
    except httpx.HTTPError as exc:
        print(f"  Error reaching {url}: {exc}")
        return False


def check_obs() -> bool:
    obs = OBSController()
    try:
        obs.connect()
        obs.get_version()
        return True
    except Exception as exc:
        print(f"  Error connecting to OBS at {obs.host}:{obs.port}: {exc}")
        return False
    finally:
        obs.disconnect()


def main() -> None:
    gradio_ok = check_url(GRADIO_URL)
    qdrant_ok = check_url(QDRANT_DASHBOARD_URL)
    obs_ok = check_obs()
    phoenix_ok = check_url(PHOENIX_URL)
    ragas_html_ok = RAGAS_HTML_PATH.exists()

    print()
    print(f"{'✅' if gradio_ok else '❌'} Gradio UI reachable at {GRADIO_URL}")
    print(f"{'✅' if qdrant_ok else '❌'} Qdrant dashboard reachable at {QDRANT_DASHBOARD_URL}")
    print(f"{'✅' if obs_ok else '❌'} OBS WebSocket connected")
    print(f"{'✅' if phoenix_ok else '⚠️ '} Phoenix reachable at {PHOENIX_URL} (optional)")
    if ragas_html_ok:
        print(f"✅ RAGAS results HTML found at {RAGAS_HTML_PATH}")
    else:
        print("❌ RAGAS results HTML missing — run will skip segment 07")
    print()

    if gradio_ok and qdrant_ok and obs_ok:
        print("READY TO RECORD")
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
