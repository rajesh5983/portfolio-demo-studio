"""Video frame analyser for detecting lag, scene cuts, and silence gaps.

Uses OpenCV for frame reading and comparison, scipy for signal smoothing.
No ffmpeg subprocess — pure Python/C extension processing.

Screen recording note:
  demo_bot.py uses instant wheel/click events (≤ 1 frame of visual change)
  followed by wait_for_timeout() holds.  At the default 0.25 s sampling rate,
  all transition frames fall between samples, so every sampled frame lands on
  a static hold.  This means even "good" recordings report high lag totals —
  this is expected and reflects how demo bots work, not a capture failure.
  The detected lag and silence periods are the primary output: they become VO
  cue insertion points for generate_vo_scripts.py.

Usage (from run_audit_batch.py):
    from video_auditor import VideoAuditor
    report = VideoAuditor(Path("recording.mp4")).run()
"""

from dataclasses import asdict, dataclass
from pathlib import Path

import cv2
import numpy as np
from scipy.signal import medfilt

# Seconds between sampled frames. 0.25 s → 4 samples/sec.
_SAMPLE_INTERVAL = 0.25

# Downscale resolution for frame comparisons.
_ANALYSIS_WIDTH = 320
_ANALYSIS_HEIGHT = 180

# Median-filter kernel for smoothing the raw per-frame diff signal.
# Kernel 3 = 0.75 s window — eliminates single-frame glitches without
# smoothing away genuine 1 s scroll/navigation transitions.
_SMOOTH_KERNEL = 3

# Internal threshold for silence detection (mean diff < this = low activity).
# Typical scrolling produces mean diffs of 10–30; static holds land at 0–5.
_SILENCE_MEAN_MAX = 8.0


@dataclass
class Segment:
    start_sec: float
    end_sec: float
    duration_sec: float
    type: str  # "lag" | "silence"


