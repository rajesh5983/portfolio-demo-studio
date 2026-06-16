"""Batch video auditor for all recordings in a project.

Usage:
    python engine/run_audit_batch.py --project workershield
    python engine/run_audit_batch.py --project workershield --no-trim
    python engine/run_audit_batch.py --project workershield --segment 03_fairdesk

Actions:
    1. Finds all MP4s in projects/{project}/assets/recordings/
    2. Runs VideoAuditor on each recording
    3. Saves individual JSON to projects/{project}/audit/{segment}_audit.json
    4. Saves consolidated JSON to projects/{project}/audit/consolidated_audit.json
    5. Prints a summary table
    6. Trims lag frames via ffmpeg (unless --no-trim), saving optimised videos to
       projects/{project}/assets/recordings_optimised/
"""

import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import click

from video_auditor import VideoAuditor

sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

REPO_ROOT = Path(__file__).resolve().parent.parent


def trim_lag_segments(
    video_path: Path,
    lag_segments: list[dict],
    duration: float,
    output_path: Path,
) -> None:
    """Remove lag segments from video via ffmpeg trim+concat filter."""
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg not found on PATH — install with: winget install ffmpeg")

    # Build list of keeper intervals (everything that is NOT a lag).
    keeps: list[tuple[float, float]] = []
    cursor = 0.0
    for lag in sorted(lag_segments, key=lambda s: s["start_sec"]):
        if lag["start_sec"] > cursor + 0.05:
            keeps.append((cursor, lag["start_sec"]))
        cursor = lag["end_sec"]
    if cursor < duration - 0.05:
        keeps.append((cursor, duration))

    if not keeps:
        return

    parts = [
        f"[0:v]trim={s:.3f}:{e:.3f},setpts=PTS-STARTPTS[v{i}]"
        for i, (s, e) in enumerate(keeps)
    ]
    concat_inputs = "".join(f"[v{i}]" for i in range(len(parts)))
    filter_complex = ";".join(parts) + f";{concat_inputs}concat=n={len(parts)}:v=1:a=0[vout]"

    output_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y",
        "-i", str(video_path),
        "-filter_complex", filter_complex,
        "-map", "[vout]",
        "-c:v", "libx264", "-crf", "18",
        str(output_path),
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def print_summary_table(results: list[tuple[str, dict]]) -> None:
    col_seg = 20
    header = (
        f"{'Segment':<{col_seg}} | {'Duration':>8} | {'Lag':>7} | {'Cuts':>4} | Action"
    )
    sep = "-" * col_seg + "-+-" + "-" * 8 + "-+-" + "-" * 7 + "-+-" + "-" * 4 + "-+-----------"
    click.echo(header)
    click.echo(sep)
    for stem, report in results:
        s = report["summary"]
        m, sec = divmod(int(report["duration_sec"]), 60)
        dur_fmt = f"{m}:{sec:02d}"
        lag = f"{s['total_lag_sec']:.1f}s"
        cuts = str(s["scene_cut_count"])
        action = s["recommended_action"]
        icon = "✅" if action == "KEEP" else "⚠️ " if action == "TRIM" else "❌"
        click.echo(f"{stem:<{col_seg}} | {dur_fmt:>8} | {lag:>7} | {cuts:>4} | {action} {icon}")


@click.command()
@click.option("--project", required=True, help="Project slug under projects/, e.g. workershield")
@click.option(
    "--no-trim",
    "no_trim",
    is_flag=True,
    default=False,
    help="Audit only — do not modify any video files",
)
@click.option("--segment", default=None, help="Audit a single segment by stem, e.g. 03_fairdesk")
def main(project: str, no_trim: bool, segment: str | None) -> None:
    recordings_dir = REPO_ROOT / "projects" / project / "assets" / "recordings"
    audit_dir = REPO_ROOT / "projects" / project / "audit"
    optimised_dir = REPO_ROOT / "projects" / project / "assets" / "recordings_optimised"
    audit_dir.mkdir(parents=True, exist_ok=True)

    if not recordings_dir.exists():
        raise click.ClickException(f"Recordings directory not found: {recordings_dir}")

    # Resolve which MP4(s) to process.
    if segment:
        all_mp4s = list(recordings_dir.glob("*.mp4"))
        mp4s = [p for p in all_mp4s if p.stem == segment or segment in p.stem]
        if not mp4s:
            available = ", ".join(p.stem for p in sorted(all_mp4s))
            raise click.ClickException(
                f"No MP4 matching '{segment}' in {recordings_dir}. Available: {available}"
            )
    else:
        mp4s = sorted(recordings_dir.glob("*.mp4"))

    if not mp4s:
        raise click.ClickException(f"No MP4 files found in {recordings_dir}")

    click.echo(f"Auditing {len(mp4s)} recording(s) for project '{project}'...")
    click.echo()

    results: list[tuple[str, dict]] = []

    for mp4 in mp4s:
        click.echo(f"  [{mp4.stem}] analysing...", nl=False)
        auditor = VideoAuditor(mp4)
        report = auditor.run()

        report_path = audit_dir / f"{mp4.stem}_audit.json"
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

        action = report["summary"]["recommended_action"]
        lag_sec = report["summary"]["total_lag_sec"]
        click.echo(f" {action} (lag={lag_sec}s, cuts={report['summary']['scene_cut_count']})")

        results.append((mp4.stem, report))

        if not no_trim and action == "TRIM" and report["lag_segments"]:
            out = optimised_dir / mp4.name
            click.echo(f"  [{mp4.stem}] trimming lag -> {out.relative_to(REPO_ROOT)}")
            try:
                trim_lag_segments(mp4, report["lag_segments"], report["duration_sec"], out)
                click.echo(f"  [{mp4.stem}] saved optimised ({out.stat().st_size // 1024} KB)")
            except (subprocess.CalledProcessError, RuntimeError) as exc:
                click.echo(f"  [{mp4.stem}] WARNING: trim failed: {exc}")

    # Consolidated report.
    totals = {
        "total_lag_sec": round(sum(r["summary"]["total_lag_sec"] for _, r in results), 2),
        "keep_count": sum(1 for _, r in results if r["summary"]["recommended_action"] == "KEEP"),
        "trim_count": sum(1 for _, r in results if r["summary"]["recommended_action"] == "TRIM"),
        "rerecord_count": sum(1 for _, r in results if r["summary"]["recommended_action"] == "RERECORD"),
    }
    consolidated = {
        "project": project,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "totals": totals,
        "segments": {stem: report for stem, report in results},
    }
    consolidated_path = audit_dir / "consolidated_audit.json"
    consolidated_path.write_text(json.dumps(consolidated, indent=2), encoding="utf-8")

    click.echo()
    print_summary_table(results)
    click.echo()
    click.echo(
        f"Total lag: {totals['total_lag_sec']}s | "
        f"KEEP: {totals['keep_count']} | "
        f"TRIM: {totals['trim_count']} | "
        f"RERECORD: {totals['rerecord_count']}"
    )
    click.echo(f"Reports: {audit_dir.relative_to(REPO_ROOT)}/")
    if no_trim:
        click.echo("(--no-trim: no video files modified)")


if __name__ == "__main__":
    main()
