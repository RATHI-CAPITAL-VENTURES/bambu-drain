"""Naming a print folder after its model.

The sliced `.gcode.3mf` lands ~15 minutes before the first chamber segment,
because the printer writes it when the job is sent. That makes it the only
START marker available — every other signal can only say a print has ended.

Its filename is the only name there is: slicing strips the mesh objects, so
there is no model name inside the file. And Bambu Studio falls back to the
PROCESS PRESET name whenever the Studio project is unnamed, so roughly half of
them describe a layer height rather than a model.
"""

import datetime as dt
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from bambu_drain import config
from bambu_drain.config import Rule
from bambu_drain.drain import Drainer, model_name, session_name
from bambu_drain.ledger import Ledger

SLICED = Rule("**/*.3mf", "", group="print", starts_session=True, names_session=True)
SEGMENT = Rule("**/*.mp4", "video", group="print", ends_session_if_short=0.95)
T = dt.datetime(2026, 9, 2, 20, 17).timestamp()


class TestModelName(unittest.TestCase):
    def test_a_real_project_name_survives(self):
        self.assertEqual(model_name(Path("Steamer_Cable_Holder_v1.gcode.3mf")),
                         "Steamer_Cable_Holder_v1")

    def test_spaces_and_punctuation_become_safe(self):
        self.assertEqual(model_name(Path("Articulated Dragon (v3).gcode.3mf")),
                         "Articulated_Dragon_v3")

    def test_a_leading_digit_model_is_not_mistaken_for_a_preset(self):
        self.assertEqual(model_name(Path("3DBenchy.gcode.3mf")), "3DBenchy")

    def test_studio_preset_names_are_rejected(self):
        for n in ("0.2mm layer, 2 walls, 15% infill.gcode.3mf",
                  "0.16mm Optimal @BBL X1C.gcode.3mf",
                  "0.20mm Standard @BBL P1P.gcode.3mf",
                  "0.08mm Extra Fine.gcode.3mf"):
            self.assertIsNone(model_name(Path(n)), n)

    def test_both_suffixes_are_stripped(self):
        self.assertEqual(model_name(Path("Thing.3mf")), "Thing")
        self.assertEqual(model_name(Path("Thing.gcode.3mf")), "Thing")

    def test_a_very_long_name_is_bounded(self):
        self.assertLessEqual(len(model_name(Path("x" * 300 + ".gcode.3mf"))), 60)

    def test_an_empty_name_is_none(self):
        self.assertIsNone(model_name(Path(".gcode.3mf")))


class TestSessionName(unittest.TestCase):
    def test_named_is_model_then_short_date(self):
        self.assertEqual(session_name(T, "Steamer"), "Steamer_09_02_26")

    def test_unnamed_is_just_the_date(self):
        self.assertEqual(session_name(T, None), "09_02_26")


