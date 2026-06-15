"""Single entry point for the automated demo pipeline.

Runs pre-flight checks (Gradio UI, Qdrant dashboard, OBS WebSocket) and,
if they all pass, launches engine/demo_bot.py to record the demo.

Usage:
    python engine/run_demo.py --project workershield
"""

import asyncio
import subprocess
import sys
import time
from pathlib import Path

import click
import httpx
from playwright.async_api import async_playwright

from launch_chrome import CHROME_CDP_PORT
from obs_controller import ensure_chrome_foreground
from preflight import GRADIO_URL, QDRANT_DASHBOARD_URL, check_obs, check_url

REPO_ROOT = Path(__file__).resolve().parent.parent


def ensure_chrome_cdp() -> None:
    cdp_url = f"http://localhost:{CHROME_CDP_PORT}/json"
    try:
        httpx.get(cdp_url, timeout=3.0).raise_for_status()
        print("Chrome already running with CDP — skipping launch")
        return
    except httpx.HTTPError:
        pass

    print("Chrome not running with CDP — launching...")
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "engine" / "launch_chrome.py")],
        cwd=str(REPO_ROOT),
    )
    if result.returncode != 0:
        print("CHROME LAUNCH FAILED")
        sys.exit(1)

    time.sleep(4)


def run_preflight() -> bool:
    checks = [
        ("Gradio UI reachable", lambda: check_url(GRADIO_URL), GRADIO_URL),
        ("Qdrant dashboard reachable", lambda: check_url(QDRANT_DASHBOARD_URL), QDRANT_DASHBOARD_URL),
        ("OBS WebSocket connected", check_obs, None),
    ]

    all_ok = True
    for name, check, url in checks:
        ok = check()
        label = f"{name} at {url}" if url else name
        print(f"{'✅' if ok else '❌'} {label}")
        if not ok:
            all_ok = False

    return all_ok


async def maximize_chrome_window() -> None:
    """Bring Chrome to the foreground and resize its window to fill the screen."""
    ensure_chrome_foreground()

    cdp_url = f"http://localhost:{CHROME_CDP_PORT}"
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(cdp_url)
        for context in browser.contexts:
            for page in context.pages:
                await page.evaluate(
                    "window.moveTo(0, 0); window.resizeTo(screen.width, screen.height);"
                )
                return


async def run_demo_bot(project: str) -> int:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(REPO_ROOT / "engine" / "demo_bot.py"),
        "--project",
        project,
        cwd=str(REPO_ROOT),
    )
    return await process.wait()


async def run(project: str) -> int:
    await maximize_chrome_window()
    return await run_demo_bot(project)


@click.command()
@click.option("--project", default="workershield", help="Project slug under projects/, e.g. workershield")
def main(project: str) -> None:
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

    ensure_chrome_cdp()
    print()

    if not run_preflight():
        print()
        print("PREFLIGHT FAILED — fix the issue(s) above before running the demo bot")
        sys.exit(1)

    print("PREFLIGHT PASSED — starting demo bot")
    sys.exit(asyncio.run(run(project)))


if __name__ == "__main__":
    main()
