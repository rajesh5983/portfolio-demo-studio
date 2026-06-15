"""Smoke test for the ElevenLabs text-to-speech REST API.

Usage:
    python engine/test_elevenlabs.py
"""

import os
import sys
from pathlib import Path

import httpx
import yaml
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config" / "voice_config.yaml"
OUTPUT_PATH = REPO_ROOT / "output" / "test_audio.mp3"
ENV_PATH = REPO_ROOT / ".env"

ELEVENLABS_TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"

TEST_PHRASE = (
    "WorkerShield routes your query through three compliance agents — "
    "SafeShift, FairDesk, and HealthNav. Built for Australian workplace compliance."
)


def load_yaml(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def main() -> None:
    load_dotenv(ENV_PATH)

    api_key = os.getenv("ELEVENLABS_API_KEY")
    voice_id = os.getenv("ELEVENLABS_VOICE_ID")

    if not api_key:
        print("ELEVENLABS_API_KEY is not set in .env")
        sys.exit(1)
    if not voice_id:
        print("ELEVENLABS_VOICE_ID is not set in .env")
        sys.exit(1)

    voice_config = load_yaml(CONFIG_PATH)

    url = ELEVENLABS_TTS_URL.format(voice_id=voice_id)
    params = {"output_format": voice_config.get("output_format", "mp3_44100_128")}
    payload = {
        "text": TEST_PHRASE,
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

    if response.status_code != 200:
        print(f"Request failed with HTTP {response.status_code}")
        print(response.text)
        sys.exit(1)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_bytes(response.content)

    size_kb = OUTPUT_PATH.stat().st_size / 1024
    print(f"Saved {size_kb:.1f} KB to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
