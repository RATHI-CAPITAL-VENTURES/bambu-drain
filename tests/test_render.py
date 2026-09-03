"""Rebuilding a timelapse on the Pi, from footage that is still there.

The printer often keeps the assembled timelapse on its own internal storage, so
a print arrives as hours of chamber video and nothing to watch. Rebuilding on
the Mac instead would mean pulling gigabytes back out of iCloud — slow, and the
thing that has already cost time twice.
"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from bambu_drain import config, render
from bambu_drain.ledger import Ledger

MODAL = 252_000_000


class TestPlan(unittest.TestCase):
    """Sampling arithmetic, in KEYFRAMES rather than frames.

    A 750s segment carries ~774 keyframes — roughly one per second — measured on
    the real files.
    """

    def _plan(self, segments, per_segment_keyframes, length=60.0, fps=30):
        with mock.patch.object(render, "count_keyframes",
                               return_value=per_segment_keyframes):
            return render.plan([Path(f"s{i}.mp4") for i in range(segments)], length, fps)

    def test_a_44_hour_print_lands_near_60_seconds(self):
        every, frames = self._plan(21, 774)
        self.assertAlmostEqual(frames / 30, 60, delta=3)

    def test_it_samples_roughly_every_ninth_keyframe(self):
        every, _ = self._plan(21, 774)
        self.assertAlmostEqual(every, 9, delta=1)

    def test_a_short_print_never_samples_below_every_keyframe(self):
        every, _ = self._plan(1, 100)
        self.assertEqual(every, 1)

    def test_no_segments_is_not_a_crash(self):
        self.assertEqual(render.plan([], 60, 30), (1, 0))

    def test_a_segment_with_no_keyframes_does_not_divide_by_zero(self):
        every, frames = self._plan(3, 0)
        self.assertEqual(every, 1)


class TestSelection(unittest.TestCase):
    """Which sessions get rendered. All three conditions matter."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.led = Ledger(self.root / "l.db")

    def tearDown(self):
        self.led.close()
        self.tmp.cleanup()

    def _file(self, session, name, dest, staged=True, ends=False):
        p = self.root / name
        p.write_bytes(b"x")
        self.led.record_drained(f"sha-{session}-{name}", name, dest, 1,
                                p if staged else Path("/gone"),
                                session=session, src_mtime=1.0, ends_session=ends)
        if not staged:
            self.led.record_shipped(f"sha-{session}-{name}", verified=True)
            self.led.clear_staging(f"sha-{session}-{name}")

    def test_a_closed_staged_session_with_no_timelapse_is_selected(self):
        self._file("S", "ipcam-record.x.1.mp4", "prints/S/video/a.mp4")
        self._file("S", "ipcam-record.x.2.mp4", "prints/S/video/b.mp4", ends=True)
        self.assertEqual(self.led.sessions_needing_render(), ["S"])

    def test_an_unfinished_print_is_not_rendered(self):
        # Rendering a print that is still running would be worse than useless.
        self._file("S", "ipcam-record.x.1.mp4", "prints/S/video/a.mp4")
        self.assertEqual(self.led.sessions_needing_render(), [])

    def test_a_session_that_already_has_a_timelapse_is_skipped(self):
        self._file("S", "ipcam-record.x.1.mp4", "prints/S/video/a.mp4", ends=True)
        self._file("S", "video_x.mp4", "prints/S/timelapse.mp4")
        self.assertEqual(self.led.sessions_needing_render(), [])

    def test_an_already_rendered_session_is_not_rendered_twice(self):
        self._file("S", "ipcam-record.x.1.mp4", "prints/S/video/a.mp4", ends=True)
        self._file("S", render.OUT_NAME, f"prints/S/{render.OUT_NAME}")
        self.assertEqual(self.led.sessions_needing_render(), [])

    def test_a_session_whose_segments_already_shipped_is_skipped(self):
        # Nothing left on the Pi to render from.
        self._file("S", "ipcam-record.x.1.mp4", "prints/S/video/a.mp4",
                   staged=False, ends=True)
        self.assertEqual(self.led.sessions_needing_render(), [])

    def test_staged_segments_exclude_thumbnails_and_the_timelapse(self):
        self._file("S", "ipcam-record.x.1.mp4", "prints/S/video/a.mp4", ends=True)
        self._file("S", "ipcam-record.x.1.jpg", "prints/S/thumbnails/a.jpg")
        segs = self.led.staged_segments("S")
        self.assertEqual(len(segs), 1)
        self.assertTrue(str(segs[0]).endswith(".mp4"))

    def test_the_destination_folder_is_derived_from_a_sibling(self):
        self._file("S", "ipcam-record.x.1.mp4", "prints/2026-09-02_1613/video/a.mp4",
                   ends=True)
        self.assertEqual(self.led.session_dest_dir("S"), "prints/2026-09-02_1613")


class TestRenderGuards(unittest.TestCase):
    def test_no_segments_raises_rather_than_producing_an_empty_file(self):
        with self.assertRaises(render.RenderError):
            render.render([], Path("/tmp/x.mp4"))

    def test_missing_ffmpeg_is_reported_clearly(self):
        with mock.patch.object(render, "available", return_value=False):
            with self.assertRaises(render.RenderError) as e:
                render.render([Path("a.mp4")], Path("/tmp/x.mp4"))
            self.assertIn("ffmpeg", str(e.exception))


if __name__ == "__main__":
    unittest.main()
