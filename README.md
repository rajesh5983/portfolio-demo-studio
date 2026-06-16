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

**Automated (recommended):** run the demo bot to drive the Gradio UI while
OBS records each segment automatically — see
[Automated Demo Recording](#automated-demo-recording) below.

```powershell
python engine/demo_bot.py --project workershield
```

**Manual:** record one video per segment yourself (e.g. with OBS) and place
the files in `projects/workershield/assets/recordings/`, named to match the
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

## Automated Demo Recording

`engine/demo_bot.py` replaces manual OBS screen recording: it drives the
project's Gradio UI with Playwright while OBS records each segment, then
hands off to the existing audio/video pipeline.

### Prerequisites

- OBS open and running, with the WebSocket server enabled (one-time setup,
  see below)
- The project's Gradio UI running locally (e.g. WorkerShield on
  `http://localhost:7860`)
- Qdrant running locally (e.g. dashboard on `http://localhost:6333/dashboard`)

### One-time OBS setup

Follow [`demo/obs_setup_guide.md`](demo/obs_setup_guide.md) to enable the OBS
WebSocket server, set the recording format/path/resolution/encoder, and add
`OBS_WEBSOCKET_PASSWORD` to your `.env`. Confirm the connection with:

```powershell
python engine/test_obs.py
```

### The 3-command recording pipeline

Ensure WorkerShield, Qdrant and Phoenix are running on the VM first.

**Step 1 — Launch Chrome** (once per session):

```powershell
python engine/launch_chrome.py
```

Opens a dedicated Chrome window with CDP enabled on `CHROME_CDP_PORT` and
pre-loads all five demo tabs (WorkerShield, Qdrant, Phoenix, GitHub, RAGAS).
Skip this step if Chrome is already running with CDP from a previous session.

**Step 2 — Record all 8 segments:**

```powershell
python engine/run_demo.py --project workershield
```

Runs pre-flight checks (Gradio, Qdrant, OBS), maximises Chrome, then drives
`demo_bot.py` to record every segment in `script.yaml` order, saving MP4s to
`projects/workershield/assets/recordings/`.

**Step 3 — Re-record a single segment** (if one needs a do-over):

```powershell
python engine/rerecord_segment.py --project workershield --segment 03_fairdesk --warmup
```

Connects to the already-running Chrome and OBS, runs only the named segment's
function, and overwrites that recording. `--warmup` fires a FairDesk query
once before recording to warm up Ollama embeddings (fairdesk_demo only).
Segment can be specified by filename stem (`03_fairdesk`), label
(`fairdesk_demo`), or id (`3`).

### Full pipeline summary

| Step | Command |
| ---- | ------- |
| 1    | `python engine/launch_chrome.py` |
| 2    | `python engine/run_demo.py --project workershield` |
| 2b   | `python engine/rerecord_segment.py --project workershield --segment {id} --warmup` _(per-segment fix)_ |
| 3    | `python engine/generate_audio.py --project workershield` |
| 4    | `python engine/compose_video.py --project workershield` |

Once recordings land in `assets/recordings/`, run `generate_audio.py` and
`compose_video.py` as normal (steps 2 and 4 in [Workflow](#workflow)).

## Video Optimisation + Voiceover Pipeline

After recordings are complete, this pipeline audits each MP4 for lag frames
and silence gaps, generates timestamped VO cue cards with Claude, and pushes
approved cues to ElevenLabs.

### Full pipeline (correct order)

**Step 1 — Record segments** (see [Automated Demo Recording](#automated-demo-recording)):

```powershell
python engine/run_demo.py --project workershield
```

**Step 2 — Audit recordings** (no video modification yet):

```powershell
python engine/run_audit_batch.py --project workershield --no-trim
```

Review `projects/workershield/audit/consolidated_audit.json`. Re-record any
`RERECORD` segments before continuing:

```powershell
python engine/rerecord_segment.py --project workershield --segment {id} --warmup
```

**Step 3 — Optimise (trim lag frames):**

```powershell
python engine/run_audit_batch.py --project workershield
```

Trimmed files land in `projects/workershield/assets/recordings_optimised/`.
Original recordings are untouched.

**Step 4 — Generate VO cue card scripts:**

```powershell
python engine/generate_vo_scripts.py --project workershield
```

Edit each `projects/workershield/vo_scripts/{segment}_vo_script.yaml` and set
`approved: yes` on every cue you want to use.

**Step 5 — Dry-run then push to ElevenLabs:**

```powershell
python engine/batch_push_audio.py --project workershield --dry-run
python engine/batch_push_audio.py --project workershield
```

Dry run prints total character count and estimated credit cost. The real run
skips cues already on disk — safe to re-run after interruption.

**Step 6 — Compose final video:**

```powershell
python engine/compose_video.py --project workershield
```

---

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
│   ├── launch_chrome.py       # open Chrome with CDP + all demo tabs
│   ├── run_demo.py            # preflight + full-run orchestrator
│   ├── demo_bot.py            # Playwright + OBS -> automated screen recordings
│   ├── rerecord_segment.py    # re-record a single segment by name/id
│   ├── obs_controller.py      # OBS WebSocket v5 wrapper
│   ├── preflight.py           # pre-flight readiness checks (Gradio, Qdrant, OBS, Phoenix, RAGAS)
│   ├── video_auditor.py       # OpenCV lag/scene/silence detector -> AuditReport dict
│   ├── run_audit_batch.py     # batch auditor + optional ffmpeg trim
│   ├── generate_vo_scripts.py # Claude -> timestamped VO cue card YAML
│   ├── batch_push_audio.py    # ElevenLabs batch push with dry-run cost estimate
│   ├── generate_audio.py      # ElevenLabs TTS -> per-segment MP3s (simple path)
│   ├── compose_video.py       # ffmpeg overlay + concat -> final MP4
│   ├── generate_script.py     # Claude -> script.yaml
│   ├── test_obs.py            # smoke test OBS WebSocket connection
│   └── test_elevenlabs.py     # smoke test ElevenLabs API key + voice
├── demo/
│   └── obs_setup_guide.md     # one-time OBS WebSocket setup
├── templates/
│   └── script_template.yaml  # schema reference for script.yaml
├── config/
│   └── voice_config.yaml     # ElevenLabs voice settings
├── projects/
│   └── <project>/
│       ├── script.yaml
│       ├── audit/             # generated audit JSON reports
│       ├── vo_scripts/        # Claude-generated VO cue card YAML files
│       └── assets/
│           ├── recordings/              # raw screen recordings (gitignored)
│           ├── recordings_optimised/    # lag-trimmed copies (gitignored)
│           └── audio/                  # generated narration MP3s (gitignored)
└── output/                    # final composed MP4s (gitignored)
```

## Notes

- Recordings, generated audio, and final videos are gitignored (see
  `.gitignore`) — only the pipeline code, configs, and `.gitkeep` placeholders
  are tracked.
- All file paths in the codebase are built with `pathlib.Path`, so the
  pipeline works the same on Windows, macOS, and Linux.
