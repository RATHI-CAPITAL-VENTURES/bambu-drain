"""A short segment means the print stopped.

The chamber recording rotates at a fixed size, so this is a *physical* signal
rather than an inferential one — unlike the time gap, which cannot work (a real
print boundary measured 18.2 minutes while gaps within a print reach 14), and
unlike the timelapse, which is often written to the printer's internal storage
and never reaches us at all.

Measured across 61 real segments:

    full segments      240.2 - 240.4 MB   (100% of modal, every one)
    print endings      29.6, 72.2, 112.2, 160.8, 190.5, 218.5 MB
    nothing at all     between 92% and 99%
"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from bambu_drain import config
from bambu_drain.config import Rule
from bambu_drain.drain import Drainer, _size_family
from bambu_drain.ledger import Ledger

MODAL = 252_000_000                      # ~240.3 MB
SEGMENT = Rule("**/*.mp4", "video", group="print", ends_session_if_short=0.95)
PLAIN = Rule("**/*.mp4", "video", group="print")
LAPSE = Rule("timelapse/video_*.mp4", "", group="print", rename="timelapse.mp4",
             ends_session=True)


class TestSizeFamily(unittest.TestCase):
    def test_segments_of_a_print_share_a_family(self):
        self.assertEqual(_size_family(Path("ipcam-record.2026-09-02_21-45-11.3.mp4")),
                         "ipcam-record%.mp4")

    def test_two_prints_share_the_same_family(self):
        a = _size_family(Path("ipcam-record.2026-09-02_21-45-11.3.mp4"))
        b = _size_family(Path("ipcam-record.2026-09-03_08-17-24.46.mp4"))
        self.assertEqual(a, b)


class TestBoundaryDetection(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        root = Path(self.tmp.name)
        self.led = Ledger(root / "ledger.db")
        self.cfg = config.from_dict({
            "gadget": {"image": str(root / "s.img")},
            "drain": {"staging": str(root / "staging")},
            "rule": [{"glob": "**/*.mp4", "dest": "video", "group": "print"}],
        })
        self.d = Drainer(self.cfg, self.led, object())
        # Enough full segments for the modal size to be meaningful.
        for i in range(8):
            self.led.record_drained(f"sha{i}", f"ipcam-record.x.{i}.mp4",
                                    f"p/{i}.mp4", MODAL, Path(f"/s/{i}.mp4"))

    def tearDown(self):
        self.led.close()
        self.tmp.cleanup()

    def _short(self, pct):
        return self.d.closes_session(
            SEGMENT, Path("ipcam-record.x.99.mp4"), int(MODAL * pct))

    def test_a_full_segment_does_not_end_a_print(self):
        self.assertFalse(self._short(1.0))

    def test_the_real_endings_are_all_caught(self):
        # 29.6, 72.2, 112.2, 160.8, 190.5, 218.5 MB against a 240.3 MB modal.
        for pct in (0.12, 0.30, 0.47, 0.67, 0.79, 0.91):
            self.assertTrue(self._short(pct), f"{pct:.0%} should end a print")

    def test_the_gap_between_full_and_short_is_wide(self):
        # Nothing real landed between 92% and 99%; the threshold sits there.
        self.assertTrue(self._short(0.92))
        self.assertFalse(self._short(0.99))

    def test_it_does_nothing_without_enough_history(self):
        # A fresh install must not guess a rotation size from two files.
        fresh = Ledger(Path(self.tmp.name) / "fresh.db")
        d = Drainer(self.cfg, fresh, object())
        fresh.record_drained("a", "ipcam-record.x.1.mp4", "p", MODAL, Path("/s/a"))
        self.assertFalse(
            d.closes_session(SEGMENT, Path("ipcam-record.x.2.mp4"), 1000))
        fresh.close()

    def test_a_rule_without_the_setting_is_unaffected(self):
        self.assertFalse(self.d.closes_session(PLAIN, Path("ipcam-record.x.9.mp4"), 1))

    def test_an_explicit_ends_session_rule_still_wins(self):
        self.assertTrue(self.d.closes_session(LAPSE, Path("video_x.mp4"), MODAL))


if __name__ == "__main__":
    unittest.main()


class TestModalSizeIsRobust(unittest.TestCase):
    """Real segments differ by kilobytes, so an exact-value mode finds nothing.

    The first implementation grouped by exact byte count and returned None for
    61 perfectly good segments, silently disabling boundary detection.
    """

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.led = Ledger(Path(self.tmp.name) / "l.db")

    def tearDown(self):
        self.led.close()
        self.tmp.cleanup()

    def _add(self, i, size):
        self.led.record_drained(f"s{i}", f"ipcam-record.x.{i}.mp4", "p", size,
                                Path(f"/s/{i}"))

    def test_sizes_that_never_repeat_still_yield_a_modal_size(self):
        for i, mb in enumerate([240.2, 240.3, 240.4, 240.3, 240.2, 240.4, 240.3]):
            self._add(i, int(mb * 1024 * 1024))
        m = self.led.modal_size("ipcam-record%.mp4")
        self.assertIsNotNone(m, "kilobyte variation must not defeat detection")
        self.assertAlmostEqual(m / 1024 / 1024, 240.3, delta=0.3)

    def test_short_segments_do_not_drag_the_estimate_down(self):
        for i, mb in enumerate([240.2, 240.3, 240.4, 240.3, 240.2, 240.4]):
            self._add(i, int(mb * 1024 * 1024))
        for j, mb in enumerate([29.6, 112.2]):          # two print endings
            self._add(100 + j, int(mb * 1024 * 1024))
        m = self.led.modal_size("ipcam-record%.mp4")
        self.assertGreater(m / 1024 / 1024, 200, "median must stay on the full segments")

    def test_too_little_history_returns_none(self):
        self._add(1, 1000)
        self.assertIsNone(self.led.modal_size("ipcam-record%.mp4"))
