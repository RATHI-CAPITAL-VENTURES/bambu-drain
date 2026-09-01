"""The staged copy must be durable before the original is destroyed.

This is the bug that a power cut found in the field: a 150 MB file was copied,
checksum-verified, deleted from the printer's stick, and then reduced to a
0-byte staging file by an unclean shutdown two minutes later. The verify had
read the copy back out of the page cache, so it confirmed bytes that were never
written to disk.

These tests guard the ordering, not just the presence of an fsync.
"""

import os
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from bambu_drain import config, drain
from bambu_drain.drain import Drainer, fsync_file_and_parent
from bambu_drain.ledger import Ledger


class FakeGadget:
    def __init__(self, idle=99999, media_present=True):
        self._idle = idle
        self.media_present = media_present
        self.cycled_out = 0
        self.cycled_in = 0

    def idle_seconds(self):
        return self._idle

    def cycle_out(self, settle_seconds=1.0):
        self.cycled_out += 1

    def cycle_in(self):
        self.cycled_in += 1


class TestFsyncHelper(unittest.TestCase):
    def test_fsyncs_both_the_file_and_its_directory(self):
        # The data being durable is no help if the directory entry pointing at
        # it is not.
        with TemporaryDirectory() as d:
            f = Path(d) / "sub" / "a.bin"
            f.parent.mkdir(parents=True)
            f.write_bytes(b"x" * 1024)
            with mock.patch.object(os, "fsync", wraps=os.fsync) as fsync:
                fsync_file_and_parent(f)
            self.assertEqual(fsync.call_count, 2)

    def test_closes_its_descriptors(self):
        with TemporaryDirectory() as d:
            f = Path(d) / "a.bin"
            f.write_bytes(b"x")
            before = len(os.listdir("/dev/fd")) if os.path.isdir("/dev/fd") else None
            for _ in range(50):
                fsync_file_and_parent(f)
            if before is not None:
                self.assertLess(len(os.listdir("/dev/fd")) - before, 10)


