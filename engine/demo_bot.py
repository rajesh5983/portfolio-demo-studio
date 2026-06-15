"""Automated demo recording bot for portfolio projects (Playwright + OBS WebSocket).

Drives the project's Gradio UI, GitHub repo page, Qdrant dashboard, and
Phoenix UI while OBS records each segment, replacing manual screen
recording. Recordings are saved to projects/{project}/assets/recordings/,
named per script.yaml's `recording_file` for each segment, ready for
engine/generate_audio.py and engine/compose_video.py.

Usage:
    python engine/demo_bot.py --project workershield

Requires OBS running with the WebSocket server enabled, and Chrome already
running with remote debugging enabled on CHROME_CDP_PORT, with the
WorkerShield UI (GRADIO_URL), Qdrant dashboard (QDRANT_DASHBOARD_URL),
Phoenix UI (PHOENIX_URL), and the GitHub repo already open in tabs (see
demo/obs_setup_guide.md / engine/launch_chrome.py).
"""

import asyncio
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import click
import httpx
import yaml
from dotenv import load_dotenv
from playwright.async_api import Browser, Page, async_playwright

from obs_controller import OBSController, ensure_chrome_foreground

sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parent.parent

GRADIO_URL = os.getenv("GRADIO_URL", "http://localhost:7860")
QDRANT_DASHBOARD_URL = os.getenv("QDRANT_DASHBOARD_URL", "http://localhost:6333/dashboard")
PHOENIX_URL = os.getenv("PHOENIX_URL", "http://localhost:6006")
CHROME_CDP_PORT = int(os.getenv("CHROME_CDP_PORT", "9222"))
CHROME_CDP_URL = f"http://localhost:{CHROME_CDP_PORT}"

GRADIO_URL_HOST = urlparse(GRADIO_URL).netloc
QDRANT_URL_HOST = urlparse(QDRANT_DASHBOARD_URL).netloc
QDRANT_COLLECTIONS_URL = f"{QDRANT_DASHBOARD_URL}#/collections"
QDRANT_WORKERSHIELD_URL = f"{QDRANT_DASHBOARD_URL}#/collections/workershield"
PHOENIX_URL_HOST = urlparse(PHOENIX_URL).netloc
PHOENIX_TRACES_URL = f"{PHOENIX_URL}/projects"

GITHUB_URL_HOST = "github.com"
GITHUB_REPO_PATH = "/rajesh5983/workershield-v1"
GITHUB_REPO_URL = f"https://github.com{GITHUB_REPO_PATH}"
GITHUB_README_URL = f"{GITHUB_REPO_URL}/blob/dev/README.md"
GITHUB_MCP_ARCHITECTURE_URL = f"{GITHUB_REPO_URL}/blob/dev/docs/MCP_ARCHITECTURE.md"
GITHUB_MCP_SERVER_URL = f"{GITHUB_REPO_URL}/tree/dev/mcp_server"

RAGAS_RESULTS_PATH = REPO_ROOT / "projects" / "workershield" / "ragas_results.html"
RAGAS_RESULTS_URL = RAGAS_RESULTS_PATH.resolve().as_uri()

# Gradio locators -- adjust these to match the labels/text used by the
# running app if they differ.
ANSWER_SELECTOR = "#answer-panel .prose"
CLEAR_BUTTON_NAME = "Clear"

# Phrases shown in the answer panel while the agent is still working --
# seeing these (or anything shorter than MIN_RESPONSE_CHARS) means the
# real response hasn't arrived yet.
LOADING_PHRASES = (
    "searching",
    "retrieving",
)

# A response saying the corpus doesn't cover the question -- treated as
# not-yet-ready so the bot keeps polling rather than recording a fallback.
NOT_READY_PHRASES = (
    "i don't have enough information",
)

# An "out of scope" response means retrying won't produce a different
# answer -- fail the segment immediately instead of polling for the full
# timeout.
OUT_OF_SCOPE_PHRASES = (
    "out of scope",
    "outside the scope",
)

MIN_RESPONSE_CHARS = 50

