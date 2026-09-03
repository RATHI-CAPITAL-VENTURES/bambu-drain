"""Build a timelapse from staged chamber segments, on the Pi, before shipping.

On the P2S the assembled timelapse is often written to the printer's INTERNAL
storage and never reaches the drive, so a print arrives as hours of chamber
footage and nothing to watch. This fills that gap while the footage is still on
the Pi — reconstructing on the Mac instead would mean pulling gigabytes back out
of iCloud, which is slow, and which we have already been bitten by twice.

**Keyframes only.** Measured on the real hardware, one 750 s segment:

    full decode + select every 275th frame   294 s
    -skip_frame nokey, every 9th keyframe     66 s

A 4.4-hour print is 21 segments, so that is 103 minutes against 23. The
segments carry roughly one keyframe per second — 774 in 750 s — which is far
more than the ~1800 frames a 60-second timelapse needs, and an I-frame is a
full-quality picture rather than a reconstructed one.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

log = logging.getLogger("bambu_drain.render")

OUT_NAME = "timelapse-reconstructed.mp4"
# Roughly one keyframe per second of print, so this is the sampling interval in
# keyframes rather than frames.
_KEYFRAME_PROBE_TIMEOUT = 120


class RenderError(RuntimeError):
    pass


def available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def count_keyframes(path: Path) -> int:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-skip_frame", "nokey", "-select_streams", "v:0",
         "-count_frames", "-show_entries", "stream=nb_read_frames",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, timeout=_KEYFRAME_PROBE_TIMEOUT,
    )
    try:
        return int(proc.stdout.strip())
    except (ValueError, AttributeError):
        return 0


def plan(segments: list[Path], length: float, fps: int) -> tuple[int, int]:
    """(every_nth_keyframe, expected_frames) for a target output length."""
    if not segments:
        return 1, 0
    # Probe one segment and scale: they are the same length by construction, and
    # probing 21 of them costs a minute for no new information.
    per = count_keyframes(segments[0])
    total = per * len(segments)
    want = max(1, int(length * fps))
    every = max(1, round(total / want)) if total else 1
    return every, (total // every if every else 0)


def render(segments: list[Path], out: Path, length: float = 60.0,
           fps: int = 30, crf: int = 23) -> Path:
    """Concatenate the segments and sample them into a timelapse."""
    if not available():
        raise RenderError("ffmpeg/ffprobe not installed")
    if not segments:
        raise RenderError("no segments to render")

    every, expected = plan(segments, length, fps)
    log.info("rendering %d segments -> every %d keyframes, ~%d frames (%.0fs)",
             len(segments), every, expected, expected / fps)

    started = time.monotonic()
    with tempfile.TemporaryDirectory() as tmp:
        listing = Path(tmp) / "segs.txt"
        listing.write_text("".join(f"file '{s.resolve()}'\n" for s in sorted(segments)))
        staged = Path(tmp) / OUT_NAME
        proc = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostats",
                # Only decode I-frames. This is the whole performance story.
                "-skip_frame", "nokey",
                "-f", "concat", "-safe", "0", "-i", str(listing),
                "-vf", f"select='not(mod(n\\,{every}))',setpts=N/{fps}/TB",
                "-r", str(fps), "-c:v", "libx264", "-crf", str(crf),
                "-preset", "veryfast", "-pix_fmt", "yuv420p",
                "-movflags", "+faststart", "-an", "-y", str(staged),
            ],
            capture_output=True, text=True,
        )
        if proc.returncode != 0 or not staged.exists():
            raise RenderError(f"ffmpeg failed: {proc.stderr.strip()[:400]}")
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(staged), str(out))

    log.info("rendered %s (%.1f MB) in %.0fs", out.name,
             out.stat().st_size / 1024**2, time.monotonic() - started)
    return out
