"""Launch Chrome with a dedicated automation profile and CDP debugging enabled.

Opens the WorkerShield, Qdrant, Phoenix, GitHub, and RAGAS results tabs needed
by engine/demo_bot.py, then confirms the Chrome DevTools Protocol (CDP)
endpoint is reachable.

Usage:
    python engine/launch_chrome.py
"""

import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

sys.stdout.reconfigure(encoding="utf-8")

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parent.parent
USER_DATA_DIR = REPO_ROOT / ".chrome-automation-profile"

CHROME_CDP_PORT = int(os.getenv("CHROME_CDP_PORT", "9222"))
GRADIO_URL = os.getenv("GRADIO_URL", "http://localhost:7860")
QDRANT_DASHBOARD_URL = os.getenv("QDRANT_DASHBOARD_URL", "http://localhost:6333/dashboard")
PHOENIX_URL = os.getenv("PHOENIX_URL", "http://localhost:6006")
GITHUB_REPO_URL = "https://github.com/rajesh5983/workershield-v1"

RAGAS_RESULTS_PATH = REPO_ROOT / "projects" / "workershield" / "ragas_results.html"
RAGAS_RESULTS_URL = RAGAS_RESULTS_PATH.resolve().as_uri()

CHROME_CANDIDATES = [
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
]


def find_chrome() -> Path | None:
    for candidate in CHROME_CANDIDATES:
        if candidate.is_file():
            return candidate
    return None


def main() -> None:
    chrome_path = find_chrome()
    if chrome_path is None:
        print("❌ Could not find chrome.exe in any of the expected locations:")
        for candidate in CHROME_CANDIDATES:
            print(f"   {candidate}")
        sys.exit(1)

    urls = [GRADIO_URL, QDRANT_DASHBOARD_URL, PHOENIX_URL, GITHUB_REPO_URL, RAGAS_RESULTS_URL]
    args = [
        str(chrome_path),
        f"--remote-debugging-port={CHROME_CDP_PORT}",
        f"--user-data-dir={USER_DATA_DIR}",
        "--no-first-run",
        "--no-default-browser-check",
        "--start-maximized",
        *urls,
    ]

    try:
        subprocess.Popen(args)
        print(f"✅ Chrome launched with {len(urls)} tabs")
    except OSError as exc:
        print(f"❌ Failed to launch Chrome: {exc}")
        sys.exit(1)

    time.sleep(3)

    cdp_url = f"http://localhost:{CHROME_CDP_PORT}/json"
    try:
        response = httpx.get(cdp_url, timeout=5.0)
        response.raise_for_status()
        print(f"✅ CDP reachable on port {CHROME_CDP_PORT}")
    except httpx.HTTPError as exc:
        print(f"❌ CDP not reachable on port {CHROME_CDP_PORT}: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
