"""A stick flagged dirty is a stick the printer calls "not formatted".

Found 2026-10-04, two days after 0.9.7 began reconnecting the drive: the
printer enumerated it and never mounted it. The boot region was valid and fsck
called it clean, but VolumeFlags had VolumeDirty set. Linux clears only a flag
it set itself, so once something marked it — a brownout, a yanked write — it
stayed marked, and it only mattered once the printer started remounting.
Clearing it with `fsck.exfat -p` had the printer writing within a second.
"""

import os
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from bambu_drain import config, drain, health, imagefs
from bambu_drain.drain import Drainer
from bambu_drain.ledger import Ledger


def make_image(path: Path, flags: int) -> Path:
    data = bytearray(4096)
    data[3:11] = b"EXFAT   "
    data[106:108] = flags.to_bytes(2, "little")
    path.write_bytes(bytes(data))
    return path


def fsck_that_cleans(image: Path):
    def run(argv, **kw):
        b = bytearray(image.read_bytes())
        b[106] &= ~0x2
        image.write_bytes(bytes(b))
        return mock.Mock(returncode=1, stdout="", stderr="")
    return run


class TestFlag(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.img = Path(self.tmp.name) / "stick.img"

    def test_reads_the_dirty_bit_and_ignores_the_others(self):
        self.assertTrue(imagefs.is_dirty(make_image(self.img, 0x2), "exfat"))
        self.assertFalse(imagefs.is_dirty(make_image(self.img, 0x0), "exfat"))
        # bit 0 is ActiveFat, bit 2 MediaFailure: not this.
        self.assertFalse(imagefs.is_dirty(make_image(self.img, 0x1), "exfat"))

    def test_fat32_is_not_guessed_at(self):
        self.assertFalse(imagefs.is_dirty(make_image(self.img, 0x2), "fat32"))

    def test_a_clean_volume_runs_no_fsck(self):
        make_image(self.img, 0)
        with mock.patch.object(imagefs.subprocess, "run") as run:
            self.assertFalse(imagefs.clear_dirty(self.img, "exfat"))
        run.assert_not_called()

    def test_a_dirty_volume_is_checked_not_just_flipped(self):
        make_image(self.img, 0x2)
        with mock.patch.object(imagefs.subprocess, "run",
                               side_effect=fsck_that_cleans(self.img)) as run:
            self.assertTrue(imagefs.clear_dirty(self.img, "exfat"))
        self.assertEqual(run.call_args[0][0], ["fsck.exfat", "-p", str(self.img)])
        self.assertFalse(imagefs.is_dirty(self.img, "exfat"))

    def test_fsck_that_exits_ok_but_leaves_it_dirty_is_an_error(self):
        make_image(self.img, 0x2)
        with mock.patch.object(imagefs.subprocess, "run",
                               return_value=mock.Mock(returncode=0, stdout="", stderr="")):
            with self.assertRaises(imagefs.MountError):
                imagefs.clear_dirty(self.img, "exfat")

    def test_it_refuses_while_the_pi_has_the_image_mounted(self):
        make_image(self.img, 0x2)
        with mock.patch.object(imagefs, "loop_attached", return_value=True), \
             mock.patch.object(imagefs.subprocess, "run") as run:
            with self.assertRaises(imagefs.MountError):
                imagefs.clear_dirty(self.img, "exfat")
        run.assert_not_called()

    def test_a_volume_fsck_will_not_clean_is_an_error(self):
        make_image(self.img, 0x2)
        with mock.patch.object(imagefs.subprocess, "run",
                               return_value=mock.Mock(returncode=4, stdout="", stderr="bad")):
            with self.assertRaises(imagefs.MountError):
                imagefs.clear_dirty(self.img, "exfat")


class FakeGadget:
    media_present = True
    exists = True
    bound = True

    def __init__(self, image):
        self.image = image
        self.dirty_at_insert = []
        self.reconnects = []

    def last_host_write(self):
        return time.time() - 99999

    def cycle_out(self, settle_seconds=1.0):
        pass

    def cycle_in(self, reconnect=False):
        # What the printer would find when the drive comes back, and whether
        # it would actually re-read it.
        self.dirty_at_insert.append(imagefs.is_dirty(self.image, "exfat"))
        self.reconnects.append(reconnect)


class TestDrainPass(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.img = make_image(root / "stick.img", 0x2)
        self.stick = root / "stick"
        self.stick.mkdir()
        self.led = Ledger(root / "ledger.db")
        self.addCleanup(self.led.close)
        self.cfg = config.from_dict({
            "gadget": {"image": str(self.img)},
            "drain": {"idle_minutes": 0, "staging": str(root / "staging")},
            "rule": [{"glob": "**/*.mp4", "dest": "video"}],
        })

    @contextmanager
    def _mount(self, *a, **kw):
        self.mounted = True
        try:
            yield self.stick
        finally:
            self.mounted = False

    def _pass(self, run, drainer=None, g=None, **kw):
        g = g or FakeGadget(self.img)
        self.mounted = False
        fsck_calls = []

        def guarded(argv, **k):
            # fsck must never run with the Pi's own mount still live.
            fsck_calls.append(self.mounted)
            return run(argv, **k)

        with mock.patch.object(drain.imagefs, "mounted", self._mount), \
             mock.patch.object(imagefs.subprocess, "run", side_effect=guarded):
            (drainer or Drainer(self.cfg, self.led, g)).run_once(**kw)
        self.assertNotIn(True, fsck_calls, "fsck ran while the image was mounted")
        self.fsck_calls = fsck_calls
        return g

    def test_the_printer_gets_the_stick_back_clean_and_rereads_it(self):
        # An idle printer that refused the dirty volume wrote nothing, so
        # there is nothing to delete — the reconnect must come from the clean.
        g = self._pass(fsck_that_cleans(self.img))
        self.assertEqual(g.dirty_at_insert, [False])
        self.assertEqual(g.reconnects, [True])
        self.assertIn("volume_cleaned", [r["kind"] for r in self.led.recent_events()])

    def test_a_clean_stick_costs_no_fsck_and_no_reconnect(self):
        make_image(self.img, 0)
        g = self._pass(lambda *a, **k: self.fail("fsck on a clean volume"))
        self.assertEqual(g.reconnects, [False])

    def test_a_stick_that_cannot_be_cleaned_still_goes_back_and_says_so(self):
        g = self._pass(lambda *a, **k: mock.Mock(returncode=4, stdout="", stderr="x"))
        self.assertEqual(g.dirty_at_insert, [True])
        self.assertEqual(g.reconnects, [True], "fsck may have written before failing")
        self.assertIn("volume_dirty", [r["kind"] for r in self.led.recent_events()])

    def test_a_failed_clean_is_not_retried_every_poll(self):
        bad = lambda *a, **k: mock.Mock(returncode=4, stdout="", stderr="x")
        d = Drainer(self.cfg, self.led, FakeGadget(self.img))
        self._pass(bad, drainer=d)
        self._pass(bad, drainer=d)
        self.assertEqual(self.fsck_calls, [])
        kinds = [r["kind"] for r in self.led.recent_events()]
        self.assertEqual(kinds.count("volume_dirty"), 1)

    def test_a_dry_run_writes_nothing(self):
        self._pass(lambda *a, **k: self.fail("fsck in a dry run"), dry_run=True)
        self.assertTrue(imagefs.is_dirty(self.img, "exfat"))

    def test_a_truncated_pass_leaves_uncopied_files_to_a_later_fsck(self):
        for name in ("a.mp4", "b.mp4"):
            src = self.stick / name
            src.write_bytes(b"z" * 64)
            past = time.time() - 3600
            os.utime(src, (past, past))
        with mock.patch.object(Drainer, "eject_budget", return_value=-1):
            self._pass(lambda *a, **k: self.fail("fsck on a truncated pass"))
        self.assertTrue(imagefs.is_dirty(self.img, "exfat"))


class TestHealth(unittest.TestCase):
    def test_a_dirty_stick_is_a_problem(self):
        payload = {"gadget": {"exists": True, "bound": True, "media_present": True,
                              "volume_dirty": True},
                   "drain": {}, "archive": {}}
        self.assertTrue(any("unformatted" in p for p in health.problems(payload)))


class TestHealthWhileMounted(unittest.TestCase):
    def test_the_flag_is_unknown_while_the_pi_has_it_mounted(self):
        # Linux holds it dirty for its own mount; that is not what the printer
        # will meet, and `status` must not raise the alarm for it.
        with TemporaryDirectory() as d:
            img = make_image(Path(d) / "stick.img", 0x2)
            cfg = config.from_dict({"gadget": {"image": str(img)},
                                    "rule": [{"glob": "*.mp4", "dest": "v"}]})
            with mock.patch.object(imagefs, "loop_attached", return_value=True):
                self.assertIsNone(health._volume_dirty(cfg))
            with mock.patch.object(imagefs, "loop_attached", return_value=False):
                self.assertTrue(health._volume_dirty(cfg))


class TestGadgetCreate(unittest.TestCase):
    def _create(self, root, exists, locked=False):
        from bambu_drain import cli, lock
        img = make_image(root / "stick.img", 0x2)
        cfg = config.from_dict({"gadget": {"image": str(img)},
                                "rule": [{"glob": "*.mp4", "dest": "v"}]})
        gadget = mock.Mock(exists=exists)
        led = Ledger(root / "ledger.db")
        self.addCleanup(led.close)
        args = mock.Mock(action="create")
        with mock.patch.object(cli, "_wire", return_value=(cfg, led, gadget, None, None)), \
             mock.patch.object(imagefs.subprocess, "run", side_effect=fsck_that_cleans(img)) as run, \
             mock.patch("builtins.print"):
            if locked:
                with lock.single_instance(cfg.drain_lock_path):
                    cli.cmd_gadget(args)
            else:
                cli.cmd_gadget(args)
        return img, run

    def test_boot_hands_the_printer_a_clean_stick(self):
        with TemporaryDirectory() as d:
            img, _ = self._create(Path(d), exists=False)
            self.assertFalse(imagefs.is_dirty(img, "exfat"))

    def test_an_existing_gadget_means_the_printer_may_have_it(self):
        with TemporaryDirectory() as d:
            img, run = self._create(Path(d), exists=True)
            run.assert_not_called()

    def test_a_drain_pass_in_progress_is_not_fscked_underneath(self):
        with TemporaryDirectory() as d:
            img, run = self._create(Path(d), exists=False, locked=True)
            run.assert_not_called()
            self.assertTrue(imagefs.is_dirty(img, "exfat"))


class TestMigrationRace(unittest.TestCase):
    def test_a_column_added_by_the_other_service_first_is_not_fatal(self):
        # Drain and ship start together; both see the old schema, one adds
        # the column first, and the other used to crash on "duplicate column".
        with TemporaryDirectory() as d:
            led = Ledger(Path(d) / "ledger.db")
            self.addCleanup(led.close)
            led._migrate(set())          # as if it had read the old schema


if __name__ == "__main__":
    unittest.main()