SAFESHIFT_EXAMPLE = (
    "My FIFO worker has a mental health condition and wants to reduce hours "
    "— what are my obligations under safety law and fair work?"
)
FAIRDESK_EXAMPLE = "What are my obligations when a worker is injured and needs to return to work?"
HEALTHNAV_EXAMPLE = "What psychosocial hazards must I manage under WHS law?"
KILLER_QUERY_EXAMPLE = (
    "How many fatigue-related incidents have we had this year, and "
    "what are our obligations to manage fatigue risk?"
)


@dataclass
class SegmentResult:
    id: int
    label: str
    recording_file: str
    target_duration: float
    actual_duration: float | None
    output_path: Path | None
    success: bool
    note: str | None = None


def log(message: str) -> None:
    timestamp = datetime.now().strftime("%H:%M:%S")
    click.echo(f"[{timestamp}] {message}")


def load_yaml(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def find_page_by_url(browser: Browser, url_host: str) -> Page | None:
    """Find an already-open tab whose URL contains `url_host`."""
    for context in browser.contexts:
        for page in context.pages:
            if url_host in page.url:
                return page
    return None


def check_phoenix_reachable() -> bool:
    try:
        response = httpx.get(PHOENIX_URL, timeout=3.0, follow_redirects=True)
        return response.status_code == 200
    except httpx.HTTPError:
        return False


async def start_segment(obs: OBSController, recordings_dir: Path, segment: dict, tag: str) -> float:
    obs.set_output_path(recordings_dir)
    log(f"[Segment {tag}] Recording started")
    obs.start_recording()
    return time.monotonic()


async def pad_to_duration(start_time: float, duration_seconds: float, tag: str) -> None:
    elapsed = time.monotonic() - start_time
    remaining = duration_seconds - elapsed
    if remaining > 0:
        log(f"[Segment {tag}] Holding for {remaining:.1f}s to reach target duration ({duration_seconds}s)")
        await asyncio.sleep(remaining)


def move_recording(saved_path: str | None, target_path: Path, tag: str) -> None:
    if not saved_path:
        log(f"[Segment {tag}] WARNING: OBS did not report an output path; check {target_path.parent} manually")
        return
    src = Path(saved_path)
    if not src.exists():
        log(f"[Segment {tag}] WARNING: expected recording at {src} but it was not found")
        return
    target_path.parent.mkdir(parents=True, exist_ok=True)
    if target_path.exists():
        target_path.unlink()

    # OBS may still hold the file open for a moment after stop_record() returns.
    for attempt in range(10):
        try:
            src.rename(target_path)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.5)


async def finish_segment(
    obs: OBSController, recordings_dir: Path, segment: dict, start_time: float, tag: str
) -> tuple[float, Path | None]:
    await pad_to_duration(start_time, segment["duration_seconds"], tag)
    actual_duration = time.monotonic() - start_time
    log(f"[Segment {tag}] Recording stopped ({actual_duration:.1f}s)")
    saved_path = obs.stop_recording()
    target_path = recordings_dir / segment["recording_file"]
    move_recording(saved_path, target_path, tag)
    if target_path.exists():
        log(f"[Segment {tag}] Saved -> {target_path}")
        return actual_duration, target_path
    return actual_duration, None


async def scroll_to_top(page: Page) -> None:
    await page.evaluate("window.scrollTo(0, 0)")
    await page.wait_for_timeout(500)


async def smooth_scroll(page: Page, steps: int = 4, pixels: int = 250, delay_ms: int = 1000) -> None:
    for _ in range(steps):
        await page.mouse.wheel(0, pixels)
        await page.wait_for_timeout(delay_ms)


async def scroll_to_text(page: Page, text: str, tag: str) -> None:
    log(f"[Segment {tag}] Scrolling to '{text}'...")
    locator = page.get_by_text(text, exact=False).first
    try:
        await locator.scroll_into_view_if_needed(timeout=5_000)
    except Exception:
        log(f"[Segment {tag}] WARNING: could not find '{text}' on page")


async def click_clear(page: Page, tag: str) -> None:
    log(f"[Segment {tag}] Clicking Clear...")
    await page.get_by_role("button", name=CLEAR_BUTTON_NAME).click()


