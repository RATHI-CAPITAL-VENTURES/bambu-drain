import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from bambu_drain import config
from bambu_drain.drain import Drainer, staging_usage_bytes
from bambu_drain.ledger import Ledger


class FakeGadget:
    """Models the real primitive: the backing image's mtime.

    `quiet_seconds()` derives idleness from this and must additionally discount
    writes the drainer itself caused, so the fake exposes the mtime rather than
    a pre-computed idle figure.
    """

    media_present = True

    def __init__(self, idle):
        self._idle = idle

    def last_host_write(self):
        return time.time() - self._idle

    def idle_seconds(self):
        return self._idle

    def cycle_out(self, settle_seconds=1.0):
        pass

    def cycle_in(self):
        pass


def make_cfg(tmp: Path, staging_max_gb=64.0, idle_minutes=5.0):
    return config.from_dict({
        "gadget": {"image": str(tmp / "stick.img")},
        "drain": {
            "idle_minutes": idle_minutes,
            "staging": str(tmp / "staging"),
            "staging_max_gb": staging_max_gb,
        },
        "rule": [{"glob": "**/*.mp4", "dest": "timelapse"}],
    })


class TestGating(unittest.TestCase):
    """The drain loop must refuse in exactly two situations, and it must say
    which one, because 'nothing happened' is the failure mode this project is
    most likely to die of."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.led = Ledger(self.root / "ledger.db")

    def tearDown(self):
        self.led.close()
        self.tmp.cleanup()

    def test_blocks_while_printer_is_writing(self):
        cfg = make_cfg(self.root)
        d = Drainer(cfg, self.led, FakeGadget(idle=10))
        self.assertIn("printer active", d.blocked_reason())

    def test_allows_once_idle(self):
        cfg = make_cfg(self.root)
        d = Drainer(cfg, self.led, FakeGadget(idle=99999))
        self.assertIsNone(d.blocked_reason())

    def test_blocks_when_staging_is_over_budget(self):
        # Draining now would trade the printer's full disk for the Pi's.
        staging = self.root / "staging"
        staging.mkdir()
        (staging / "big.mp4").write_bytes(b"x" * 2048)
        cfg = make_cfg(self.root, staging_max_gb=1024 / 1024**3)  # 1 KiB budget
        d = Drainer(cfg, self.led, FakeGadget(idle=99999))
        self.assertIn("staging over budget", d.blocked_reason())

    def test_run_once_reports_the_skip_rather_than_silently_returning(self):
        cfg = make_cfg(self.root)
        d = Drainer(cfg, self.led, FakeGadget(idle=0))
        result = d.run_once()
        self.assertIsNotNone(result["skipped"])
        self.assertEqual(result["moved"], 0)


class TestStagingUsage(unittest.TestCase):
    def test_missing_directory_is_zero_not_an_error(self):
        self.assertEqual(staging_usage_bytes(Path("/nonexistent/xyz")), 0)

    def test_sums_recursively(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            (root / "a").mkdir()
            (root / "a" / "f.mp4").write_bytes(b"x" * 100)
            (root / "g.mp4").write_bytes(b"x" * 50)
            self.assertEqual(staging_usage_bytes(root), 150)


if __name__ == "__main__":
    unittest.main()
