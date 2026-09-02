"""The arithmetic that turns hours of chamber footage into a minute.

On the P2S the assembled timelapse goes to INTERNAL storage while the chamber
recording goes to the USB drive, so an archived print has 4.6 hours of 1080p30
and no timelapse. The per-segment thumbnails are not an alternative: there are 22
of them for a 4-hour print, which is under a second of video.
"""

import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "make_timelapse", Path(__file__).resolve().parent.parent / "tools" / "make_timelapse.py")
mt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mt)

# The real print: 22 segments x 750.68s at 30 fps.
REAL_SECONDS = 750.678756 * 22
REAL_FPS = 30.0


class TestSampling(unittest.TestCase):
    def test_the_real_print_lands_near_60_seconds(self):
        every, frames = mt.sampling(REAL_SECONDS, REAL_FPS, 60.0, 30)
        self.assertAlmostEqual(frames / 30, 60, delta=2)

    def test_it_samples_roughly_one_frame_per_9_seconds_of_print(self):
        every, _ = mt.sampling(REAL_SECONDS, REAL_FPS, 60.0, 30)
        self.assertAlmostEqual(every / REAL_FPS, 9.2, delta=1.0)

    def test_a_longer_target_samples_more_densely(self):
        short, _ = mt.sampling(REAL_SECONDS, REAL_FPS, 30.0, 30)
        long_, _ = mt.sampling(REAL_SECONDS, REAL_FPS, 120.0, 30)
        self.assertGreater(short, long_)

    def test_a_short_print_never_samples_below_every_frame(self):
        # 30s of source into a 60s target cannot invent frames.
        every, frames = mt.sampling(30.0, 30.0, 60.0, 30)
        self.assertEqual(every, 1)
        self.assertEqual(frames, 900)

    def test_zero_length_request_still_yields_a_frame(self):
        every, frames = mt.sampling(REAL_SECONDS, REAL_FPS, 0.0, 30)
        self.assertGreaterEqual(every, 1)
        self.assertGreaterEqual(frames, 1)

    def test_thumbnails_would_not_have_worked(self):
        # 22 stills at 30 fps is 0.73 seconds. Recorded because it was the
        # first idea and the arithmetic is the whole answer.
        self.assertLess(22 / 30, 1.0)


class TestLocalFraction(unittest.TestCase):
    def test_a_real_local_file_reads_as_fully_present(self):
        import tempfile
        with tempfile.NamedTemporaryFile(delete=False) as fh:
            fh.write(b"x" * 200_000)
            path = Path(fh.name)
        try:
            self.assertGreaterEqual(mt.local_fraction(path), 0.95)
        finally:
            path.unlink()

    def test_an_empty_file_is_not_a_division_by_zero(self):
        import tempfile
        with tempfile.NamedTemporaryFile(delete=False) as fh:
            path = Path(fh.name)
        try:
            self.assertEqual(mt.local_fraction(path), 1.0)
        finally:
            path.unlink()


if __name__ == "__main__":
    unittest.main()