async def click_sources_accordion(page: Page, tag: str) -> None:
    log(f"[Segment {tag}] Clicking Sources accordion...")
    sources = page.get_by_text("Sources", exact=False).first
    try:
        await sources.click(timeout=5_000)
    except Exception:
        log(f"[Segment {tag}] WARNING: could not click Sources accordion")


async def wait_for_real_response(page: Page, tag: str, timeout: int = 120, interval: int = 2) -> bool:
    """Poll the answer panel until a real, fully-rendered response appears.

    Rejects an empty panel, loading-state text (e.g. "Searching..."), and
    short fragments -- otherwise the loading state gets mistaken for the
    response and the segment stops recording before the real answer arrives.
    An "out of scope" / "outside the scope" response fails the segment
    immediately, since retrying won't produce a different answer.
    """
    answer = page.locator(ANSWER_SELECTOR).first
    log(f"[Segment {tag}] Waiting for response (timeout {timeout}s, polling every {interval}s)...")
    start = time.monotonic()
    last_log = 0.0
    while True:
        elapsed = time.monotonic() - start
        if elapsed >= timeout:
            log(f"[Segment {tag}] FAILED: no real response after {timeout}s")
            return False

        if elapsed - last_log >= 10:
            log(f"[Segment {tag}] Still waiting for response... {int(elapsed)}s elapsed")
            last_log = elapsed

        try:
            if await answer.count() == 0:
                await asyncio.sleep(interval)
                continue

            text = (await answer.inner_text()).strip()
            lowered = text.lower()

            if any(phrase in lowered for phrase in OUT_OF_SCOPE_PHRASES):
                log(f"[Segment {tag}] OUT OF SCOPE response detected — retrying not supported")
                return False

            if not text or len(text) <= MIN_RESPONSE_CHARS:
                await asyncio.sleep(interval)
                continue

            if any(phrase in lowered for phrase in LOADING_PHRASES):
                await asyncio.sleep(interval)
                continue

            if any(phrase in lowered for phrase in NOT_READY_PHRASES):
                await asyncio.sleep(interval)
                continue

            log(f"[Segment {tag}] Response received after {int(elapsed)}s ({len(text)} chars)")
            return True
        except Exception as exc:
            log(f"[Segment {tag}] WARNING: poll error: {exc}")
            await asyncio.sleep(interval)


async def submit_example_query(page: Page, example_text: str, tag: str) -> bool:
    """Clear, select an example, submit, and wait for a real response.

    Flow: scroll to top -> Clear -> scroll to top -> click example -> hold on
    the filled input -> scroll to top -> click 'Ask WorkerShield' -> wait for
    a real (non-loading) response.
    """
    await scroll_to_top(page)

    await click_clear(page, tag)
    await page.wait_for_timeout(800)

    await scroll_to_top(page)

    log(f"[Segment {tag}] Clicking example: {example_text!r}")
    await page.get_by_role("button", name=example_text).first.click()
    await page.wait_for_timeout(1500)

    await scroll_to_top(page)

    log(f"[Segment {tag}] Clicking 'Ask WorkerShield'...")
    await page.get_by_role("button", name="Ask WorkerShield").click()
    await page.wait_for_timeout(1000)

    success = await wait_for_real_response(page, tag)
    if success:
        log(f"[Segment {tag}] Holding 2s for response to finish rendering...")
        await page.wait_for_timeout(2000)
    return success


async def reveal_response_sequence(page: Page, tag: str) -> None:
    """Standard post-response scroll sequence: Agent Route -> Confidence ->
    response text -> Sources accordion. Call only after a real response has
    been detected by wait_for_real_response()."""
    await scroll_to_top(page)

    log(f"[Segment {tag}] Revealing Agent Route...")
    await smooth_scroll(page, steps=2, pixels=200, delay_ms=1000)
    await page.wait_for_timeout(3000)

    log(f"[Segment {tag}] Revealing Confidence...")
    await smooth_scroll(page, steps=1, pixels=150, delay_ms=1000)
    await page.wait_for_timeout(2000)

    log(f"[Segment {tag}] Revealing response text...")
    await smooth_scroll(page, steps=3, pixels=250, delay_ms=1200)
    await page.wait_for_timeout(4000)

    log(f"[Segment {tag}] Revealing Sources...")
    await smooth_scroll(page, steps=2, pixels=200, delay_ms=1000)
    await page.wait_for_timeout(1000)

    await click_sources_accordion(page, tag)
    await page.wait_for_timeout(3000)


