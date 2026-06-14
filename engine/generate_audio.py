"""Generate per-segment narration audio for a project using the ElevenLabs REST API.

Usage:
    python engine/generate_audio.py --project workershield
"""

import os
from pathlib import Path

import click
import httpx
import yaml
from dotenv import load_dotenv

load_dotenv()

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
ELEVENLABS_TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config" / "voice_config.yaml"


def load_yaml(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def generate_segment_audio(text: str, voice_config: dict, api_key: str) -> bytes:
    """Call the ElevenLabs text-to-speech endpoint and return raw MP3 bytes."""
    voice_id = voice_config["voice_id"]
    url = ELEVENLABS_TTS_URL.format(voice_id=voice_id)

    params = {"output_format": voice_config.get("output_format", "mp3_44100_128")}
    payload = {
        "text": text,
        "model_id": voice_config.get("model", "eleven_multilingual_v2"),
        "voice_settings": {
            "stability": voice_config.get("stability", 0.5),
            "similarity_boost": voice_config.get("similarity_boost", 0.75),
            "speed": voice_config.get("speed", 1.0),
        },
    }
    headers = {
        "xi-api-key": api_key,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }

    response = httpx.post(url, params=params, json=payload, headers=headers, timeout=120.0)
    response.raise_for_status()
    return response.content


@click.command()
@click.option(
    "--project",
    required=True,
    help="Project slug under projects/, e.g. workershield",
)
def main(project: str) -> None:
    if not ELEVENLABS_API_KEY:
        raise click.ClickException(
            "ELEVENLABS_API_KEY is not set. Add it to your .env file (see .env.example)."
        )

    voice_config = load_yaml(CONFIG_PATH)
    if voice_config.get("voice_id") == "PLACEHOLDER_VOICE_ID":
        raise click.ClickException(
            "config/voice_config.yaml still has the placeholder voice_id. "
            "Set it to a real ElevenLabs voice ID before generating audio."
        )

    script_path = REPO_ROOT / "projects" / project / "script.yaml"
    if not script_path.exists():
        raise click.ClickException(f"No script found at {script_path}")

    script = load_yaml(script_path)
    audio_dir = REPO_ROOT / "projects" / project / "assets" / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)

    for segment in script.get("segments", []):
        narration = (segment.get("narration") or "").strip()
        if not narration:
            click.echo(f"Skipping segment {segment.get('id')} ({segment.get('label')}): no narration")
            continue

        recording_stem = Path(segment["recording_file"]).stem
        out_path = audio_dir / f"{recording_stem}.mp3"

        click.echo(f"Generating audio for segment {segment['id']} ({segment['label']})...")
        audio_bytes = generate_segment_audio(narration, voice_config, ELEVENLABS_API_KEY)
        out_path.write_bytes(audio_bytes)
        click.echo(f"  -> saved {out_path}")

    click.echo("Done.")


if __name__ == "__main__":
    main()
