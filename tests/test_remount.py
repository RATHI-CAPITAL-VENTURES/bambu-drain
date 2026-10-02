"""The printer must be made to re-read a stick the Pi has changed.

Found on 2026-10-02: the printer refused to record for "not enough storage"
with 23 GB free on disk. A media change does not make it remount, so it kept
the allocation bitmap from its first mount, never saw anything the Pi freed,
and wrote that stale bitmap back — 9.2 GB marked used on a stick holding 3 MB,
every orphaned run a recording drained weeks before. Health said "ok" all week.
"""

import os
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from bambu_drain import config, drain, gadget as gadget_mod, imagefs
from bambu_drain.drain import Drainer, RECLAIM_ABOVE_BYTES
from bambu_drain.gadget import GadgetError, MassStorageGadget
from bambu_drain.ledger import Ledger


class FakeGadget:
    media_present = True
    exists = True

    def __init__(self, bound=True):
        self.bound = bound
        self.calls = []

    def last_host_write(self):
        return time.time() - 99999

    def cycle_out(self, settle_seconds=1.0):
        self.calls.append("out")

    def cycle_in(self, reconnect=False):
        self.calls.append("in+reconnect" if reconnect else "in")

    def bind(self):
        self.calls.append("bind")
        self.bound = True


class DrainCase(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.stick = self.root / "stick"
        self.stick.mkdir()
        self.led = Ledger(self.root / "ledger.db")
        self.cfg = config.from_dict({
            "gadget": {"image": str(self.root / "stick.img")},
            "drain": {"idle_minutes": 0, "min_file_age_minutes": 0,
                      "staging": str(self.root / "staging")},
            "rule": [{"glob": "**/*.mp4", "dest": "video", "group": "print",
                      "discard_after_timelapse": True},
                     {"glob": "**/*.3mf", "dest": "", "group": "print"}],
        })

    def tearDown(self):
        self.led.close()
        self.tmp.cleanup()

    def _source(self, name="a.mp4", body=b"z" * 4096):
        src = self.stick / name
        src.write_bytes(body)
        past = time.time() - 3600
        os.utime(src, (past, past))
        return src

    @contextmanager
    def _fake_mount(self, *a, **kw):
        yield self.stick

    def _run(self, g, dry_run=False, orphaned=0, reclaim=None, drainer=None,
             fsck="/usr/sbin/fsck.exfat"):
        drainer = drainer or Drainer(self.cfg, self.led, g)
        with mock.patch.object(drain.imagefs, "mounted", self._fake_mount), \
             mock.patch.object(drain.imagefs, "orphaned_bytes", return_value=orphaned), \
             mock.patch.object(drain.shutil, "which", return_value=fsck), \
             mock.patch.object(drain.imagefs, "reclaim", reclaim or mock.Mock()) as rec:
            result = drainer.run_once(dry_run=dry_run)
        return result, rec


class TestReconnectAfterChange(DrainCase):
    def test_a_pass_that_deleted_a_file_reconnects(self):
        self._source()
        g = FakeGadget()
        result, _ = self._run(g)
        self.assertEqual(g.calls, ["out", "in+reconnect"])
        self.assertTrue(result["reconnected"])

    def test_a_pass_that_found_nothing_only_changes_the_medium(self):
        # This runs every poll while the printer is idle. Detaching the whole
        # device that often would be the rude thing the media change avoids.
        g = FakeGadget()
        result, _ = self._run(g)
        self.assertEqual(g.calls, ["out", "in"])
        self.assertFalse(result["reconnected"])

    def test_a_dry_run_changes_nothing_and_does_not_reconnect(self):
        src = self._source()
        g = FakeGadget()
        self._run(g, dry_run=True, orphaned=RECLAIM_ABOVE_BYTES * 4)
        self.assertEqual(g.calls, ["out", "in"])
        self.assertTrue(src.exists())

    def test_deleting_an_already_drained_copy_counts_as_a_change(self):
        # A pass that died after recording the file and before deleting it.
        src = self._source()
        self.led.record_drained(imagefs.sha256(src), src.name, "video/a.mp4",
                                4096, self.root / "staged")
        g = FakeGadget()
        self._run(g)
        self.assertFalse(src.exists())
        self.assertEqual(g.calls, ["out", "in+reconnect"])

    def test_a_detached_gadget_is_reattached_before_any_gate(self):
        g = FakeGadget(bound=False)
        g.last_host_write = lambda: time.time()      # printer busy: gate closed
        self.cfg = config.from_dict({
            "gadget": {"image": str(self.root / "stick.img")},
            "drain": {"idle_minutes": 5, "staging": str(self.root / "staging")},
            "rule": [{"glob": "**/*.mp4", "dest": "video"}],
        })
        result, _ = self._run(g)
        self.assertIsNotNone(result["skipped"])
        self.assertEqual(g.calls, ["bind"])
        self.assertIn("gadget_reattached", [r["kind"] for r in self.led.recent_events()])

    def test_an_absent_medium_comes_back_as_a_new_drive(self):
        # The pass that died may already have deleted files.
        g = FakeGadget()
        g.media_present = False
        self._run(g)
        self.assertEqual(g.calls[0], "in+reconnect")

    def test_a_failed_reattach_does_not_stop_the_pass(self):
        g = FakeGadget(bound=False)
        g.bind = mock.Mock(side_effect=GadgetError("no UDC"))
        result, _ = self._run(g)
        self.assertIsNone(result["skipped"])
        self.assertEqual(g.calls, ["out", "in"])

    def test_the_rule_flag_reaches_the_ledger(self):
        self._source("a.mp4")
        self._source("model.3mf", b"m" * 10)
        self._run(FakeGadget())
        flags = {r["src_name"]: r["discard"] for r in self.led.unshipped()}
        self.assertEqual(flags, {"a.mp4": 1, "model.3mf": 0})


class TestReclaim(DrainCase):
    def test_orphaned_space_is_reclaimed_and_the_printer_reconnected(self):
        g = FakeGadget()
        result, rec = self._run(g, orphaned=9 * 1024**3)
        rec.assert_called_once()
        self.assertEqual(result["reclaimed_bytes"], 9 * 1024**3)
        self.assertEqual(g.calls, ["out", "in+reconnect"])
        self.assertIn("reclaimed", [r["kind"] for r in self.led.recent_events()])

    def test_overhead_sized_slack_is_left_alone(self):
        g = FakeGadget()
        _, rec = self._run(g, orphaned=RECLAIM_ABOVE_BYTES)
        rec.assert_not_called()
        self.assertEqual(g.calls, ["out", "in"])

    def test_a_failed_reclaim_is_recorded_and_the_stick_still_goes_back(self):
        g = FakeGadget()
        boom = mock.Mock(side_effect=imagefs.MountError("fsck.exfat: exit 4"))
        result, _ = self._run(g, orphaned=9 * 1024**3, reclaim=boom)
        self.assertEqual(result["reclaimed_bytes"], 0)
        # fsck may have written before it failed, so the printer still remounts.
        self.assertEqual(g.calls, ["out", "in+reconnect"])
        self.assertIn("reclaim_error", [r["kind"] for r in self.led.recent_events()])


    def test_a_reclaim_that_cannot_work_is_not_retried_every_poll(self):
        # Otherwise: fsck and a USB disconnect every 30 seconds, forever.
        g = FakeGadget()
        d = Drainer(self.cfg, self.led, g)
        boom = mock.Mock(side_effect=imagefs.MountError("exit 4"))
        self._run(g, orphaned=9 * 1024**3, reclaim=boom, drainer=d)
        _, rec = self._run(g, orphaned=9 * 1024**3, drainer=d)
        rec.assert_not_called()
        self.assertEqual(g.calls[-2:], ["out", "in"])
        # A different figure means something changed; worth one more try.
        _, rec = self._run(g, orphaned=10 * 1024**3, drainer=d)
        rec.assert_called_once()

    def test_a_missing_fsck_is_reported_once_and_never_disconnects(self):
        g = FakeGadget()
        d = Drainer(self.cfg, self.led, g)
        for _ in range(3):
            _, rec = self._run(g, orphaned=9 * 1024**3, drainer=d, fsck=None)
            rec.assert_not_called()
        self.assertEqual(g.calls, ["out", "in"] * 3)
        kinds = [r["kind"] for r in self.led.recent_events()]
        self.assertEqual(kinds.count("reclaim_error"), 1)

    def test_a_truncated_pass_does_not_fsck_files_it_has_not_copied(self):
        self._source("a.mp4")
        self._source("b.mp4", b"y" * 4096)
        g = FakeGadget()
        d = Drainer(self.cfg, self.led, g)
        with mock.patch.object(Drainer, "eject_budget", return_value=-1):
            result, rec = self._run(g, orphaned=9 * 1024**3, drainer=d)
        self.assertTrue(result["truncated"])
        rec.assert_not_called()

    def test_a_failed_unmount_is_never_swallowed(self):
        g = FakeGadget()
        boom = mock.Mock(side_effect=imagefs.UnmountError("still mounted"))
        with self.assertRaises(imagefs.UnmountError):
            self._run(g, orphaned=9 * 1024**3, reclaim=boom)


class TestImagefsReclaim(unittest.TestCase):
    def test_a_hung_fsck_is_an_error_not_a_wait(self):
        with mock.patch.object(imagefs.subprocess, "run",
                               side_effect=imagefs.subprocess.TimeoutExpired("fsck", 120)):
            with self.assertRaises(imagefs.MountError):
                imagefs.reclaim(Path("/x/stick.img"), Path("/mnt/x"), "exfat")

    def test_a_plain_directory_reports_no_orphans(self):
        # Measured against a directory this would be the rest of the disk, and
        # the drain would run fsck on every pass.
        with TemporaryDirectory() as d:
            self.assertEqual(imagefs.orphaned_bytes(Path(d)), 0)

    def test_orphans_are_used_space_no_file_holds(self):
        with TemporaryDirectory() as d:
            (Path(d) / "a").write_bytes(b"x" * 8192)
            held = (Path(d) / "a").stat().st_blocks * 512
            vfs = mock.Mock(f_blocks=1000, f_bfree=0, f_frsize=4096)
            with mock.patch.object(imagefs.os.path, "ismount", return_value=True), \
                 mock.patch.object(imagefs.os, "statvfs", return_value=vfs):
                self.assertEqual(imagefs.orphaned_bytes(Path(d)), 4096000 - held)

    def test_exfat_salvages_orphans_then_deletes_them(self):
        with TemporaryDirectory() as d:
            mp = Path(d)
            (mp / "LOST+FOUND").mkdir()
            (mp / "LOST+FOUND" / "FILE0000001.CHK").write_bytes(b"x")
            (mp / "ipcam").mkdir()

            @contextmanager
            def fake_mount(*a, **kw):
                yield mp

            with mock.patch.object(imagefs.subprocess, "run",
                                   return_value=mock.Mock(returncode=1, stdout="", stderr="")) as run, \
                 mock.patch.object(imagefs, "mounted", fake_mount):
                imagefs.reclaim(Path("/x/stick.img"), mp, "exfat")
            # `-y` alone reports the volume clean and frees nothing.
            self.assertEqual(run.call_args[0][0],
                             ["fsck.exfat", "-s", "-y", "/x/stick.img"])
            self.assertFalse((mp / "LOST+FOUND").exists())
            self.assertTrue((mp / "ipcam").exists())

    def test_a_failed_fsck_raises_rather_than_claiming_success(self):
        with mock.patch.object(imagefs.subprocess, "run",
                               return_value=mock.Mock(returncode=4, stdout="", stderr="bad")):
            with self.assertRaises(imagefs.MountError):
                imagefs.reclaim(Path("/x/stick.img"), Path("/mnt/x"), "exfat")

    def test_every_supported_filesystem_has_a_reclaim_tool(self):
        for fs in ("exfat", "fat32"):
            self.assertTrue(imagefs.reclaim_tool(fs).startswith("fsck."))


class TestGadgetCycleIn(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        root = Path(self.tmp.name)
        cfg = config.from_dict({
            "gadget": {"image": str(root / "stick.img"), "udc": "fake.udc"},
            "rule": [{"glob": "**/*.mp4", "dest": "v"}],
        }).gadget
        self.g = MassStorageGadget(cfg, configfs_root=root)
        self.g.lun.mkdir(parents=True)
        (self.g.lun / "file").write_text("\n")
        (self.g.root / "UDC").write_text("fake.udc\n")
        self.image = str(root / "stick.img")
        self.writes = []
        real = gadget_mod._write

        def rec(path, value):
            self.writes.append((path.name, value.strip()))
            real(path, value)

        patcher = mock.patch.object(gadget_mod, "_write", rec)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_a_plain_cycle_in_never_touches_the_connection(self):
        self.g.cycle_in()
        self.assertEqual(self.writes, [("file", self.image)])

    def test_reconnect_detaches_inserts_then_reattaches(self):
        # The printer must find the medium already in when the drive reappears.
        self.g.cycle_in(reconnect=True, settle_seconds=0, attach_timeout=0)
        self.assertEqual(self.writes, [("UDC", ""), ("file", self.image),
                                       ("UDC", "fake.udc")])
        self.assertTrue(self.g.bound)

    def test_the_medium_goes_back_even_if_detaching_fails(self):
        with mock.patch.object(MassStorageGadget, "unbind",
                               side_effect=GadgetError("busy")):
            with self.assertRaises(GadgetError):
                self.g.cycle_in(reconnect=True, settle_seconds=0, attach_timeout=0)
        self.assertEqual(self.writes, [("file", self.image)])


if __name__ == "__main__":
    unittest.main()
