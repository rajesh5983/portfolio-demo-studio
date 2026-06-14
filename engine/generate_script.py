"""Generate a project demo script.yaml using the Claude API.

Usage:
    python engine/generate_script.py --project workershield --notes "your talking points"
"""

import os
from pathlib import Path

import click
import yaml
from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_PATH = REPO_ROOT / "templates" / "script_template.yaml"

MODEL = "claude-sonnet-4-6"

SYSTEM_PROMPT = """You are a demo video script writer for software portfolio projects.

Given a project name and a set of talking-point notes, produce a complete
script.yaml describing a short narrated demo video. Output must be a single
YAML document with exactly these top-level fields:

  title: short human-readable title for the demo
  project: the project slug, exactly as given to you
  duration_target_seconds: total target length in seconds (default 300 = 5 minutes)
  segments: a list of segments, each with:
    - id: integer, starting at 1, increasing
    - label: short snake_case label (e.g. intro, feature_demo, wrap_architecture)
    - recording_file: filename following the pattern NN_label.mp4, matching id and label
    - duration_seconds: target length of this segment
    - narration: natural spoken-language narration for this segment, written to
      be read aloud by a text-to-speech engine

Rules:
- Split the demo into 4-6 segments that flow naturally: an intro, one or more
  feature/demo walkthroughs, and a wrap-up that touches on architecture.
- The segments' duration_seconds values should sum to duration_target_seconds.
- Write narration at roughly 2.3 spoken words per second of duration_seconds.
- Base all narration content on the provided notes - do not invent features,
  tech stack details, or claims that are not supported by the notes.
- Output ONLY the raw YAML document. No markdown code fences, no commentary,
  no extra keys.
"""


def _strip_code_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        return "\n".join(lines).strip()
    return text


@click.command()
@click.option("--project", required=True, help="Project slug, e.g. workershield")
@click.option("--notes", required=True, help="Talking points / context for the demo script")
def main(project: str, notes: str) -> None:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise click.ClickException(
            "ANTHROPIC_API_KEY is not set. Add it to your .env file (see .env.example)."
        )

    template_text = TEMPLATE_PATH.read_text(encoding="utf-8")

    user_message = (
        f"Project: {project}\n\n"
        f"Talking points / notes:\n{notes}\n\n"
        f"Reference schema (templates/script_template.yaml):\n{template_text}\n\n"
        f'Write a complete script.yaml for the "{project}" project\'s demo video, '
        f'following the schema above. Set the "project" field to "{project}".'
    )

    client = Anthropic(api_key=api_key)
    response = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )

    raw_text = "".join(
        block.text for block in response.content if block.type == "text"
    )
    yaml_text = _strip_code_fences(raw_text)

    parsed = yaml.safe_load(yaml_text)
    if not isinstance(parsed, dict) or "segments" not in parsed:
        raise click.ClickException(
            "Claude did not return a valid script.yaml document:\n\n" + yaml_text
        )

    out_dir = REPO_ROOT / "projects" / project
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "script.yaml"

    if not yaml_text.endswith("\n"):
        yaml_text += "\n"
    out_path.write_text(yaml_text, encoding="utf-8")

    click.echo(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
