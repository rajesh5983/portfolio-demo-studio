"""Re-record a single demo segment without running the full bot.

Connects to the already-running Chrome (CDP) and OBS WebSocket, runs only
the requested segment's function from demo_bot.py, and overwrites that
segment's recording in projects/{project}/assets/recordings/.

Usage:
    python engine/rerecord_segment.py --project workershield --segment 03_fairdesk
    python engine/rerecord_segment.py --project workershield --segment 03_fairdesk --warmup

Requires OBS running with the WebSocket server enabled, and Chrome already
running with remote debugging enabled on CHROME_CDP_PORT (see
engine/launch_chrome.py).
"""

import asyncio
import sys
from pathlib import Path

import click
import httpx
from dotenv import load_dotenv
from playwright.async_api import Browser, Page, async_playwright

from demo_bot import (
    CHROME_CDP_PORT,
    CHROME_CDP_URL,
    FAIRDESK_EXAMPLE,
    GRADIO_URL,
    GRADIO_URL_HOST,
    HEALTHNAV_EXAMPLE,
    QDRANT_URL_HOST,
    SAFESHIFT_EXAMPLE,
    SegmentResult,
    click_clear,
    find_page_by_url,
    load_yaml,
    log,
    run_fairdesk_segment,
    run_intro_segment,
    run_mcp_wrap_segment,
    run_phoenix_segment,
    run_qdrant_segment,
    run_query_segment,
    run_ragas_segment,
    scroll_to_top,
    submit_example_query,
)
from obs_controller import OBSController, ensure_chrome_foreground

sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parent.parent

# Labels whose segment involves the WorkerShield Gradio tab.
WORKERSHIELD_LABELS = ("fairdesk_demo", "safeshift_demo", "healthnav_demo", "mcp_wrap_demo")


class RerecordOBSController(OBSController):
    """OBS controller for re-recording a single segment.

    SetRecordDirectory returns a 500 from OBS when the target path doesn't
    exist yet or OBS is mid-recording, which aborts the whole re-record.
    Skip it and rely on the recording path already configured in OBS
    Settings -> Output -> Recording Path; finalize_recording() below picks
    up the resulting file by modification time instead.
    """

    def set_output_path(self, directory: Path) -> None:
        pass


def find_segment(project: str, segment: str) -> dict:
    """Look up a segment by recording-file stem (e.g. '03_fairdesk'), label, or id."""
    script_path = REPO_ROOT / "projects" / project / "script.yaml"
    if not script_path.exists():
        raise click.ClickException(f"No script found at {script_path}")

    script = load_yaml(script_path)
    segments = script.get("segments", [])
    for seg in segments:
        stem = Path(seg["recording_file"]).stem
        if segment in (stem, seg.get("label"), str(seg.get("id"))):
            return seg

    available = ", ".join(Path(seg["recording_file"]).stem for seg in segments)
    raise click.ClickException(f"Segment '{segment}' not found in {script_path}. Available: {available}")


def check_workershield_reachable() -> bool:
    try:
        response = httpx.get(GRADIO_URL, timeout=5.0, follow_redirects=True)
        return response.status_code == 200
    except httpx.HTTPError as exc:
        log(f"WorkerShield not reachable at {GRADIO_URL}: {exc}")
        return False


def find_workershield_page(browser: Browser) -> Page:
    page = find_page_by_url(browser, GRADIO_URL_HOST)
    if page is None:
        raise click.ClickException(
            f"No open Chrome tab found for {GRADIO_URL_HOST}; "
            "open the WorkerShield UI in Chrome before running this script"
        )
    return page


def get_latest_mp4(folder: Path) -> Path | None:
    files = list(folder.glob("*.mp4"))
    if not files:
        return None
    return max(files, key=lambda f: f.stat().st_mtime)


async def finalize_recording(result: SegmentResult, segment: dict, recordings_dir: Path, tag: str) -> SegmentResult:
    """Make sure the recording ends up named for this segment.

    finish_segment() already renames OBS's reported output file, but that
    report can be empty since RerecordOBSController never calls
    SetRecordDirectory. Fall back to the most recently written .mp4.
    """
    target = recordings_dir / segment["recording_file"]
    if result.output_path and result.output_path.exists():
        return result

    log(f"[Segment {tag}] Waiting for OBS to finish writing the recording...")
    await asyncio.sleep(3)

    latest = get_latest_mp4(recordings_dir)
    if latest is None:
        return result

    if latest.name != target.name:
        if target.exists():
            target.unlink()
        latest.rename(target)
        log(f"[Segment {tag}] Renamed {latest.name} -> {target.name}")

    result.output_path = target
    return result


async def run_preflight(browser: Browser, label: str) -> None:
    """Get Chrome into a clean recording state right before OBS starts."""
    if label in WORKERSHIELD_LABELS:
        page = find_workershield_page(browser)
        log("[Preflight] Bringing WorkerShield tab to front...")
        ensure_chrome_foreground()
        await page.bring_to_front()

        log("[Preflight] Clearing input and scrolling to top...")
        await click_clear(page, "preflight")
        await page.wait_for_timeout(800)
        await scroll_to_top(page)

    log("READY TO RECORD — starting in 3s")
    await asyncio.sleep(3)


