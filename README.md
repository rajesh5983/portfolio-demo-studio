# portfolio-demo-studio

A centralised demo video production pipeline for AI/data portfolio projects.

Given a screen recording per "segment" and a narration script, this pipeline
generates voiceover audio (ElevenLabs), then composes a final demo video
(ffmpeg) — one MP4 per project, output to `output/`.

## Stack

- Python 3.11
- [ElevenLabs](https://elevenlabs.io/) REST API (via `httpx`, no SDK)
- [ffmpeg](https://ffmpeg.org/) (via `subprocess`, must be on PATH)
- [Claude API](https://www.anthropic.com/) (via the `anthropic` SDK) for script generation
- [GitHub CLI](https://cli.github.com/) for repo creation

## Setup

### 1. Install Python dependencies

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Install ffmpeg (Windows)

```powershell
winget install ffmpeg
```

Confirm it's on PATH:

```powershell
ffmpeg -version
ffprobe -version
```

### 3. Configure API keys

Copy `.env.example` to `.env` and fill in your keys:

```powershell
copy .env.example .env
```

```
ELEVENLABS_API_KEY=your_key_here
ANTHROPIC_API_KEY=your_key_here
```

### 4. Configure the voice

Edit `config/voice_config.yaml` and replace `voice_id` with a real
ElevenLabs voice ID. Adjust `stability`, `similarity_boost`, `speed`, and
`output_format` as needed.

## Workflow

Each project lives under `projects/<project>/` and is driven by a single
`script.yaml` (see `templates/script_template.yaml` for the schema).

### 1. Generate a script (optional — Claude-assisted)

```powershell
python engine/generate_script.py --project workershield --notes "your talking points"
```

This calls Claude (`claude-sonnet-4-6`) to turn your notes into a structured
`projects/workershield/script.yaml`, with one segment per part of the demo.
You can also write or edit `script.yaml` by hand.

### 2. Generate narration audio

```powershell
python engine/generate_audio.py --project workershield
```

Reads `projects/workershield/script.yaml`, calls the ElevenLabs
text-to-speech API for each segment's narration, and writes one MP3 per
segment to `projects/workershield/assets/audio/`.

### 3. Record your screen segments

Record one video per segment (e.g. with OBS) and place the files in
`projects/workershield/assets/recordings/`, named to match the
`recording_file` value for each segment in `script.yaml`
(e.g. `01_intro.mp4`, `02_safeshift.mp4`, ...).

### 4. Compose the final video

```powershell
python engine/compose_video.py --project workershield
```

For each segment, ffmpeg overlays the narration audio onto the matching
recording (padding short audio with silence, or trimming long audio, so it
exactly matches the video's duration), then concatenates all segments in
order into:

```
output/workershield_demo_final.mp4
```

## Adding a new project

1. Copy the `projects/workershield/` directory to `projects/<new_project>/`
   (or just create `projects/<new_project>/script.yaml` plus
   `assets/recordings/` and `assets/audio/` subfolders).
2. Update `script.yaml`: set `title`, `project`, `duration_target_seconds`,
   and the `segments` list (one entry per recorded clip).
3. Run the three commands above with `--project <new_project>`.

## Project layout

```
portfolio-demo-studio/
├── engine/
│   ├── generate_audio.py     # ElevenLabs TTS -> per-segment MP3s
│   ├── compose_video.py      # ffmpeg overlay + concat -> final MP4
│   └── generate_script.py    # Claude -> script.yaml
├── templates/
│   └── script_template.yaml  # schema reference for script.yaml
├── config/
│   └── voice_config.yaml     # ElevenLabs voice settings
├── projects/
│   └── <project>/
│       ├── script.yaml
│       └── assets/
│           ├── recordings/   # raw screen recordings (gitignored)
│           └── audio/        # generated narration MP3s (gitignored)
└── output/                    # final composed MP4s (gitignored)
```

## Notes

- Recordings, generated audio, and final videos are gitignored (see
  `.gitignore`) — only the pipeline code, configs, and `.gitkeep` placeholders
  are tracked.
- All file paths in the codebase are built with `pathlib.Path`, so the
  pipeline works the same on Windows, macOS, and Linux.