def make_result(
    segment: dict, actual_duration: float | None, output_path: Path | None, success: bool, note: str | None = None
) -> SegmentResult:
    return SegmentResult(
        id=segment["id"],
        label=segment["label"],
        recording_file=segment["recording_file"],
        target_duration=segment["duration_seconds"],
        actual_duration=actual_duration,
        output_path=output_path,
        success=success,
        note=note,
    )


async def run_intro_segment(browser: Browser, segment: dict, obs: OBSController, recordings_dir: Path) -> SegmentResult:
    """GitHub repo tour: header, README, architecture section, file tree, folders."""
    tag = Path(segment["recording_file"]).stem

    ensure_chrome_foreground()

    page = find_page_by_url(browser, GITHUB_URL_HOST)
    if page is None:
        log(f"[Segment {tag}] Opening new GitHub tab: {GITHUB_REPO_URL}")
        page = await browser.contexts[0].new_page()
        await page.goto(GITHUB_REPO_URL)
        await page.wait_for_timeout(2000)
    elif GITHUB_REPO_PATH not in page.url:
        log(f"[Segment {tag}] Navigating existing GitHub tab to {GITHUB_REPO_URL}")
        await page.goto(GITHUB_REPO_URL)
        await page.wait_for_timeout(2000)

    log(f"[Segment {tag}] Bringing GitHub tab to front...")
    ensure_chrome_foreground()
    await page.bring_to_front()
    await scroll_to_top(page)

    start_time = await start_segment(obs, recordings_dir, segment, tag)

    log(f"[Segment {tag}] Holding 3s on repo header...")
    await page.wait_for_timeout(3000)

    log(f"[Segment {tag}] Scrolling to README top...")
    await smooth_scroll(page, steps=2)
    log(f"[Segment {tag}] Holding 4s on README...")
    await page.wait_for_timeout(4000)

    log(f"[Segment {tag}] Scrolling to Architecture section (money shot)...")
    try:
        await page.get_by_role("heading", name="Architecture").first.scroll_into_view_if_needed(timeout=5_000)
    except Exception:
        await scroll_to_text(page, "Architecture", tag)
    log(f"[Segment {tag}] Holding 6s on architecture section...")
    await page.wait_for_timeout(6000)

    log(f"[Segment {tag}] Scrolling back to file tree...")
    await scroll_to_top(page)
    log(f"[Segment {tag}] Holding 4s on file tree (agents/, corpus/, ui/, config/)...")
    await page.wait_for_timeout(4000)

    log(f"[Segment {tag}] Scrolling through repo folders...")
    await smooth_scroll(page)
    log(f"[Segment {tag}] Holding 4s...")
    await page.wait_for_timeout(4000)

    log(f"[Segment {tag}] Navigating to {GITHUB_README_URL} for the full architecture diagram...")
    await page.goto(GITHUB_README_URL)
    await page.wait_for_timeout(1000)
    ensure_chrome_foreground()
    await page.bring_to_front()
    await scroll_to_top(page)

    log(f"[Segment {tag}] Scrolling to Architecture section (full system diagram)...")
    try:
        await page.get_by_role("heading", name="Architecture").first.scroll_into_view_if_needed(timeout=5_000)
    except Exception:
        await scroll_to_text(page, "Architecture", tag)
    log(f"[Segment {tag}] Holding 8s on architecture diagram (sets up segment 08's MCP story)...")
    await page.wait_for_timeout(8000)

    actual_duration, output_path = await finish_segment(obs, recordings_dir, segment, start_time, tag)
    return make_result(segment, actual_duration, output_path, success=True)


