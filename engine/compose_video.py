"""Compose the final demo video for a project using ffmpeg (via subprocess).

For each segment this script:
  1. Reads per-cue audio files and vo_script timestamps to build a
     silence-padded combined audio track matching the recording's duration.
  2. Overlays the combined audio onto the recording.
  3. Concatenates all segments into output/{project}_demo_final.mp4

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
MIN_SILENCE = 0.05  # seconds — skip gaps shorter than this to avoid empty files


def load_yaml(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _check_ffmpeg_available() -> None:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        raise click.ClickException(
            "ffmpeg/ffprobe not found on PATH. Install with: winget install ffmpeg"
        )


def get_duration_seconds(path: Path, extra: float = 0.0) -> float:
    """Return the duration of a media file in seconds, via ffprobe.

    Pass extra=0.1 for video files to account for codec delay when building
    audio tracks that must cover the full playback window.
    """
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    return float(result.stdout.strip()) + extra


def parse_timestamp(ts: str) -> float:
    """Parse 'MM:SS' timestamp string to seconds."""
    parts = ts.split(":")
    return int(parts[0]) * 60 + int(parts[1])


def generate_silence(duration: float, out_path: Path) -> None:
    """Generate a silent stereo MP3 of the given duration."""
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i", "anullsrc=r=44100:cl=stereo",
        "-t", f"{duration:.3f}",
        "-q:a", "2",
        str(out_path),
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def concat_audio_files(piece_paths: list[Path], out_path: Path) -> None:
    """Concatenate audio pieces via concat filter (handles format differences)."""
    n = len(piece_paths)
    inputs: list[str] = []
    for p in piece_paths:
        inputs.extend(["-i", str(p)])

    filter_graph = (
        "".join(f"[{i}:a]" for i in range(n))
        + f"concat=n={n}:v=0:a=1[aout]"
    )

    cmd = [
        "ffmpeg", "-y",
        *inputs,
        "-filter_complex", filter_graph,
        "-map", "[aout]",
        "-ar", "44100",
        "-ac", "2",
        "-q:a", "2",
        str(out_path),
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def build_audio_track(
    segment_id: str,
    vo_script: dict,
    audio_dir: Path,
    video_duration: float,
    temp_dir: Path,
) -> Path:
    """Build a silence-padded audio track matching video_duration from per-cue MP3s."""
    cues = vo_script["cues"]
    pieces: list[Path] = []
    sil_files: list[Path] = []
    current_pos = 0.0
    desc_parts: list[str] = []

    for idx, cue in enumerate(cues):
        cue_file = audio_dir / f"{segment_id}_{idx:02d}.mp3"
        if not cue_file.exists():
            raise click.ClickException(f"Missing cue audio: {cue_file}")

        cue_duration = get_duration_seconds(cue_file)
        cue_time = parse_timestamp(cue["timestamp"])

        # FIX A: cue timestamp at/near video end — shift it back so it fits.
        # All last-cue timestamps equal total_duration in the vo_script, which
        # lands at or past the actual recording end.
        if cue_time >= video_duration - 2.0:
            ideal_start = video_duration - cue_duration - 0.5
            adjusted = max(ideal_start, current_pos)
            click.echo(
                f"[{segment_id}] Cue {idx:02d} shifted "
                f"{cue_time:.2f}s -> {adjusted:.2f}s to fit within video"
            )
            cue_time = adjusted

        gap = cue_time - current_pos
        if gap >= MIN_SILENCE:
            sil_path = temp_dir / f"{segment_id}_sil_{idx:02d}.mp3"
            generate_silence(gap, sil_path)
            pieces.append(sil_path)
            sil_files.append(sil_path)
            desc_parts.append(f"silence {gap:.1f}s")

        pieces.append(cue_file)
        current_pos = cue_time + cue_duration
        desc_parts.append(f"cue_{idx:02d} ({cue_duration:.1f}s)")

    # FIX B: handle negative remaining without crashing
    remaining = video_duration - current_pos
    if remaining >= MIN_SILENCE:
        sil_path = temp_dir / f"{segment_id}_sil_end.mp3"
        generate_silence(remaining, sil_path)
        pieces.append(sil_path)
        sil_files.append(sil_path)
        desc_parts.append(f"silence {remaining:.1f}s")
    elif remaining < -0.1:
        click.echo(
            f"[{segment_id}] Warning: audio {-remaining:.1f}s longer than video "
            f"— -t will trim"
        )

    click.echo(f"[{segment_id}] " + " + ".join(desc_parts))

    combined_path = temp_dir / f"{segment_id}_combined_audio.mp3"
    if len(pieces) == 1:
        shutil.copy2(pieces[0], combined_path)
    else:
        concat_audio_files(pieces, combined_path)

    for p in sil_files:
        p.unlink(missing_ok=True)

    return combined_path


def mix_audio_onto_video(
    video_path: Path, audio_path: Path, out_path: Path, video_duration: float
) -> None:
    """Overlay combined audio track onto video (video stream copied, audio re-encoded).

    Uses -t rather than -shortest so the output is locked to the exact recording
    duration even when the last cue audio extends slightly past the video end.
    """
    cmd = [
        "ffmpeg", "-y",
        "-i", str(video_path),
        "-i", str(audio_path),
        "-map", "0:v",
        "-map", "1:a",
        "-c:v", "copy",
        "-c:a", "aac",
        "-t", f"{video_duration:.3f}",  # FIX C: explicit duration, not -shortest
        str(out_path),
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def concat_segments(segment_paths: list[Path], out_path: Path) -> None:
    """Concatenate per-segment MP4s into the final video."""
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
    help="Keep intermediate temp files instead of deleting them.",
)
@click.option(
    "--segment",
    "only_segment",
    default=None,
    help="Process only this segment ID (e.g. 03_fairdesk). Other segments are "
         "taken from existing output; final video is still rebuilt.",
)
def main(project: str, keep_temp: bool, only_segment: str | None) -> None:
    _check_ffmpeg_available()

    script_path = REPO_ROOT / "projects" / project / "script.yaml"
    if not script_path.exists():
        raise click.ClickException(f"No script found at {script_path}")

    script = load_yaml(script_path)
    recordings_dir = REPO_ROOT / "projects" / project / "assets" / "recordings"
    audio_dir = REPO_ROOT / "projects" / project / "assets" / "audio"
    vo_scripts_dir = REPO_ROOT / "projects" / project / "vo_scripts"

    output_root = REPO_ROOT / "output" / project
    segments_dir = output_root / "segments"
    temp_dir = output_root / "temp"
    segments_dir.mkdir(parents=True, exist_ok=True)
    temp_dir.mkdir(parents=True, exist_ok=True)

    final_out = REPO_ROOT / "output" / f"{project}_demo_final.mp4"

    segment_outputs: list[Path] = []
    for segment in script.get("segments", []):
        segment_id = Path(segment["recording_file"]).stem  # e.g. "01_intro"
        seg_out = segments_dir / f"{segment_id}.mp4"

        if only_segment and segment_id != only_segment:
            if seg_out.exists():
                segment_outputs.append(seg_out)
            else:
                click.echo(f"[{segment_id}] Skipped (no existing output — run without --segment to build it)")
            continue

        video_path = recordings_dir / segment["recording_file"]
        vo_script_path = vo_scripts_dir / f"{segment_id}_vo_script.yaml"

        if not video_path.exists():
            raise click.ClickException(f"Missing recording: {video_path}")
        if not vo_script_path.exists():
            raise click.ClickException(f"Missing vo_script: {vo_script_path}")

        vo_script = load_yaml(vo_script_path)
        n_cues = len(vo_script["cues"])
        # FIX D: add 0.1s codec-delay buffer so audio covers the full frame window
        video_duration = get_duration_seconds(video_path, extra=0.1)

        click.echo(
            f"[{segment_id}] Video: {video_duration:.0f}s | "
            f"Audio cues: {n_cues} | Building audio track..."
        )

        combined_audio = build_audio_track(
            segment_id, vo_script, audio_dir, video_duration, temp_dir
        )

        click.echo(f"[{segment_id}] Merging audio onto video...")
        mix_audio_onto_video(video_path, combined_audio, seg_out, video_duration)
        combined_audio.unlink(missing_ok=True)

        click.echo(f"[{segment_id}] Done -> {seg_out}")
        segment_outputs.append(seg_out)

    click.echo(f"\nConcatenating {len(segment_outputs)} segments -> {final_out}")
    concat_segments(segment_outputs, final_out)

    if not keep_temp:
        shutil.rmtree(temp_dir, ignore_errors=True)

    final_duration = get_duration_seconds(final_out)
    click.echo(f"\nDone: {final_out}")
    click.echo(f"Total duration: {final_duration:.1f}s ({final_duration / 60:.1f} min)")


if __name__ == "__main__":
    main()