class VideoAuditor:
    """Analyse a single MP4 and return a structured audit report."""

    def __init__(self, video_path: Path) -> None:
        self.video_path = Path(video_path)
        cap = cv2.VideoCapture(str(self.video_path))
        if not cap.isOpened():
            raise ValueError(f"Cannot open video: {self.video_path}")

        self.fps: float = cap.get(cv2.CAP_PROP_FPS) or 30.0
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        self.duration: float = total_frames / self.fps
        self.width: int = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.height: int = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        cap.release()

        self._frames: list[tuple[float, np.ndarray]] | None = None
        self._metrics: tuple[list[float], np.ndarray, np.ndarray] | None = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_frames(self) -> list[tuple[float, np.ndarray]]:
        if self._frames is None:
            self._frames = self._read_sampled_frames()
        return self._frames

    def _read_sampled_frames(self) -> list[tuple[float, np.ndarray]]:
        cap = cv2.VideoCapture(str(self.video_path))
        sample_step = max(1, int(self.fps * _SAMPLE_INTERVAL))
        frames: list[tuple[float, np.ndarray]] = []
        idx = 0
        while True:
            if idx % sample_step == 0:
                ret, frame = cap.read()
                if not ret:
                    break
                small = cv2.resize(frame, (_ANALYSIS_WIDTH, _ANALYSIS_HEIGHT))
                frames.append((idx / self.fps, small))
            else:
                if not cap.grab():
                    break
            idx += 1
        cap.release()
        return frames

    def _get_metrics(self) -> tuple[list[float], np.ndarray, np.ndarray]:
        """Return (timestamps, mean_diffs, p95_diffs) for all frame pairs.

        Computed once and cached.  Two metrics are returned so that lag
        detection (uses p95) and silence detection (uses mean) can share
        a single frame-reading pass.
        """
        if self._metrics is not None:
            return self._metrics

        frames = self._get_frames()
        n = len(frames) - 1
        if n <= 0:
            self._metrics = ([], np.array([]), np.array([]))
            return self._metrics

        timestamps: list[float] = []
        means = np.empty(n, dtype=np.float64)
        p95s = np.empty(n, dtype=np.float64)

        for i in range(n):
            diff = cv2.absdiff(frames[i][1], frames[i + 1][1])
            flat = diff.flatten().astype(np.float64)
            timestamps.append(frames[i + 1][0])
            means[i] = np.mean(flat)
            p95s[i] = np.percentile(flat, 95)

        self._metrics = (timestamps, means, p95s)
        return self._metrics

    # ------------------------------------------------------------------
    # Public detection methods
    # ------------------------------------------------------------------

    def detect_lag_segments(self, threshold: float = 0.98, min_duration: float = 1.5) -> list[Segment]:
        """Detect frozen/lag frames using 95th-percentile pixel diff.

        threshold=0.98 → max p95 of (1–0.98)×255 ≈ 5.1.  This separates
        truly frozen or spinner-only frames (p95 ≈ 0–4) from scrolling and
        navigation events (p95 ≈ 50–200) in screen recordings.
        """
        max_diff = (1.0 - threshold) * 255.0
        timestamps, _, p95s = self._get_metrics()
        if not timestamps:
            return []

        smoothed = medfilt(p95s, kernel_size=_SMOOTH_KERNEL)
        frozen = smoothed <= max_diff
        return _runs_to_segments(timestamps, frozen, min_duration, "lag")

    def detect_scene_cuts(self, threshold: float = 0.4) -> list[float]:
        """Detect scene changes via grayscale histogram Bhattacharyya distance.

        For in-site navigation (same colour palette), distances are typically
        0.1–0.3; cross-site navigations (e.g. GitHub → Qdrant dark UI) exceed
        0.4.  Lower threshold here if same-site navigation cuts are needed.
        """
        frames = self._get_frames()
        cuts: list[float] = []
        prev_hist: np.ndarray | None = None

        for ts, frame in frames:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            hist = cv2.calcHist([gray], [0], None, [256], [0.0, 256.0])
            cv2.normalize(hist, hist)
            if prev_hist is not None:
                dist = cv2.compareHist(prev_hist, hist, cv2.HISTCMP_BHATTACHARYYA)
                if dist > threshold:
                    cuts.append(round(ts, 2))
            prev_hist = hist

        return cuts

    def detect_silence_gaps(self, min_duration: float = 2.0) -> list[Segment]:
        """Detect extended low-activity periods using mean pixel diff.

        Intentional demo holds (presenter waiting for viewer to read) and model
        inference waits both produce low mean diffs.  These are reported as
        silence gaps — good VO narration insertion points.
        """
        timestamps, means, _ = self._get_metrics()
        if not timestamps:
            return []

        smoothed = medfilt(means, kernel_size=_SMOOTH_KERNEL)
        silent = smoothed <= _SILENCE_MEAN_MAX
        return _runs_to_segments(timestamps, silent, min_duration, "silence")

    # ------------------------------------------------------------------
    # VO cue generation
    # ------------------------------------------------------------------

    def generate_vo_cues(
        self,
        scene_cuts: list[float],
        lag_segments: list[Segment],
        silence_segments: list[Segment],
    ) -> list[dict]:
        """Merge detection results into VO insertion cue points.

        Deduplicates cues within 1.0 s of each other to avoid stacking
        multiple cue types at the same timestamp.
        """
        cues: list[dict] = []
        seen: list[float] = []

        def add(time_sec: float, cue_type: str, label: str) -> None:
            if any(abs(time_sec - t) < 1.0 for t in seen):
                return
            seen.append(time_sec)
            cues.append({
                "time_sec": round(time_sec, 2),
                "time_fmt": self._fmt(time_sec),
                "type": cue_type,
                "label": label,
            })

        add(0.0, "intro", "Opening — set context before action starts")

        for ts in scene_cuts:
            add(ts, "scene_change", "New section — start next VO sentence here")

        for seg in lag_segments:
            add(min(seg.end_sec + 1.0, self.duration), "post_lag",
                "Demo resumed — explain what just loaded")

        for seg in silence_segments:
            add(seg.start_sec, "silence_gap",
                f"Fill {seg.duration_sec:.0f}s gap with narration or cut")

        return sorted(cues, key=lambda c: c["time_sec"])

    # ------------------------------------------------------------------
    # Master runner
    # ------------------------------------------------------------------

    def run(self) -> dict:
        """Run all detectors and return the full AuditReport dict."""
        lag_segs = self.detect_lag_segments()
        scene_cuts = self.detect_scene_cuts()
        silence_gaps = self.detect_silence_gaps()
        vo_cues = self.generate_vo_cues(scene_cuts, lag_segs, silence_gaps)

        total_lag = sum(s.duration_sec for s in lag_segs)
        total_silence = sum(s.duration_sec for s in silence_gaps)

        # Recommendations calibrated for automated screen recordings.
        # demo_bot.py makes instant wheel/click events (< 1 frame of visual
        # change) followed by wait_for_timeout() holds.  At 0.25 s sampling,
        # all transitions are missed and every sample lands on a static hold —
        # so even a "good" recording shows near-100 % static time.  This is
        # expected, not a bug.  The detected lag periods ARE the VO cue points.
        #
        #   RERECORD — recording is too short to contain real content
        #              (< 10 s); likely a capture failure
        #   TRIM     — any single lag segment > 5 s detected; use VO narration
        #              or ffmpeg trim to handle long inference/hold periods
        #   KEEP     — all pauses are < 5 s; normal short-pause pacing
        if self.duration < 10.0:
            action = "RERECORD"
        elif any(s.duration_sec > 5.0 for s in lag_segs):
            action = "TRIM"
        else:
            action = "KEEP"

        return {
            "filename": self.video_path.name,
            "duration_sec": round(self.duration, 2),
            "fps": round(self.fps, 2),
            "resolution": f"{self.width}x{self.height}",
            "lag_segments": [asdict(s) for s in lag_segs],
            "scene_cuts": scene_cuts,
            "silence_gaps": [asdict(s) for s in silence_gaps],
            "vo_cues": vo_cues,
            "summary": {
                "total_lag_sec": round(total_lag, 2),
                "lag_count": len(lag_segs),
                "scene_cut_count": len(scene_cuts),
                "silence_count": len(silence_gaps),
                "total_silence_sec": round(total_silence, 2),
                "recommended_action": action,
            },
        }

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    @staticmethod
    def _fmt(seconds: float) -> str:
        m = int(seconds) // 60
        s = int(seconds) % 60
        return f"{m:02d}:{s:02d}"


# ------------------------------------------------------------------
# Module-level helper (shared by all VideoAuditor instances)
# ------------------------------------------------------------------

def _runs_to_segments(
    timestamps: list[float],
    mask: np.ndarray,
    min_duration: float,
    seg_type: str,
) -> list[Segment]:
    """Convert a boolean mask over frame-pair timestamps into Segment objects."""
    segments: list[Segment] = []
    in_run = False
    run_start = 0.0

    for i, (ts, active) in enumerate(zip(timestamps, mask)):
        if active and not in_run:
            in_run = True
            run_start = timestamps[i - 1] if i > 0 else ts
        elif not active and in_run:
            dur = ts - run_start
            if dur >= min_duration:
                segments.append(Segment(
                    start_sec=round(run_start, 2),
                    end_sec=round(ts, 2),
                    duration_sec=round(dur, 2),
                    type=seg_type,
                ))
            in_run = False

    if in_run and timestamps:
        dur = timestamps[-1] - run_start
        if dur >= min_duration:
            segments.append(Segment(
                start_sec=round(run_start, 2),
                end_sec=round(timestamps[-1], 2),
                duration_sec=round(dur, 2),
                type=seg_type,
            ))

    return segments
