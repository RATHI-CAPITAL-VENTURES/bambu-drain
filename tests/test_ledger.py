import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from bambu_drain.ledger import Ledger


class TestLedger(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.led = Ledger(Path(self.tmp.name) / "sub" / "ledger.db")

    def tearDown(self):
        self.led.close()
        self.tmp.cleanup()

    def test_creates_parent_directory(self):
        self.assertTrue((Path(self.tmp.name) / "sub").is_dir())

    def test_known_gates_the_delete(self):
        self.assertFalse(self.led.known("abc"))
        self.led.record_drained("abc", "a.mp4", "timelapse/2026/08/a.mp4", 10, Path("/s/a.mp4"))
        self.assertTrue(self.led.known("abc"))

    def test_record_drained_is_idempotent(self):
        for _ in range(3):
            self.led.record_drained("abc", "a.mp4", "t/a.mp4", 10, Path("/s/a.mp4"))
        self.assertEqual(self.led.stats()["files_total"], 1)

    def test_unshipped_then_shipped(self):
        self.led.record_drained("abc", "a.mp4", "t/a.mp4", 10, Path("/s/a.mp4"))
        self.led.record_drained("def", "b.mp4", "t/b.mp4", 20, Path("/s/b.mp4"))
        self.assertEqual(len(self.led.unshipped()), 2)
        self.assertEqual(self.led.stats()["bytes_pending_ship"], 30)

        self.led.record_shipped("abc", verified=True)
        self.led.clear_staging("abc")
        pending = self.led.unshipped()
        self.assertEqual([r["sha256"] for r in pending], ["def"])
        self.assertEqual(self.led.stats()["files_total"], 2)

    def test_unverified_ship_records_no_verified_at(self):
        self.led.record_drained("abc", "a.mp4", "t/a.mp4", 10, Path("/s/a.mp4"))
        self.led.record_shipped("abc", verified=False)
        row = next(iter(self.led.db.execute("SELECT * FROM files")))
        self.assertIsNotNone(row["shipped_at"])
        self.assertIsNone(row["verified_at"])

    def test_last_closer_is_the_latest_ending_file_not_the_latest_file(self):
        self.led.record_drained("seg", "seg.mp4", "prints/s/video/seg.mp4", 5,
                                Path("/s/seg.mp4"), session="s", src_mtime=100.0,
                                ends_session=True)
        self.led.record_drained("mini", "mini.jpg", "prints/s/thumbnails/mini.jpg",
                                1, Path("/s/mini.jpg"), session="s", src_mtime=100.03)
        self.assertEqual(self.led.last_print_file()["src_mtime"], 100.03)
        self.assertEqual(self.led.last_closer()["src_mtime"], 100.0)
        self.assertIsNone(Ledger(Path(self.tmp.name) / "empty.db").last_closer())

    def test_session_opened_at_is_the_earliest_source_mtime(self):
        self.led.record_drained("a", "a", "prints/s/a", 1, Path("/s/a"),
                                session="s", src_mtime=200.0)
        self.led.record_drained("b", "b", "prints/s/b", 1, Path("/s/b"),
                                session="s", src_mtime=150.0)
        self.assertEqual(self.led.session_opened_at("s"), 150.0)
        self.assertIsNone(self.led.session_opened_at("nope"))

    def test_reassign_moves_a_record_between_sessions(self):
        self.led.record_drained("a", "a.jpg", "prints/old/thumbnails/a.jpg", 1,
                                Path("/st/prints/old/thumbnails/a.jpg"),
                                session="old", src_mtime=1.0)
        self.led.reassign("a", "new", "prints/new/thumbnails/a.jpg",
                          Path("/st/prints/new/thumbnails/a.jpg"))
        row = self.led.session_files("new")[0]
        self.assertEqual(row["dest_rel"], "prints/new/thumbnails/a.jpg")
        self.assertEqual(row["staging_path"], "/st/prints/new/thumbnails/a.jpg")
        self.assertEqual(self.led.session_files("old"), [])

    def test_events_are_ordered_newest_first(self):
        self.led.event("drain", "one")
        self.led.event("ship", "two")
        kinds = [r["kind"] for r in self.led.recent_events()]
        self.assertEqual(kinds[0], "ship")


if __name__ == "__main__":
    unittest.main()
