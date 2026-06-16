"""Batch ElevenLabs audio pusher for approved VO cue cards.

Reads approved cues from vo_scripts/{segment}_vo_script.yaml and calls the
ElevenLabs TTS API for each one, saving per-cue MP3s to assets/audio/.

Usage:
    python engine/batch_push_audio.py --project workershield --dry-run
    python engine/batch_push_audio.py --project workershield
    python engine/batch_push_audio.py --project workershield --segment 03_fairdesk

Dry run: counts total characters and estimates credit cost without calling the API.
Real run: skips cues already on disk, so it is safe to re-run after interruption.
"""

import os
import sys
from pathlib import Path

import click
import httpx
import yaml
from dotenv import load_dotenv

load_dotenv()

sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config" / "voice_config.yaml"

ELEVENLABS_TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"

# ElevenLabs bills by character; 1 credit ≈ 1 character on most plans.
_CHARS_PER_CREDIT = 1


def _load_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _tts(text: str, voice_config: dict, api_key: str, voice_id: str) -> bytes:
    url = ELEVENLABS_TTS_URL.format(voice_id=voice_id)
    response = httpx.post(
        url,
        params={"output_format": voice_config.get("output_format", "mp3_44100_128")},
        json={
            "text": text,
            "model_id": voice_config.get("model", "eleven_multilingual_v2"),
            "voice_settings": {
                "stability": voice_config.get("stability", 0.5),
                "similarity_boost": voice_config.get("similarity_boost", 0.75),
                "speed": voice_config.get("speed", 1.0),
            },
        },
        headers={
            "xi-api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        },
        timeout=120.0,
    )
    response.raise_for_status()
    return response.content


@click.command()
@click.option("--project", required=True, help="Project slug under projects/, e.g. workershield")
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    default=False,
    help="Count chars and estimate credit cost — do not call the API",
)
@click.option("--segment", default=None, help="Process a single segment, e.g. 03_fairdesk")
def main(project: str, dry_run: bool, segment: str | None) -> None:
    api_key = os.getenv("ELEVENLABS_API_KEY")
    voice_id = os.getenv("ELEVENLABS_VOICE_ID")

    if not dry_run:
        if not api_key:
            raise click.ClickException("ELEVENLABS_API_KEY not set in .env")
        if not voice_id:
            raise click.ClickException("ELEVENLABS_VOICE_ID not set in .env")

    voice_config = _load_yaml(CONFIG_PATH)

    vo_dir = REPO_ROOT / "projects" / project / "vo_scripts"
    audio_dir = REPO_ROOT / "projects" / project / "assets" / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)

    if segment:
        script_files = [vo_dir / f"{segment}_vo_script.yaml"]
    else:
        script_files = sorted(vo_dir.glob("*_vo_script.yaml"))

    if not script_files or not any(f.exists() for f in script_files):
        raise click.ClickException(
            f"No VO scripts found in {vo_dir}. "
            "Run generate_vo_scripts.py first, then set approved: yes on each cue."
        )

    total_chars = 0
    total_cues_generated = 0
    total_cues_skipped = 0
    segments_processed = 0

    for script_path in script_files:
        if not script_path.exists():
            click.echo(f"  WARNING: {script_path.name} not found — skipping")
            continue

        vo_script = _load_yaml(script_path)
        stem = script_path.stem.replace("_vo_script", "")
        all_cues = vo_script.get("cues", [])
        approved = [
            c for c in all_cues
            if c.get("approved") is True or str(c.get("approved", "")).strip().lower() == "yes"
        ]

        if not approved:
            click.echo(
                f"  {stem}: 0 approved cues "
                f"({len(all_cues)} total) — set approved: yes in {script_path.name}"
            )
            continue

        seg_chars = sum(len((c.get("vo_text") or "").strip()) for c in approved)
        click.echo(f"  {stem}: {len(approved)} approved cue(s), {seg_chars} chars")
        total_chars += seg_chars
        segments_processed += 1

        for i, cue in enumerate(approved):
            text = (cue.get("vo_text") or "").strip()
            if not text:
                continue

            out_path = audio_dir / f"{stem}_{i:02d}.mp3"

            if dry_run:
                ts = cue.get("timestamp", "??:??")
                click.echo(f"    [{i:02d}] {ts} — {len(text)} chars")
                continue

            if out_path.exists():
                click.echo(f"    [{i:02d}] {out_path.name} already exists — skipping")
                total_cues_skipped += 1
                total_cues_generated += 1
                continue

            ts = cue.get("timestamp", "??:??")
            click.echo(f"    [{i:02d}] {ts} generating {len(text)} chars...", nl=False)
            audio_bytes = _tts(text, voice_config, api_key, voice_id)
            out_path.write_bytes(audio_bytes)
            size_kb = out_path.stat().st_size / 1024
            click.echo(f" -> {out_path.name} ({size_kb:.1f} KB)")
            total_cues_generated += 1

    click.echo()
    if dry_run:
        estimated_credits = total_chars * _CHARS_PER_CREDIT
        click.echo(
            f"DRY RUN COMPLETE — {total_chars} chars across "
            f"{segments_processed} segment(s), ~{estimated_credits} credits"
        )
    else:
        new_cues = total_cues_generated - total_cues_skipped
        click.echo(
            f"DONE — {segments_processed} segment(s), "
            f"{new_cues} new cue(s) generated, "
            f"{total_cues_skipped} skipped (already exist), "
            f"{total_chars} total chars"
        )


if __name__ == "__main__":
    main()
