"""Chamber footage and thumbnails are raw material, not something to keep.

`discard_after_timelapse` deletes them once the print's timelapse is verified
on the Mac. Everything here is about the "once": the footage is the only source
a timelapse can be rebuilt from, so it must outlive every way the timelapse
could still be lost.
"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from bambu_drain import config, ship
from bambu_drain.ledger import Ledger
from bambu_drain.ship import Shipper

S = "Benchy_10_02_26"


class DiscardCase(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.staging = self.root / "staging"
        self.led = Ledger(self.root / "ledger.db")
        self.cfg = config.from_dict({
            "gadget": {"image": str(self.root / "s.img")},
            "drain": {"staging": str(self.staging)},
            "ship": {"host": "mac", "dest": "/arch"},
            "render": {"enabled": False},
            "rule": [{"glob": "**/*.mp4", "dest": "video"}],
        })
        self.rsynced = []
        self.remote_sha = {}          # dest_rel -> what the Mac reports
        self.n = 0

    def tearDown(self):
        self.led.close()
        self.tmp.cleanup()

    def add(self, rel, discard=False, session=S, body=b"data", ends=False):
        self.n += 1
        sha = f"sha{self.n}"
        path = self.staging / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        self.led.record_drained(sha, path.name, rel, len(body), path,
                                session=session, src_mtime=1000.0 + self.n,
                                ends_session=ends, discard=discard)
        self.remote_sha[rel] = sha
        return sha, path

    def print_with(self, timelapse="timelapse-reconstructed.mp4"):
        self.model = self.add(f"prints/{S}/Benchy.gcode.3mf")
        self.segs = [self.add(f"prints/{S}/video/ipcam-record.{i}.mp4", discard=True,
                              ends=(i == 2)) for i in range(3)]
        self.thumb = self.add(f"prints/{S}/thumbnails/ipcam-record.0.jpg", discard=True)
        self.tl = self.add(f"prints/{S}/{timelapse}") if timelapse else None

    def ship(self):
        def ssh(_self, *command):
            cmd = command[0]
            if cmd.startswith("shasum"):
                for rel, sha in self.remote_sha.items():
                    if cmd.endswith(f"/arch/{rel}") or cmd.endswith(f"/arch/{rel}'"):
                        return mock.Mock(returncode=0, stdout=f"{sha}  x\n")
                return mock.Mock(returncode=1, stdout="")
            return mock.Mock(returncode=0, stdout="/Users/x")

        def run(argv, **kw):
            self.rsynced.append(argv[-1].split("/arch/")[1])
            return mock.Mock(returncode=0, stderr="")

        with mock.patch.object(Shipper, "reachable", return_value=True), \
             mock.patch.object(Shipper, "_ssh", ssh), \
             mock.patch.object(ship.subprocess, "run", run):
            return Shipper(self.cfg, self.led).run_once()


class TestDiscard(DiscardCase):
    def test_only_the_model_and_the_timelapse_reach_the_mac(self):
        self.print_with()
        result = self.ship()
        self.assertEqual(sorted(self.rsynced),
                         [f"prints/{S}/Benchy.gcode.3mf",
                          f"prints/{S}/timelapse-reconstructed.mp4"])
        self.assertEqual((result["shipped"], result["discarded"], result["pending"]),
                         (2, 4, 0))
        for _, path in self.segs + [self.thumb]:
            self.assertFalse(path.exists(), f"{path.name} should be gone from staging")
        self.assertEqual(self.led.unshipped(), [])
        self.assertIn("discard", [r["kind"] for r in self.led.recent_events()])

    def test_the_printers_own_timelapse_counts_too(self):
        self.print_with(timelapse="timelapse.mp4")
        self.assertEqual(self.ship()["discarded"], 4)

    def test_footage_survives_a_timelapse_that_did_not_verify(self):
        # Deleting the segments here would leave nothing to rebuild from.
        self.print_with()
        self.remote_sha[f"prints/{S}/timelapse-reconstructed.mp4"] = "corrupted"
        result = self.ship()
        self.assertEqual(result["discarded"], 0)
        for _, path in self.segs + [self.thumb]:
            self.assertTrue(path.exists())
        self.assertFalse(any("video/" in r for r in self.rsynced),
                         "waiting for the timelapse, not shipping the footage")
        self.assertEqual(result["pending"], 5)

        # ...and goes the moment a later pass gets the timelapse across.
        self.remote_sha[f"prints/{S}/timelapse-reconstructed.mp4"] = self.tl[0]
        self.assertEqual(self.ship()["discarded"], 4)

    def test_a_timelapse_lost_to_a_power_cut_releases_the_footage(self):
        # Truncated in staging: marked shipped-unverified and never retried.
        # Footage waiting on it would sit in staging until the drain stopped.
        self.print_with()
        self.tl[1].write_bytes(b"")
        result = self.ship()
        self.assertIn("local_corrupt", [r["kind"] for r in self.led.recent_events()])
        self.assertEqual(result["discarded"], 0)
        self.assertEqual(len([r for r in self.rsynced if "video/" in r]), 3)
        self.assertEqual(self.led.unshipped(), [])

    def test_a_timelapse_that_vanished_from_staging_releases_the_footage(self):
        self.print_with()
        self.tl[1].unlink()
        self.ship()
        self.assertEqual(len([r for r in self.rsynced if "video/" in r]), 3)

    def test_a_timelapse_caught_by_a_discard_rule_still_ships(self):
        # A config with the timelapse rule removed: `**/*.mp4` takes it.
        self.add(f"prints/{S}/video/ipcam-record.0.mp4", discard=True, ends=True)
        self.add(f"prints/{S}/timelapse.mp4", discard=True)
        result = self.ship()
        self.assertEqual((result["shipped"], result["discarded"], result["pending"]),
                         (1, 1, 0))
        self.assertEqual(self.rsynced, [f"prints/{S}/timelapse.mp4"])

    def test_a_print_with_no_timelapse_keeps_its_footage(self):
        # Too few segments, a failed render, a session that timed out: the
        # footage is then the only record of the print.
        self.print_with(timelapse=None)
        result = self.ship()
        self.assertEqual(result["discarded"], 0)
        self.assertEqual(result["shipped"], 5)
        self.assertEqual(len([r for r in self.rsynced if "video/" in r]), 3)

    def test_an_empty_timelapse_is_not_a_timelapse(self):
        self.print_with(timelapse=None)
        self.add(f"prints/{S}/timelapse.mp4", body=b"")
        self.assertEqual(self.ship()["discarded"], 0)

    def test_files_drained_before_the_flag_existed_ship_as_before(self):
        self.add(f"prints/{S}/video/ipcam-record.0.mp4", discard=False, ends=True)
        self.add(f"prints/{S}/timelapse.mp4")
        result = self.ship()
        self.assertEqual((result["shipped"], result["discarded"]), (2, 0))

    def test_a_discarded_file_is_still_known_and_not_counted_as_archived(self):
        self.print_with()
        self.ship()
        sha = self.segs[0][0]
        self.assertTrue(self.led.known(sha), "a re-drained copy must be recognised")
        self.assertEqual(self.led.stats()["files_total"], 2)
        self.assertEqual(self.led.modal_size("ipcam-record%.mp4", minimum=3), 4)


class TestWhatCountsAsATimelapse(DiscardCase):
    def test_a_model_named_timelapse_is_not_one(self):
        # `LIKE '%timelapse%.mp4'` matched every segment of this print, which
        # would now mean deleting its footage with no timelapse made.
        s = "Timelapse_stand_10_02_26"
        for i in range(3):
            self.add(f"prints/{s}/video/ipcam-record.{i}.mp4", discard=True,
                     session=s, ends=(i == 2))
        self.assertEqual(self.led.timelapses(s), [])
        self.assertIn(s, self.led.sessions_needing_render())
        self.assertEqual(self.ship()["discarded"], 0)

    def test_a_renamed_duplicate_still_counts(self):
        self.add(f"prints/{S}/timelapse-0a1b2c3d.mp4")
        self.assertEqual(len(self.led.timelapses(S)), 1)
        self.assertFalse(self.led.timelapse_verified(S))

    def test_a_session_with_a_timelapse_needs_no_render(self):
        self.print_with(timelapse="timelapse.mp4")
        self.assertNotIn(S, self.led.sessions_needing_render())


class TestMigration(unittest.TestCase):
    def test_a_ledger_from_before_the_flag_gains_the_columns(self):
        import sqlite3
        with TemporaryDirectory() as d:
            path = Path(d) / "ledger.db"
            db = sqlite3.connect(path)
            db.execute("CREATE TABLE files (sha256 TEXT PRIMARY KEY, src_name TEXT "
                       "NOT NULL, dest_rel TEXT NOT NULL, size INTEGER NOT NULL, "
                       "staging_path TEXT, drained_at REAL NOT NULL, shipped_at REAL, "
                       "verified_at REAL)")
            db.execute("INSERT INTO files VALUES ('a','a.mp4','video/a.mp4',1,NULL,1,1,1)")
            db.commit()
            db.close()
            led = Ledger(path)
            self.addCleanup(led.close)
            row = led.db.execute("SELECT discard, discarded_at FROM files").fetchone()
            self.assertEqual((row["discard"], row["discarded_at"]), (0, None))
            self.assertEqual(led.stats()["files_total"], 1)


if __name__ == "__main__":
    unittest.main()