async def run_query_segment(
    page: Page,
    segment: dict,
    obs: OBSController,
    recordings_dir: Path,
    example_text: str,
) -> SegmentResult:
    """Shared flow for segments 02-04: Clear, select example, submit, reveal response."""
    tag = Path(segment["recording_file"]).stem

    ensure_chrome_foreground()
    log(f"[Segment {tag}] Bringing WorkerShield tab to front...")
    ensure_chrome_foreground()
    await page.bring_to_front()

    example_locator = page.get_by_role("button", name=example_text).first
    log(f"[Segment {tag}] Waiting for example button to load...")
    await example_locator.wait_for(state="visible", timeout=30_000)

    await scroll_to_top(page)

    start_time = await start_segment(obs, recordings_dir, segment, tag)

    success = await submit_example_query(page, example_text, tag)
    await reveal_response_sequence(page, tag)

    actual_duration, output_path = await finish_segment(obs, recordings_dir, segment, start_time, tag)
    return make_result(segment, actual_duration, output_path, success=success)


async def run_fairdesk_segment(page: Page, segment: dict, obs: OBSController, recordings_dir: Path) -> SegmentResult:
    """Segment 03: casual employee overtime query via FairDesk."""
    return await run_query_segment(page, segment, obs, recordings_dir, FAIRDESK_EXAMPLE)


async def run_qdrant_segment(page: Page, segment: dict, obs: OBSController, recordings_dir: Path) -> SegmentResult:
    tag = Path(segment["recording_file"]).stem

    ensure_chrome_foreground()
    log(f"[Segment {tag}] Bringing Qdrant tab to front...")
    ensure_chrome_foreground()
    await page.bring_to_front()

    log(f"[Segment {tag}] Navigating to {QDRANT_COLLECTIONS_URL}...")
    await page.goto(QDRANT_COLLECTIONS_URL)
    await scroll_to_top(page)

    start_time = await start_segment(obs, recordings_dir, segment, tag)

    log(f"[Segment {tag}] Holding 3s on collections list...")
    await page.wait_for_timeout(3000)

    log(f"[Segment {tag}] Holding 3s on workershield row (1270 points, GREEN)...")
    await page.wait_for_timeout(3000)

    log(f"[Segment {tag}] Clicking workershield collection...")
    collection_row = page.get_by_text("workershield", exact=False).first
    try:
        await collection_row.click(timeout=10_000)
    except Exception:
        log(f"[Segment {tag}] WARNING: could not find 'workershield' collection row")

    log(f"[Segment {tag}] Holding 4s...")
    await page.wait_for_timeout(4000)

    await scroll_to_top(page)
    log(f"[Segment {tag}] Holding 2s on vector config (text-dense 768 Cosine, text-sparse Sparse)...")
    await page.wait_for_timeout(2000)

    log(f"[Segment {tag}] Revealing vectors...")
    await smooth_scroll(page, steps=3, pixels=200)
    log(f"[Segment {tag}] Holding 4s...")
    await page.wait_for_timeout(4000)

    log(f"[Segment {tag}] Navigating to {QDRANT_WORKERSHIELD_URL}...")
    await page.goto(QDRANT_WORKERSHIELD_URL)
    await page.wait_for_timeout(1000)

    log(f"[Segment {tag}] Clicking 'Points' tab if visible...")
    points_tab = page.get_by_text("Points", exact=False).first
    try:
        await points_tab.click(timeout=5_000)
    except Exception:
        log(f"[Segment {tag}] 'Points' tab not found, staying on current view")

    log(f"[Segment {tag}] Holding 4s...")
    await page.wait_for_timeout(4000)

    log(f"[Segment {tag}] Navigating back via Collections sidebar link...")
    collections_link = page.get_by_role("link", name="Collections", exact=False).first
    try:
        await collections_link.click(timeout=5_000)
    except Exception:
        log(f"[Segment {tag}] 'Collections' sidebar link not found")

    log(f"[Segment {tag}] Holding 2s...")
    await page.wait_for_timeout(2000)

    actual_duration, output_path = await finish_segment(obs, recordings_dir, segment, start_time, tag)
    return make_result(segment, actual_duration, output_path, success=True)


