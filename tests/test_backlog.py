"""Draining a large backlog must not take all afternoon.

A 4-hour print left 5.4 GB across 22 segments. Draining it took four passes
over ~25 minutes, and the reason was subtle: **our own deletions bump the
backing image's mtime**, which is the exact signal used to detect the printer
writing. Every pass therefore reset its own idle clock, so each chunk waited the
full 5-minute gate again.

The gate itself was never the problem — it held for the entire print, correctly.
"""

import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from bambu_drain import config
from bambu_drain.drain import Drainer
from bambu_drain.ledger import Ledger


class Gadget:
    media_present = True

    def __init__(self, quiet_seconds):
        self.mtime = time.time() - quiet_seconds

    def last_host_write(self):
        return self.mtime

    def cycle_out(self, settle_seconds=1.0):
        pass

    def cycle_in(self):
        pass


def make(tmp, **drain):
    base = {"idle_minutes": 5, "staging": str(tmp / "staging")}
    base.update(drain)
    return config.from_dict({
        "gadget": {"image": str(tmp / "stick.img")},
        "drain": base,
        "rule": [{"glob": "**/*.mp4", "dest": "timelapse"}],
    })


class TestOwnWritesDoNotResetTheClock(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "stick.img").write_bytes(b"x")
        self.led = Ledger(self.root / "ledger.db")

    def tearDown(self):
        self.led.close()
        self.tmp.cleanup()

    def test_a_pass_of_our_own_does_not_look_like_the_printer_waking_up(self):
        g = Gadget(quiet_seconds=3600)
        d = Drainer(make(self.root), self.led, g)
        self.assertGreater(d.quiet_seconds(), 3500)

        # Simulate a finished pass: our deletions moved the image mtime to now,
        # and we recorded both that and the printer's pre-pass mtime.
        d._printer_mtime = g.mtime
        g.mtime = time.time()
        d._own_mtime = g.mtime

        self.assertGreater(
            d.quiet_seconds(), 3500,
            "our own write must not reset the printer-idle clock",
        )
        self.assertIsNone(d.blocked_reason(), "the next backlog chunk may proceed")

    def test_a_real_printer_write_does_reset_it(self):
        g = Gadget(quiet_seconds=3600)
        d = Drainer(make(self.root), self.led, g)
        d._printer_mtime = g.mtime
        g.mtime = time.time()
        d._own_mtime = g.mtime

        # Printer writes: mtime moves again, no longer matching our recorded one.
        g.mtime = time.time() + 0.5
        self.assertLess(d.quiet_seconds(), 60)
        self.assertIn("printer active", d.blocked_reason())


class TestEjectBudgetScalesWithConfidence(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "stick.img").write_bytes(b"x")
        self.led = Ledger(self.root / "ledger.db")

    def tearDown(self):
        self.led.close()
        self.tmp.cleanup()

    def _budget(self, quiet):
        cfg = make(self.root, max_eject_seconds=120,
                   long_idle_minutes=20, max_eject_seconds_long_idle=900)
        return Drainer(cfg, self.led, Gadget(quiet)).eject_budget()

    def test_just_past_the_gate_stays_conservative(self):
        # Five minutes of quiet is the minimum bar for believing a print ended.
        self.assertEqual(self._budget(6 * 60), 120)

    def test_long_quiet_earns_a_long_window(self):
        # An hour of silence is not a pause between layers.
        self.assertEqual(self._budget(60 * 60), 900)

    def test_the_boundary(self):
        self.assertEqual(self._budget(20 * 60 - 1), 120)
        self.assertEqual(self._budget(20 * 60 + 1), 900)

    def test_a_54gb_backlog_fits_in_one_long_window(self):
        # 5.4 GB to local disk at a conservative 30 MB/s is ~180s. The old
        # 120s budget chopped that into four passes; 900s does not.
        seconds_needed = 5.4 * 1024 / 30
        self.assertLess(seconds_needed, self._budget(60 * 60))
        self.assertGreater(seconds_needed, 120, "the old budget really was too small")


if __name__ == "__main__":
    unittest.main()