class TestDrainOrdering(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.stick = self.root / "stick"
        self.stick.mkdir()
        self.led = Ledger(self.root / "ledger.db")

        self.cfg = config.from_dict({
            "gadget": {"image": str(self.root / "stick.img")},
            "drain": {
                "idle_minutes": 0,
                "min_file_age_minutes": 0,
                "staging": str(self.root / "staging"),
            },
            "rule": [{"glob": "**/*.mp4", "dest": "timelapse", "delete": True}],
        })

    def tearDown(self):
        self.led.close()
        self.tmp.cleanup()

    def _make_source(self, name="video.mp4", size=4096):
        src = self.stick / name
        src.write_bytes(b"z" * size)
        past = time.time() - 3600
        os.utime(src, (past, past))
        return src

    @contextmanager
    def _fake_mount(self, *a, **kw):
        yield self.stick

    def test_fsync_happens_before_the_source_is_unlinked(self):
        src = self._make_source()
        order = []

        real_fsync = os.fsync
        real_unlink = Path.unlink

        def rec_fsync(fd):
            order.append("fsync")
            return real_fsync(fd)

        def rec_unlink(self_path, *a, **kw):
            if self_path == src:
                order.append("unlink")
            return real_unlink(self_path, *a, **kw)

        d = Drainer(self.cfg, self.led, FakeGadget())
        with mock.patch.object(drain.os, "fsync", rec_fsync), \
             mock.patch.object(Path, "unlink", rec_unlink), \
             mock.patch.object(drain.imagefs, "mounted", self._fake_mount):
            result = d.run_once()

        self.assertEqual(result["moved"], 1)
        self.assertIn("fsync", order)
        self.assertIn("unlink", order)
        self.assertLess(
            order.index("fsync"), order.index("unlink"),
            "the staged copy must be on disk before the original is destroyed",
        )
        self.assertFalse(src.exists())

    def test_medium_is_reinserted_even_when_the_pass_explodes(self):
        # A printer with its stick back beats a complete drain pass.
        self._make_source()
        gadget = FakeGadget()
        d = Drainer(self.cfg, self.led, gadget)

        @contextmanager
        def boom(*a, **kw):
            raise OSError("mount blew up")
            yield  # pragma: no cover

        with mock.patch.object(drain.imagefs, "mounted", boom):
            with self.assertRaises(OSError):
                d.run_once()
        self.assertEqual(gadget.cycled_out, 1)
        self.assertEqual(gadget.cycled_in, 1)

    def test_a_mismatched_copy_leaves_the_original_on_the_stick(self):
        src = self._make_source()
        d = Drainer(self.cfg, self.led, FakeGadget())
        with mock.patch.object(drain.imagefs, "sha256", side_effect=["aaa", "bbb"]), \
             mock.patch.object(drain.imagefs, "mounted", self._fake_mount):
            result = d.run_once()
        self.assertEqual(result["moved"], 0)
        self.assertTrue(src.exists(), "a failed verify must never delete the source")


if __name__ == "__main__":
    unittest.main()


class TestShipDiagnosesTheRightEnd(unittest.TestCase):
    """A truncated local copy must not be reported as a remote problem.

    The real incident logged "remote checksum mismatch" when the Mac was fine
    and our own staged file was 0 bytes. Blaming the far end sends you
    debugging the network while the actual data loss goes unnoticed.
    """

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.led = Ledger(self.root / "ledger.db")
        self.cfg = config.from_dict({
            "gadget": {"image": str(self.root / "s.img")},
            "ship": {"host": "mac", "dest": "/tmp/arch"},
            "rule": [{"glob": "**/*.mp4", "dest": "timelapse"}],
        })

    def tearDown(self):
        self.led.close()
        self.tmp.cleanup()

    def test_truncated_staging_file_is_reported_as_local_loss(self):
        from bambu_drain.ship import Shipper

        staged = self.root / "video.mp4"
        staged.write_bytes(b"")                      # the 0-byte survivor
        self.led.record_drained("sha1", "video.mp4", "timelapse/video.mp4",
                                157286400, staged)   # ledger remembers 150 MB

        shipper = Shipper(self.cfg, self.led)
        with mock.patch.object(Shipper, "reachable", return_value=True), \
             mock.patch.object(Shipper, "_ssh") as ssh:
            result = shipper.run_once()

        ssh.assert_not_called()                      # never touched the network
        self.assertEqual(result["shipped"], 0)
        kinds = [r["kind"] for r in self.led.recent_events()]
        self.assertIn("local_corrupt", kinds)

    def test_it_does_not_retry_a_lost_file_forever(self):
        from bambu_drain.ship import Shipper

        staged = self.root / "video.mp4"
        staged.write_bytes(b"")
        self.led.record_drained("sha1", "video.mp4", "timelapse/video.mp4", 999, staged)
        shipper = Shipper(self.cfg, self.led)
        with mock.patch.object(Shipper, "reachable", return_value=True), \
             mock.patch.object(Shipper, "_ssh"):
            shipper.run_once()
        self.assertEqual(len(self.led.unshipped()), 0, "a lost file must stop being pending")


class TestMediumSelfHeal(unittest.TestCase):
    """The printer must never be left looking at an empty card reader.

    If a pass dies between eject and insert, the printer reports "no USB drive"
    and silently has nowhere to write — indistinguishable from the cable falling
    out, and unnoticed until a print fails.
    """

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.led = Ledger(self.root / "ledger.db")
        self.cfg = config.from_dict({
            "gadget": {"image": str(self.root / "stick.img")},
            "drain": {"idle_minutes": 999, "staging": str(self.root / "staging")},
            "rule": [{"glob": "**/*.mp4", "dest": "timelapse"}],
        })

    def tearDown(self):
        self.led.close()
        self.tmp.cleanup()

    def test_absent_medium_is_reinserted_even_when_the_gate_is_closed(self):
        # idle_minutes=999 means the drain itself will not run. Healing must
        # still happen — a printer mid-print is exactly when it matters most.
        g = FakeGadget(idle=0, media_present=False)
        d = Drainer(self.cfg, self.led, g)
        result = d.run_once()
        self.assertIsNotNone(result["skipped"], "gate should still be closed")
        self.assertEqual(g.cycled_in, 1, "medium must be re-inserted anyway")
        self.assertIn("medium_reinserted", [r["kind"] for r in self.led.recent_events()])

    def test_present_medium_is_left_alone(self):
        g = FakeGadget(idle=0, media_present=True)
        Drainer(self.cfg, self.led, g).run_once()
        self.assertEqual(g.cycled_in, 0)
