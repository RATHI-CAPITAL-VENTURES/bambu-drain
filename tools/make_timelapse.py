#!/usr/bin/env python3
"""Build a timelapse from the chamber-camera segments of a print.

Why this exists: on the P2S the assembled timelapse is written to the printer's
INTERNAL storage, while the chamber recording goes to the USB drive. So a print
archived by bambu-drain has hours of 1080p30 footage and no timelapse.

The footage is far better raw material than the per-segment thumbnails, of which
there is one every ~12 minutes — 22 stills for a 4-hour print, under a second of
video. This samples the actual recording instead.

    tools/make_timelapse.py prints/2026-09-02_0945
    tools/make_timelapse.py prints/2026-09-02_0945 --length 90

The output is named `timelapse-reconstructed.mp4`, deliberately NOT
`timelapse.mp4`: one is what the printer made, the other is what we assembled,
and conflating them makes the archive lie about its own provenance.

Requires ffmpeg. Files stored in iCloud must be downloaded first — see --check.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

OUT_NAME = "timelapse-reconstructed.mp4"


def ffprobe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=r_frame_rate,width,height",
         "-show_entries", "format=duration", "-of", "json", str(path)],
        capture_output=True, text=True, timeout=120,
    )
    if out.returncode != 0:
        raise RuntimeError(f"ffprobe failed on {path.name}: {out.stderr.strip()}")
    return json.loads(out.stdout)


def sampling(total_seconds: float, src_fps: float, length: float, out_fps: int):
    """(every_nth_frame, resulting_frame_count) for a target output length.

    Keeping one frame per N of source and renumbering them is what turns 4.6
    hours into a minute. N is rounded, so the result lands near the target
    rather than exactly on it — a 4-hour print cannot always be divided into
    exactly 1800 frames.
    """
    target_frames = max(1, int(length * out_fps))
    every_nth = max(1, round(total_seconds * src_fps / target_frames))
    # max(1, …): rounding every_nth UP can make the division land a hair below
    # 1.0, which floors to a reported frame count of zero for a clip that will
    # in fact contain one frame.
    return every_nth, max(1, int(total_seconds * src_fps / every_nth))


def local_fraction(path: Path) -> float:
    """How much of an iCloud-backed file is actually on disk."""
    apparent = path.stat().st_size
    if apparent == 0:
        return 1.0
    on_disk = path.stat().st_blocks * 512
    return min(1.0, on_disk / apparent)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("print_dir", type=Path, help="a prints/<session>/ directory")
    ap.add_argument("--length", type=float, default=60.0,
                    help="target output length in seconds (default 60)")
    ap.add_argument("--fps", type=int, default=30, help="output fps (default 30)")
    ap.add_argument("--crf", type=int, default=20, help="x264 quality (default 20)")
    ap.add_argument("--check", action="store_true",
                    help="report readiness and the plan, then stop")
    args = ap.parse_args()

    if not shutil.which("ffmpeg"):
        print("error: ffmpeg not found", file=sys.stderr)
        return 1

    d = args.print_dir.expanduser()
    video = d / "video"
    if not video.is_dir():
        print(f"error: no video/ directory under {d}", file=sys.stderr)
        return 1

    segments = sorted(video.glob("*.mp4"))
    if not segments:
        print(f"error: no segments in {video}", file=sys.stderr)
        return 1

    # iCloud evicts large files. ffmpeg will simply hang on a placeholder, so
    # check before starting rather than after twenty minutes of nothing.
    not_local = [s for s in segments if local_fraction(s) < 0.95]
    total_bytes = sum(s.stat().st_size for s in segments)

    if not_local:
        pct = 100 * (len(segments) - len(not_local)) / len(segments)
        print(f"  {len(segments)} segments, {total_bytes / 1024**3:.2f} GB")
        print(f"  {len(not_local)} still in iCloud — {pct:.0f}% downloaded")
        print(r"  Run:  find <print_dir>/video -name '*.mp4' -exec brctl download {} \;")
        print("  (then re-run; ffprobe blocks indefinitely on a placeholder)")
        return 0 if args.check else 1

    probe = ffprobe(segments[0])
    num, den = probe["streams"][0]["r_frame_rate"].split("/")
    src_fps = float(num) / float(den)
    per_segment = float(probe["format"]["duration"])
    total_seconds = per_segment * len(segments)
    every_nth, actual_frames = sampling(total_seconds, src_fps,
                                        args.length, args.fps)

    print(f"  {len(segments)} segments, {total_bytes / 1024**3:.2f} GB")
    print(f"  source     {probe['streams'][0]['width']}x{probe['streams'][0]['height']} "
          f"@ {src_fps:g} fps, {total_seconds / 3600:.2f} h total")
    print(f"  sampling   every {every_nth} frames "
          f"(one per {every_nth / src_fps:.1f}s of print)")
    print(f"  output     ~{actual_frames} frames = "
          f"{actual_frames / args.fps:.0f}s at {args.fps} fps")
    if args.check:
        return 0

    out = d / OUT_NAME
    with tempfile.TemporaryDirectory() as tmp:
        listing = Path(tmp) / "segments.txt"
        listing.write_text("".join(f"file '{s.resolve()}'\n" for s in segments))

        # Sampling straight from the concat input avoids writing a 5 GB
        # intermediate. setpts renumbers the kept frames so they play at a
        # constant rate instead of inheriting the source timestamps.
        # -stats writes a progress line per half-second with a carriage
        # return, which is right at a terminal and 80 KB of noise when the
        # output is piped or captured. Only ask for it when someone is watching.
        progress = ["-stats"] if sys.stderr.isatty() else ["-nostats"]
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", *progress,
            "-f", "concat", "-safe", "0", "-i", str(listing),
            "-vf", f"select='not(mod(n\\,{every_nth}))',setpts=N/{args.fps}/TB",
            "-r", str(args.fps), "-c:v", "libx264", "-crf", str(args.crf),
            "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            "-an", "-y", str(out),
        ]
        print(f"\n  encoding -> {out.name}")
        r = subprocess.run(cmd)
        if r.returncode != 0:
            print("error: ffmpeg failed", file=sys.stderr)
            return 1

    mb = out.stat().st_size / 1024**2
    print(f"\n  done: {out}")
    print(f"  {mb:.1f} MB, {actual_frames / args.fps:.0f}s")
    if mb > 100:
        print(f"  (large for its length — pass --crf 26 for roughly half the size)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