class TestSlicedFileStartsAPrint(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        root = Path(self.tmp.name)
        self.led = Ledger(root / "l.db")
        self.cfg = config.from_dict({
            "gadget": {"image": str(root / "s.img")},
            "drain": {"staging": str(root / "st"), "session_gap_minutes": 45},
            "rule": [{"glob": "**/*.mp4", "dest": "video", "group": "print"}],
        })
        self.d = Drainer(self.cfg, self.led, object())

    def tearDown(self):
        self.led.close()
        self.tmp.cleanup()

    def _rec(self, mtime, session, ends=False):
        self.led.record_drained(f"s{mtime}{ends}", "f", "p/f", 1, Path("/s/f"),
                                session=session, src_mtime=mtime, ends_session=ends)

    def test_the_sliced_file_names_the_session(self):
        s = self.d.session_for(T, SLICED, Path("Steamer_Cable_Holder_v1.gcode.3mf"))
        self.assertEqual(s, "Steamer_Cable_Holder_v1_09_02_26")

    def test_a_preset_named_file_leaves_a_plain_date(self):
        s = self.d.session_for(T, SLICED, Path("0.2mm layer, 2 walls, 15% infill.gcode.3mf"))
        self.assertEqual(s, "09_02_26")

    def test_a_reprint_the_same_day_gets_its_own_folder(self):
        # A, then B, then A again — an ordinary afternoon now that names carry
        # no time of day. The third must not land in the first one's folder.
        s1 = self.d.session_for(T, SLICED, Path("A.gcode.3mf"))
        self._rec(T, s1, ends=True)
        s2 = self.d.session_for(T + 3600, SLICED, Path("B.gcode.3mf"))
        self._rec(T + 3600, s2, ends=True)
        s3 = self.d.session_for(T + 7200, SLICED, Path("A.gcode.3mf"))
        self.assertEqual((s1, s2, s3), ("A_09_02_26", "B_09_02_26", "A_09_02_26-2"))

    def test_a_redo_the_same_day_gets_its_own_folder(self):
        # No sliced file, so no name: a second nameless run that day is "-2".
        s1 = self.d.session_for(T, SEGMENT, Path("ipcam.1.mp4"))
        self._rec(T, s1, ends=True)
        s2 = self.d.session_for(T + 3600, SEGMENT, Path("ipcam.1.mp4"))
        self.assertEqual((s1, s2), ("09_02_26", "09_02_26-2"))

    def test_segments_arriving_later_join_the_named_session(self):
        s = self.d.session_for(T, SLICED, Path("Steamer.gcode.3mf"))
        self._rec(T, s)
        # first chamber segment, 13 minutes later, as observed
        self.assertEqual(self.d.session_for(T + 13 * 60, SEGMENT, Path("ipcam.1.mp4")), s)

    def test_a_new_sliced_file_always_opens_a_new_print(self):
        # Even inside the gap window: pressing print means a new print.
        s1 = self.d.session_for(T, SLICED, Path("A.gcode.3mf"))
        self._rec(T, s1)
        s2 = self.d.session_for(T + 60, SLICED, Path("B.gcode.3mf"))
        self.assertNotEqual(s1, s2)
        self.assertTrue(s2.startswith("B_"), s2)


class TestTheRecordingThatBeatTheSlicedFile(unittest.TestCase):
    """Two prints in a row, to the second:

        19:39:20  ipcam-record.….42.jpg        the first thumbnail
        19:39:22  07 Vault Door_plate_1.gcode.3mf

    Sorted by mtime the thumbnail arrived first, opened `2026-09-15_1939`, and
    the sliced file opened `2026-09-15_1939_07_Vault_Door_plate_1` two seconds
    later. The thumbnail was held six hours as an unfinished print and shipped
    alone. The sliced file now takes over the session that preceded it —
    ledger rows and staged files both — when that session is nameless and
    opened within `START_SKEW_SECONDS`.
    """

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.staging = self.root / "st"
        self.led = Ledger(self.root / "l.db")
        self.cfg = config.from_dict({
            "gadget": {"image": str(self.root / "s.img")},
            "drain": {"staging": str(self.staging), "session_gap_minutes": 45},
            "rule": [{"glob": "**/*.mp4", "dest": "video", "group": "print"}],
        })
        self.d = Drainer(self.cfg, self.led, object())

    def tearDown(self):
        self.led.close()
        self.tmp.cleanup()

    def _stage(self, sha, session, sub, name, mtime, ends=False):
        rel = f"prints/{session}/{sub}/{name}" if sub else f"prints/{session}/{name}"
        path = self.staging / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
        self.led.record_drained(sha, name, rel, 1, path, session=session,
                                src_mtime=mtime, ends_session=ends)
        return path

    def test_the_sliced_file_adopts_the_recording_that_preceded_it(self):
        thumb_session = self.d.session_for(T + 1, SEGMENT, Path("ipcam.42.jpg"))
        self.assertEqual(thumb_session, "09_02_26")
        old = self._stage("t42", thumb_session, "thumbnails", "ipcam.42.jpg", T + 1)

        s = self.d.session_for(T + 3, SLICED, Path("07 Vault Door_plate_1.gcode.3mf"))
        self.assertEqual(s, "07_Vault_Door_plate_1_09_02_26")

        row = self.led.db.execute("SELECT * FROM files WHERE sha256 = 't42'").fetchone()
        self.assertEqual(row["session"], s)
        self.assertEqual(row["dest_rel"], f"prints/{s}/thumbnails/ipcam.42.jpg")
        self.assertFalse(old.exists(), "the staged file moved with its record")
        self.assertTrue(Path(row["staging_path"]).exists())
        self.assertEqual(Path(row["staging_path"]), self.staging / row["dest_rel"])
        self.assertFalse((self.staging / "prints" / thumb_session).exists(),
                         "the empty folder is gone")
        # Nothing is left behind under the old name to be held for six hours.
        self.assertEqual(self.led.session_files(thumb_session), [])

    def test_a_preset_named_sliced_file_simply_joins(self):
        # No model name means no rename — the stamp it would get is the one
        # the recording already has.
        thumb_session = self.d.session_for(T + 1, SEGMENT, Path("ipcam.1.jpg"))
        self._stage("t1", thumb_session, "thumbnails", "ipcam.1.jpg", T + 1)
        s = self.d.session_for(T + 3, SLICED, Path("0.2mm layer, 2 walls, 15% infill.gcode.3mf"))
        self.assertEqual(s, thumb_session)

    def test_a_session_that_opened_earlier_is_not_adopted(self):
        # A print that has been recording for ten minutes is not this job's.
        s0 = self.d.session_for(T - 600, SEGMENT, Path("ipcam.1.jpg"))
        self._stage("a", s0, "thumbnails", "ipcam.1.jpg", T - 600)
        self._stage("b", s0, "video", "ipcam.1.mp4", T - 1)
        s = self.d.session_for(T, SLICED, Path("Next.gcode.3mf"))
        self.assertNotEqual(s, s0)
        self.assertEqual(self.led.db.execute(
            "SELECT session FROM files WHERE sha256 = 'a'").fetchone()[0], s0)

    def test_a_named_session_is_not_adopted(self):
        s0 = self.d.session_for(T - 2, SLICED, Path("First.gcode.3mf"))
        self._stage("a", s0, "", "First.gcode.3mf", T - 2)
        s = self.d.session_for(T, SLICED, Path("Second.gcode.3mf"))
        self.assertNotEqual(s, s0)
        self.assertTrue(s.startswith("Second_"))

    def test_adoption_never_merges_into_an_earlier_print_of_the_same_model(self):
        # Model A printed this morning; this afternoon its recording beats its
        # sliced file again. The nameless session takes A's name — suffixed,
        # because A's folder from the morning already exists.
        s0 = self.d.session_for(T - 7200, SLICED, Path("A.gcode.3mf"))
        self._stage("m", s0, "", "A.gcode.3mf", T - 7200, ends=True)
        thumb = self.d.session_for(T + 1, SEGMENT, Path("ipcam.1.jpg"))
        self._stage("t", thumb, "thumbnails", "ipcam.1.jpg", T + 1)
        s = self.d.session_for(T + 3, SLICED, Path("A.gcode.3mf"))
        self.assertEqual((s0, thumb, s), ("A_09_02_26", "09_02_26", "A_09_02_26-2"))
        self.assertEqual(self.led.db.execute(
            "SELECT session FROM files WHERE sha256 = 't'").fetchone()[0], s)

    def test_a_closed_session_is_not_adopted(self):
        s0 = self.d.session_for(T - 2, SEGMENT, Path("ipcam.9.mp4"))
        self._stage("a", s0, "video", "ipcam.9.mp4", T - 2, ends=True)
        s = self.d.session_for(T, SLICED, Path("Next.gcode.3mf"))
        self.assertNotEqual(s, s0)


if __name__ == "__main__":
    unittest.main()
