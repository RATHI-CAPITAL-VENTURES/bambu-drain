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
    #
    # The LAST segment is always short — that is how a print ending is detected
    # in the first place — so counting it as full over-estimates the total. With
    # twenty segments that is a rounding error; with two it is 50%, and the
    # output comes out at half the requested length.
    per = count_keyframes(segments[0])
    total = per * (len(segments) - 0.5) if len(segments) > 1 else per
    want = max(1, int(length * fps))
    every = max(1, round(total / want)) if total else 1
    return every, (total // every if every else 0)


def _encode(args: list[str], out: Path, fps: int, crf: int) -> bool:
    """One encode pass with settings identical across all three clips.

    They must match exactly — resolution, codec, pixel format, frame rate — or
    the concat demuxer will refuse to join them.
    """
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostats", *args,
         "-r", str(fps), "-c:v", "libx264", "-crf", str(crf),
         "-preset", "veryfast", "-pix_fmt", "yuv420p", "-an", "-y", str(out)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        log.warning("encode failed (%s): %s", out.name, proc.stderr.strip()[:200])
    return proc.returncode == 0 and out.exists() and out.stat().st_size > 0


def render(segments: list[Path], out: Path, length: float = 60.0,
           fps: int = 30, crf: int = 23,
           head_seconds: float = 5.0, tail_seconds: float = 5.0) -> Path:
    """Sample the segments into a timelapse, bookended by real-time clips.

    Without the bookends the start and end are technically present and
    effectively invisible: at one frame per ~9 seconds of print, the first
    minute — levelling, purge, first layer — lasts about 0.2 seconds.
    """
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
        tmpd = Path(tmp)
        parts: list[Path] = []

        # Head: the opening of the print, at normal speed.
        if head_seconds > 0:
            head = tmpd / "head.mp4"
            if _encode(["-i", str(sorted(segments)[0]), "-t", str(head_seconds)],
                       head, fps, crf):
                parts.append(head)

        body = tmpd / "body.mp4"
        if not _encode(
            ["-skip_frame", "nokey", "-f", "concat", "-safe", "0",
             "-i", str(listing),
             "-vf", f"select='not(mod(n\\,{every}))',setpts=N/{fps}/TB"],
            body, fps, crf,
        ):
            raise RenderError("the timelapse body failed to encode")
        parts.append(body)

        # Tail: the last moments, at normal speed. -sseof seeks from the end.
        if tail_seconds > 0:
            tail = tmpd / "tail.mp4"
            if _encode(["-sseof", f"-{tail_seconds}",
                        "-i", str(sorted(segments)[-1])], tail, fps, crf):
                parts.append(tail)

        staged = tmpd / OUT_NAME
        if len(parts) == 1:
            shutil.move(str(parts[0]), str(staged))
        else:
            joined = tmpd / "parts.txt"
            joined.write_text("".join(f"file '{p}'\n" for p in parts))
            proc = subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostats",
                 "-f", "concat", "-safe", "0", "-i", str(joined),
                 "-c", "copy", "-movflags", "+faststart", "-y", str(staged)],
                capture_output=True, text=True,
            )
            if proc.returncode != 0 or not staged.exists():
                raise RenderError(f"concat failed: {proc.stderr.strip()[:300]}")
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(staged), str(out))

    log.info("rendered %s (%.1f MB) in %.0fs", out.name,
             out.stat().st_size / 1024**2, time.monotonic() - started)
    return out