async def run_phoenix_segment(browser: Browser, segment: dict, obs: OBSController, recordings_dir: Path) -> SegmentResult:
    tag = Path(segment["recording_file"]).stem

    log(f"[Segment {tag}] Checking Phoenix at {PHOENIX_URL}...")
    if not check_phoenix_reachable():
        log(f"[Segment {tag}] Phoenix not running — segment 06 skipped")
        return make_result(segment, None, None, success=True, note="Phoenix not running — segment 06 skipped")

    ensure_chrome_foreground()

    page = find_page_by_url(browser, PHOENIX_URL_HOST)
    if page is None:
        log(f"[Segment {tag}] No existing Phoenix tab found, opening one...")
        page = await browser.contexts[0].new_page()

    log(f"[Segment {tag}] Navigating to {PHOENIX_TRACES_URL}...")
    await page.goto(PHOENIX_TRACES_URL)
    ensure_chrome_foreground()
    await page.bring_to_front()
    await scroll_to_top(page)

    start_time = await start_segment(obs, recordings_dir, segment, tag)

    log(f"[Segment {tag}] Holding 3s on projects list...")
    await page.wait_for_timeout(3000)

    log(f"[Segment {tag}] Clicking 'workershield' project card...")
    project_card = page.get_by_text("workershield", exact=False).first
    try:
        await project_card.click(timeout=10_000)
    except Exception:
        log(f"[Segment {tag}] WARNING: could not find 'workershield' project card")

    log(f"[Segment {tag}] Holding 3s for project to load...")
    await page.wait_for_timeout(3000)

    log(f"[Segment {tag}] Clicking 'Traces' tab...")
    traces_tab = page.get_by_role("tab", name="Traces")
    try:
        await traces_tab.click(timeout=10_000)
    except Exception:
        log(f"[Segment {tag}] WARNING: could not find 'Traces' tab")

    log(f"[Segment {tag}] Holding 3s for traces list to load...")
    await page.wait_for_timeout(3000)

    await scroll_to_top(page)
    log(f"[Segment {tag}] Holding 2s on traces list (timestamps, latency)...")
    await page.wait_for_timeout(2000)

    log(f"[Segment {tag}] Clicking most recent trace row...")
    trace_row = page.locator("tr.traces-table-row, table tbody tr").first
    try:
        await trace_row.click(timeout=10_000)
    except Exception:
        log(f"[Segment {tag}] WARNING: could not find a trace row to click")

    log(f"[Segment {tag}] Holding 4s for trace waterfall to load...")
    await page.wait_for_timeout(4000)

    await scroll_to_top(page)
    log(f"[Segment {tag}] Holding 3s on waterfall (router_node, retrieval_node, reranker_node, synthesis_node)...")
    await page.wait_for_timeout(3000)

    log(f"[Segment {tag}] Scrolling through the LangGraph execution chain...")
    await smooth_scroll(page, steps=3, pixels=150, delay_ms=1200)
    log(f"[Segment {tag}] Holding 4s...")
    await page.wait_for_timeout(4000)

    log(f"[Segment {tag}] Closing any open annotation panel...")
    try:
        await page.keyboard.press("Escape")
    except Exception:
        log(f"[Segment {tag}] WARNING: could not send Escape key")

    log(f"[Segment {tag}] Holding 2s...")
    await page.wait_for_timeout(2000)

    actual_duration, output_path = await finish_segment(obs, recordings_dir, segment, start_time, tag)
    return make_result(segment, actual_duration, output_path, success=True)