async def run_warmup(page: Page) -> None:
    """Fire the FairDesk query once (no OBS) to warm up Ollama embeddings."""
    log("[Warmup] Bringing WorkerShield tab to front...")
    ensure_chrome_foreground()
    await page.bring_to_front()

    log("[Warmup] Firing FairDesk query to warm up Ollama embeddings...")
    await submit_example_query(page, FAIRDESK_EXAMPLE, "warmup")

    log("[Warmup] Clearing input before the real recording run...")
    await click_clear(page, "warmup")
    await page.wait_for_timeout(800)
    log("[Warmup] Done")


async def dispatch_segment(
    browser: Browser, segment: dict, obs: OBSController, recordings_dir: Path
) -> SegmentResult:
    """Run the demo_bot segment function matching this segment's label."""
    label = segment.get("label")

    if label == "intro":
        return await run_intro_segment(browser, segment, obs, recordings_dir)

    if label == "fairdesk_demo":
        page = find_workershield_page(browser)
        return await run_fairdesk_segment(page, segment, obs, recordings_dir)

    if label in ("safeshift_demo", "healthnav_demo"):
        page = find_workershield_page(browser)
        example = SAFESHIFT_EXAMPLE if label == "safeshift_demo" else HEALTHNAV_EXAMPLE
        return await run_query_segment(page, segment, obs, recordings_dir, example)

    if label == "qdrant_demo":
        page = find_page_by_url(browser, QDRANT_URL_HOST)
        if page is None:
            raise click.ClickException(
                f"No open Chrome tab found for {QDRANT_URL_HOST}; "
                "open the Qdrant dashboard in Chrome before running this script"
            )
        return await run_qdrant_segment(page, segment, obs, recordings_dir)

    if label == "phoenix_demo":
        return await run_phoenix_segment(browser, segment, obs, recordings_dir)

    if label == "ragas_demo":
        return await run_ragas_segment(browser, segment, obs, recordings_dir)

    if label == "mcp_wrap_demo":
        page = find_workershield_page(browser)
        return await run_mcp_wrap_segment(browser, page, segment, obs, recordings_dir)

    raise click.ClickException(f"No handler for segment label '{label}'")


async def rerecord(project: str, segment_name: str, warmup: bool) -> SegmentResult:
    segment = find_segment(project, segment_name)
    tag = Path(segment["recording_file"]).stem
    label = segment.get("label")

    recordings_dir = REPO_ROOT / "projects" / project / "assets" / "recordings"
    recordings_dir.mkdir(parents=True, exist_ok=True)

    if label == "fairdesk_demo":
        log(f"[Setup] Checking WorkerShield is reachable at {GRADIO_URL}...")
        if not check_workershield_reachable():
            raise click.ClickException(f"WorkerShield not reachable at {GRADIO_URL}")
        log("[Setup] WorkerShield reachable")

    log(f"Connecting to existing Chrome via CDP on port {CHROME_CDP_PORT}...")
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(CHROME_CDP_URL)

        if warmup:
            if label != "fairdesk_demo":
                log(f"[Warmup] --warmup is only implemented for fairdesk_demo, skipping for '{label}'")
            else:
                await run_warmup(find_workershield_page(browser))

        obs = RerecordOBSController()
        log(f"Connecting to OBS WebSocket at {obs.host}:{obs.port}...")
        obs.connect()
        version = obs.get_version()
        log(f"Connected to OBS {version.get('obs_version')} (websocket {version.get('obs_web_socket_version')})")

        obs.add_display_capture()
        ensure_chrome_foreground()

        await run_preflight(browser, label)

        try:
            result = await dispatch_segment(browser, segment, obs, recordings_dir)
        finally:
            obs.disconnect()

    result = await finalize_recording(result, segment, recordings_dir, tag)

    log(f"[Segment {tag}] Done. success={result.success}")
    if result.output_path and result.output_path.exists():
        size_mb = result.output_path.stat().st_size / (1024 * 1024)
        duration = f"{result.actual_duration:.1f}s" if result.actual_duration is not None else "n/a"
        log(f"[Segment {tag}] Duration: {duration}, size: {size_mb:.2f} MB -> {result.output_path}")
    else:
        log(f"[Segment {tag}] WARNING: no output file found in {recordings_dir}")

    return result


@click.command()
@click.option("--project", required=True, help="Project slug under projects/, e.g. workershield")
@click.option("--segment", required=True, help="Segment to re-record, e.g. 03_fairdesk")
@click.option(
    "--warmup",
    is_flag=True,
    default=False,
    help="Fire the FairDesk query once before recording (no OBS) to warm up Ollama embeddings",
)
def main(project: str, segment: str, warmup: bool) -> None:
    asyncio.run(rerecord(project, segment, warmup))


if __name__ == "__main__":
    main()
