"""Only one pass may touch the stick at a time.

Found in testing: running `bambu-drain drain --once` while the systemd service
was mid-pass raced it. Both processes ejected the medium and mounted the same
loop image. The observed symptom was a FileNotFoundError as one deleted a file
the other was about to; the unobserved danger was one calling `gadget insert`
while the other held the image mounted read-write, handing the printer a
filesystem the Pi was writing to.
"""

import multiprocessing
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from bambu_drain import config, drain
from bambu_drain.drain import Drainer
from bambu_drain.ledger import Ledger
from bambu_drain.lock import AlreadyRunning, single_instance


class TestSingleInstance(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.lock = Path(self.tmp.name) / "sub" / "drain.lock"

    def tearDown(self):
        self.tmp.cleanup()

    def test_creates_its_directory(self):
        with single_instance(self.lock):
            self.assertTrue(self.lock.parent.is_dir())

    def test_second_acquisition_raises_rather_than_blocking(self):
        with single_instance(self.lock):
            with self.assertRaises(AlreadyRunning):
                with single_instance(self.lock):
                    self.fail("two holders at once")

    def test_lock_is_released_on_exit(self):
        with single_instance(self.lock):
            pass
        with single_instance(self.lock):
            pass  # no raise

    def test_lock_is_released_when_the_body_raises(self):
        # A crashed pass must not wedge the daemon out of its own lock.
        with self.assertRaises(ValueError):
            with single_instance(self.lock):
                raise ValueError("boom")
        with single_instance(self.lock):
            pass

    def test_it_records_the_holding_pid(self):
        import os
        with single_instance(self.lock):
            self.assertEqual(self.lock.read_text().strip(), str(os.getpid()))


class TestDrainIsSerialised(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.led = Ledger(self.root / "ledger.db")
        self.cfg = config.from_dict({
            "gadget": {"image": str(self.root / "stick.img")},
            "drain": {"idle_minutes": 0, "staging": str(self.root / "staging")},
            "rule": [{"glob": "**/*.mp4", "dest": "timelapse"}],
        })

    def tearDown(self):
        self.led.close()
        self.tmp.cleanup()

    def test_a_second_pass_reports_a_skip_instead_of_racing(self):
        class FakeGadget:
            media_present = True
            def last_host_write(self): return time.time() - 99999
            def idle_seconds(self): return 99999
            def cycle_out(self, settle_seconds=1.0): pass
            def cycle_in(self): pass

        d = Drainer(self.cfg, self.led, FakeGadget())

        # Hold the lock as if the daemon were mid-pass.
        with single_instance(self.cfg.drain_lock_path):
            result = d.run_once()

        self.assertEqual(result["moved"], 0)
        self.assertIn("another bambu-drain pass is running", result["skipped"])

    def test_the_gadget_is_never_touched_while_locked_out(self):
        # The severe failure was a second process re-inserting the medium while
        # the first had it mounted. If we never reach cycle_out, we never can.
        touched = []

        class SpyGadget:
            media_present = True
            def last_host_write(self): return time.time() - 99999
            def idle_seconds(self): return 99999
            def cycle_out(self, settle_seconds=1.0): touched.append("out")
            def cycle_in(self): touched.append("in")

        d = Drainer(self.cfg, self.led, SpyGadget())
        with single_instance(self.cfg.drain_lock_path):
            d.run_once()
        self.assertEqual(touched, [], "locked-out pass must not cycle the medium")


if __name__ == "__main__":
    unittest.main()
