"""Generate voiceover cue card scripts from audit reports using the Claude API.

Reads audit JSON + narration from script.yaml, calls Claude to produce
timestamped cue cards as YAML, then saves to projects/{project}/vo_scripts/.

Usage:
    python engine/generate_vo_scripts.py --project workershield
    python engine/generate_vo_scripts.py --project workershield --segment 03_fairdesk

Edit the resulting YAML files and set `approved: yes` on each cue before
running engine/batch_push_audio.py.
"""

import json
import os
import sys
from pathlib import Path

import click
import yaml
from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()

sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

REPO_ROOT = Path(__file__).resolve().parent.parent

MODEL = "claude-sonnet-4-6"

SYSTEM_PROMPT = """\
You are a technical demo video narrator writing voiceover scripts for an AI \
portfolio demo. Write in a calm, confident, practitioner tone. Australian \
context. No hype. Lead with what the system does. Each cue is one or two \
sentences maximum.

Output ONLY a valid YAML document — no preamble, no markdown fences, no \
commentary.\
"""


def _build_user_message(stem: str, seg_data: dict, audit: dict) -> str:
    label = seg_data.get("label", stem)
    duration = audit.get("duration_sec", seg_data.get("duration_seconds", 0))
    narration = (seg_data.get("narration") or "").strip()

    cues_text = "\n".join(
        f"  - {c['time_fmt']} ({c['type']}): {c['label']}"
        for c in audit.get("vo_cues", [])
    ) or "  (none detected)"

    return (
        f"Segment: {label}\n"
        f"Duration: {duration}s\n"
        f"Base narration: {narration}\n\n"
        f"VO cue points detected:\n{cues_text}\n\n"
        f"Write a voiceover script as a series of timestamped cue cards.\n"
        f"Each cue card must have exactly these keys:\n"
        f"  - timestamp: MM:SS\n"
        f"  - cue_type: scene_change | post_lag | silence_gap | intro\n"
        f"  - vo_text: the exact words to speak (1-2 sentences max)\n"
        f"  - duration_est: estimated speaking time in seconds (integer)\n"
        f"  - approved: no\n\n"
        f"Output format (YAML only, no fences):\n"
        f"segment: {stem}\n"
        f"total_duration: {int(duration)}\n"
        f"cues:\n"
        f"  - timestamp: \"00:00\"\n"
        f"    cue_type: intro\n"
        f"    vo_text: \"...\"\n"
        f"    duration_est: 5\n"
        f"    approved: no\n"
    )


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        return "\n".join(lines).strip()
    return text


@click.command()
@click.option("--project", required=True, help="Project slug under projects/, e.g. workershield")
@click.option("--segment", default=None, help="Process a single segment by stem, e.g. 03_fairdesk")
def main(project: str, segment: str | None) -> None:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise click.ClickException("ANTHROPIC_API_KEY not set — add it to .env")

    script_path = REPO_ROOT / "projects" / project / "script.yaml"
    if not script_path.exists():
        raise click.ClickException(f"No script found at {script_path}")

    with open(script_path, encoding="utf-8") as f:
        script = yaml.safe_load(f)

    segments_by_stem: dict[str, dict] = {
        Path(seg["recording_file"]).stem: seg
        for seg in script.get("segments", [])
    }

    audit_dir = REPO_ROOT / "projects" / project / "audit"
    vo_dir = REPO_ROOT / "projects" / project / "vo_scripts"
    vo_dir.mkdir(parents=True, exist_ok=True)

    if segment:
        audit_files = [audit_dir / f"{segment}_audit.json"]
    else:
        audit_files = sorted(
            f for f in audit_dir.glob("*_audit.json") if "consolidated" not in f.name
        )

    if not audit_files:
        raise click.ClickException(
            f"No audit JSON files found in {audit_dir}. "
            "Run run_audit_batch.py first."
        )

    client = Anthropic(api_key=api_key)

    for audit_path in audit_files:
        if not audit_path.exists():
            click.echo(f"  WARNING: {audit_path.name} not found — skipping")
            continue

        stem = audit_path.stem.replace("_audit", "")
        seg_data = segments_by_stem.get(stem, {})

        with open(audit_path, encoding="utf-8") as f:
            audit = json.load(f)

        click.echo(f"  [{stem}] generating VO script...")

        response = client.messages.create(
            model=MODEL,
            max_tokens=2048,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": _build_user_message(stem, seg_data, audit)}],
        )

        raw = "".join(b.text for b in response.content if b.type == "text")
        yaml_text = _strip_fences(raw)

        parsed = yaml.safe_load(yaml_text)
        if not isinstance(parsed, dict) or "cues" not in parsed:
            click.echo(f"  WARNING: unexpected response for {stem} — writing raw output")
            click.echo(f"  Raw:\n{yaml_text[:300]}")
            continue

        out_path = vo_dir / f"{stem}_vo_script.yaml"
        if not yaml_text.endswith("\n"):
            yaml_text += "\n"
        out_path.write_text(yaml_text, encoding="utf-8")
        cue_count = len(parsed.get("cues", []))
        click.echo(f"  [{stem}] {cue_count} cues -> {out_path.relative_to(REPO_ROOT)}")

    click.echo()
    click.echo(
        f"Done. Edit {REPO_ROOT / 'projects' / project / 'vo_scripts'}/*.yaml "
        "— set approved: yes for each cue before running batch_push_audio.py"
    )


if __name__ == "__main__":
    main()
