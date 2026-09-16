"""The archive migration groups the way the daemon does — replayed on the
night of 2026-09-15, which the daemon got wrong in two places.

What the ship loop actually produced, in four folders:

    2026-09-15_1939/                       one thumbnail, held 6 h, shipped alone
    2026-09-15_1939_07_Vault_Door_plate_1/ the sliced file and a 24-minute attempt
    2026-09-15_2006/                       the 4.4-hour redo, plus a reconstruction
    2026-09-16_0024/                       the printer's own timelapse, and a thumbnail

What it should have been, and what the migration must now produce from the same
mtimes: two folders.
"""

import datetime as dt
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.regroup_archive import plan, sessions  # noqa: E402

FULL = 240_000_000
SHORT = 140_000_000


class TestTheNightOf0915(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.src = Path(self.tmp.name) / "archive"
        self.files: list[Path] = []
        t = dt.datetime(2026, 9, 15, 19, 39, 20).timestamp()
        self.t0 = t
        # The first attempt: thumbnail 2 s BEFORE the sliced file.
        self._put("x/ipcam-record.42.jpg", t, 20_000)
        self._put("x/07 Vault Door_plate_1.gcode.3mf", t + 2, 2_400_000)
        self._put("x/ipcam-record.42.mp4", t + 15 * 60, FULL)
        self._put("x/ipcam-record.43.jpg", t + 15 * 60, 14_000)
        self._put("x/ipcam-record.43.mp4", t + 24 * 60 + 19, SHORT)     # 20:03:39, closer
        # The redo, 141 s after the short segment, no new sliced file.
        r = t + 24 * 60 + 19 + 141                                       # 20:06:00
        self.redo_start = r
        for i in range(44, 64):                                          # to 00:06
            self._put(f"x/ipcam-record.{i}.jpg", r, 15_000)
            self._put(f"x/ipcam-record.{i}.mp4", r + 12 * 60, FULL)
            r += 12 * 60
        # The teardown flush, to the hundredth of a second.
        e = dt.datetime(2026, 9, 16, 0, 24, 52).timestamp()
        self._put("x/video_2026-09-16_08-22-55.jpg", e + 0.62, 20_400)
        self._put("x/ipcam-record.64.mp4", e + 0.72, 17_800_000)         # short, closer
        self._put("x/video_2026-09-16_08-22-55_mini.jpg", e + 0.75, 5_290)
        self._put("x/video_2026-09-16_08-22-55.mp4", e + 0.80, 13_200_000)

    def tearDown(self):
        self.tmp.cleanup()

    def _put(self, rel, mtime, size):
        p = self.src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("wb") as fh:
            fh.truncate(size)
        os.utime(p, (mtime, mtime))
        self.files.append(p)

    def test_two_prints_two_folders(self):
        sess = sessions(self.files, 45 * 60)
        self.assertEqual(set(sess.values()),
                         {"2026-09-15_1939_07_Vault_Door_plate_1", "2026-09-15_2006"})

    def test_the_thumbnail_that_beat_the_sliced_file_is_in_the_named_folder(self):
        sess = sessions(self.files, 45 * 60)
        self.assertEqual(sess[self.src / "x/ipcam-record.42.jpg"],
                         "2026-09-15_1939_07_Vault_Door_plate_1")

    def test_the_timelapse_and_its_thumbnails_stay_with_the_redo(self):
        sess = sessions(self.files, 45 * 60)
        for n in ("video_2026-09-16_08-22-55.jpg", "ipcam-record.64.mp4",
                  "video_2026-09-16_08-22-55_mini.jpg", "video_2026-09-16_08-22-55.mp4"):
            self.assertEqual(sess[self.src / "x" / n], "2026-09-15_2006", n)

    def test_the_plan_puts_the_timelapse_at_the_redo_root(self):
        moves, _, _ = plan(self.src, self.src, 45 * 60)
        targets = {f.name: t.relative_to(self.src) for f, t in moves}
        self.assertEqual(targets["video_2026-09-16_08-22-55.mp4"],
                         Path("prints/2026-09-15_2006/timelapse.mp4"))
        self.assertEqual(targets["07 Vault Door_plate_1.gcode.3mf"],
                         Path("prints/2026-09-15_1939_07_Vault_Door_plate_1/"
                              "07 Vault Door_plate_1.gcode.3mf"))


if __name__ == "__main__":
    unittest.main()