async def run_ragas_segment(browser: Browser, segment: dict, obs: OBSController, recordings_dir: Path) -> SegmentResult:
    """RAGAS evaluation results: static comparison table + interpretation."""
    tag = Path(segment["recording_file"]).stem

    ensure_chrome_foreground()

    page = find_page_by_url(browser, "ragas_results.html")
    if page is not None:
        log(f"[Segment {tag}] Found existing RAGAS results tab: {page.url}")
        await page.bring_to_front()
    else:
        log(f"[Segment {tag}] No existing RAGAS tab found, opening new tab: {RAGAS_RESULTS_URL}")
        page = await browser.new_page()
        await page.goto(RAGAS_RESULTS_URL)
        await page.bring_to_front()

    await page.wait_for_load_state("domcontentloaded")
    ensure_chrome_foreground()
    await scroll_to_top(page)

    start_time = await start_segment(obs, recordings_dir, segment, tag)

    log(f"[Segment {tag}] Holding 3s on RAGAS results table...")
    await page.wait_for_timeout(3000)

    log(f"[Segment {tag}] Revealing all rows...")
    await smooth_scroll(page, steps=2, pixels=150)
    log(f"[Segment {tag}] Holding 5s for viewer to read the evaluation scores...")
    await page.wait_for_timeout(5000)

    log(f"[Segment {tag}] Revealing interpretation section...")
    await smooth_scroll(page, steps=2, pixels=150)
    log(f"[Segment {tag}] Holding 5s...")
    await page.wait_for_timeout(5000)

    actual_duration, output_path = await finish_segment(obs, recordings_dir, segment, start_time, tag)
    return make_result(segment, actual_duration, output_path, success=True)


async def run_mcp_wrap_segment(
    browser: Browser,
    workershield_page: Page,
    segment: dict,
    obs: OBSController,
    recordings_dir: Path,
) -> SegmentResult:
    """MCP + cross-domain wrap: GitHub MCP architecture/tools, then the killer
    query (safeshift_node + healthnav_node + incident_check_node via MCP)."""
    tag = Path(segment["recording_file"]).stem

    ensure_chrome_foreground()

    github_page = find_page_by_url(browser, GITHUB_URL_HOST)
    if github_page is None:
        log(f"[Segment {tag}] Opening new GitHub tab: {GITHUB_MCP_ARCHITECTURE_URL}")
        github_page = await browser.contexts[0].new_page()

    log(f"[Segment {tag}] Navigating to {GITHUB_MCP_ARCHITECTURE_URL}...")
    response = await github_page.goto(GITHUB_MCP_ARCHITECTURE_URL)
    if response is None or response.status >= 400:
        log(f"[Segment {tag}] MCP_ARCHITECTURE.md not found, falling back to {GITHUB_MCP_SERVER_URL}")
        await github_page.goto(GITHUB_MCP_SERVER_URL)

    ensure_chrome_foreground()
    await github_page.bring_to_front()
    await scroll_to_top(github_page)

    start_time = await start_segment(obs, recordings_dir, segment, tag)

    log(f"[Segment {tag}] Holding 4s on MCP architecture...")
    await github_page.wait_for_timeout(4000)

    log(f"[Segment {tag}] Scrolling to MCP tools table (get_incident_summary, query_incidents, get_incident_detail)...")
    await smooth_scroll(github_page, steps=2, pixels=200)
    log(f"[Segment {tag}] Holding 4s on tool definitions...")
    await github_page.wait_for_timeout(4000)

    log(f"[Segment {tag}] Switching to WorkerShield tab...")
    ensure_chrome_foreground()
    await workershield_page.bring_to_front()

    log(f"[Segment {tag}] Killer query fires safeshift_node + healthnav_node + incident_check_node (MCP)...")
    success = await submit_example_query(workershield_page, KILLER_QUERY_EXAMPLE, tag)
    await reveal_response_sequence(workershield_page, tag)

    actual_duration, output_path = await finish_segment(obs, recordings_dir, segment, start_time, tag)
    return make_result(segment, actual_duration, output_path, success=success)


def print_summary(results: list[SegmentResult]) -> None:
    click.echo()
    click.echo("=== Demo Recording Summary ===")
    total_seconds = 0.0
    for r in results:
        if r.note:
            status = "⏭️ "
        elif r.success:
            status = "✅"
        else:
            status = "❌"
        actual = f"{r.actual_duration:.1f}s" if r.actual_duration is not None else "n/a"
        click.echo(
            f"{status} Segment {r.id:02d} ({r.label}): {actual} recorded / {r.target_duration}s target"
        )
        if r.note:
            click.echo(f"    {r.note}")
        if r.actual_duration is not None:
            total_seconds += r.actual_duration
    click.echo(f"\nTotal recording time: {total_seconds:.1f}s")

    click.echo("\nOutput files:")
    for r in results:
        path_str = str(r.output_path) if r.output_path else "MISSING"
        click.echo(f"  {r.recording_file}: {path_str}")


