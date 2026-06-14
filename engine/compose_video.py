"""Compose the final demo video for a project using ffmpeg (via subprocess).

For each segment this script:
  1. Overlays the segment's narration audio onto its recording, padding or
     trimming the audio so it exactly matches the video's duration.
  2. Concatenates all segments in order into output/{project}_demo_final.mp4

Requires ffmpeg and ffprobe to be available on the Windows PATH.

Usage:
    python engine/compose_video.py --project workershield
"""

import shutil
import subprocess
from pathlib import Path

import click
import yaml
from dotenv import load_dotenv

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parent.parent


def load_yaml(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _check_ffmpeg_available() -> None:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        raise click.ClickException(
            "ffmpeg/ffprobe not found on PATH. Install with: winget install ffmpeg"
        )


def get_duration_seconds(path: Path) -> float:
    """Return the duration of a media file in seconds, via ffprobe."""
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    return float(result.stdout.strip())


def combine_segment(video_path: Path, audio_path: Path, out_path: Path) -> None:
    """Overlay audio onto video, padding/trimming the audio to the video's duration."""
    video_duration = get_duration_seconds(video_path)

    # apad appends silence so short audio reaches the video length, then
    # atrim cuts the result to exactly that length (also trims audio that
    # was longer than the video).
    audio_filter = f"[1:a]apad,atrim=0:{video_duration:.3f}[aout]"

    cmd = [
        "ffmpeg", "-y",
        "-i", str(video_path),
        "-i", str(audio_path),
        "-filter_complex", audio_filter,
        "-map", "0:v:0",
        "-map", "[aout]",
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        str(out_path),
    ]
    subprocess.run(cmd, check=True)


def concat_segments(segment_paths: list[Path], out_path: Path) -> None:
    """Concatenate already-combined segment files into the final video."""
    list_file = out_path.parent / f"_{out_path.stem}_concat_list.txt"
    with open(list_file, "w", encoding="utf-8") as f:
        for p in segment_paths:
            f.write(f"file '{p.resolve().as_posix()}'\n")

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(list_file),
        "-c", "copy",
        str(out_path),
    ]
    try:
        subprocess.run(cmd, check=True)
    finally:
        list_file.unlink(missing_ok=True)


@click.command()
@click.option(
    "--project",
    required=True,
    help="Project slug under projects/, e.g. workershield",
)
@click.option(
    "--keep-temp",
    is_flag=True,
    default=False,
    help="Keep the intermediate per-segment files instead of deleting them.",
)
def main(project: str, keep_temp: bool) -> None:
    _check_ffmpeg_available()

    script_path = REPO_ROOT / "projects" / project / "script.yaml"
    if not script_path.exists():
        raise click.ClickException(f"No script found at {script_path}")

    script = load_yaml(script_path)
    recordings_dir = REPO_ROOT / "projects" / project / "assets" / "recordings"
    audio_dir = REPO_ROOT / "projects" / project / "assets" / "audio"
    output_dir = REPO_ROOT / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    tmp_dir = output_dir / f"_tmp_{project}"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    segment_outputs = []
    for segment in script.get("segments", []):
        recording_stem = Path(segment["recording_file"]).stem
        video_path = recordings_dir / segment["recording_file"]
        audio_path = audio_dir / f"{recording_stem}.mp3"

        if not video_path.exists():
            raise click.ClickException(f"Missing recording: {video_path}")
        if not audio_path.exists():
            raise click.ClickException(f"Missing audio: {audio_path}")

        seg_out = tmp_dir / f"{recording_stem}_combined.mp4"
        click.echo(f"Combining segment {segment['id']} ({segment['label']})...")
        combine_segment(video_path, audio_path, seg_out)
        segment_outputs.append(seg_out)

    final_out = output_dir / f"{project}_demo_final.mp4"
    click.echo(f"Concatenating {len(segment_outputs)} segments -> {final_out}")
    concat_segments(segment_outputs, final_out)

    if not keep_temp:
        for f in segment_outputs:
            f.unlink(missing_ok=True)
        tmp_dir.rmdir()

    click.echo(f"Done: {final_out}")


if __name__ == "__main__":
    main()