async def run_demo(project: str) -> list[SegmentResult]:
    script_path = REPO_ROOT / "projects" / project / "script.yaml"
    if not script_path.exists():
        raise click.ClickException(f"No script found at {script_path}")

    script = load_yaml(script_path)
    segments = script.get("segments", [])

    recordings_dir = REPO_ROOT / "projects" / project / "assets" / "recordings"
    recordings_dir.mkdir(parents=True, exist_ok=True)

    obs = OBSController()
    log(f"Connecting to OBS WebSocket at {obs.host}:{obs.port}...")
    obs.connect()
    version = obs.get_version()
    log(f"Connected to OBS {version.get('obs_version')} (websocket {version.get('obs_web_socket_version')})")

    obs.add_display_capture()
    ensure_chrome_foreground()

    results: list[SegmentResult] = []

    try:
        async with async_playwright() as pw:
            log(f"Connecting to existing Chrome via CDP on port {CHROME_CDP_PORT}...")
            browser = await pw.chromium.connect_over_cdp(CHROME_CDP_URL)

            workershield_page = find_page_by_url(browser, GRADIO_URL_HOST)
            if workershield_page is None:
                raise click.ClickException(
                    f"No open Chrome tab found for {GRADIO_URL_HOST}; "
                    "open the WorkerShield UI in Chrome before running the bot"
                )
            log(f"[Setup] Found WorkerShield tab: {workershield_page.url}")

            qdrant_page = find_page_by_url(browser, QDRANT_URL_HOST)
            if qdrant_page is None:
                raise click.ClickException(
                    f"No open Chrome tab found for {QDRANT_URL_HOST}; "
                    "open the Qdrant dashboard in Chrome before running the bot"
                )
            log(f"[Setup] Found Qdrant tab: {qdrant_page.url}")

            # Connected via CDP to the user's existing Chrome session -- never
            # close this browser, only disconnect (handled by exiting the
            # `async_playwright()` context below).

            for index, segment in enumerate(segments):
                if index > 0:
                    log("Pausing 1.5s before next segment...")
                    await asyncio.sleep(1.5)

                label = segment.get("label")
                if label == "intro":
                    result = await run_intro_segment(browser, segment, obs, recordings_dir)
                elif label == "safeshift_demo":
                    result = await run_query_segment(
                        workershield_page, segment, obs, recordings_dir, SAFESHIFT_EXAMPLE
                    )
                elif label == "fairdesk_demo":
                    result = await run_fairdesk_segment(workershield_page, segment, obs, recordings_dir)
                elif label == "healthnav_demo":
                    result = await run_query_segment(
                        workershield_page, segment, obs, recordings_dir, HEALTHNAV_EXAMPLE
                    )
                elif label == "qdrant_demo":
                    result = await run_qdrant_segment(qdrant_page, segment, obs, recordings_dir)
                elif label == "phoenix_demo":
                    result = await run_phoenix_segment(browser, segment, obs, recordings_dir)
                elif label == "ragas_demo":
                    result = await run_ragas_segment(browser, segment, obs, recordings_dir)
                elif label == "mcp_wrap_demo":
                    result = await run_mcp_wrap_segment(browser, workershield_page, segment, obs, recordings_dir)
                else:
                    log(f"WARNING: no handler for segment '{label}', skipping")
                    continue
                results.append(result)
    finally:
        obs.disconnect()

    log(f"Done. Recordings saved to {recordings_dir}")
    print_summary(results)
    return results


@click.command()
@click.option(
    "--project",
    required=True,
    help="Project slug under projects/, e.g. workershield",
)
def main(project: str) -> None:
    asyncio.run(run_demo(project))


if __name__ == "__main__":
    main()
