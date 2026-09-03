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
    def test_named(self):
        self.assertTrue(session_name(T, "Steamer").endswith("_Steamer"))

    def test_unnamed_is_just_the_stamp(self):
        self.assertNotIn("_2", session_name(T, None)[11:])
        self.assertEqual(len(session_name(T, None)), len("2026-09-02_2017"))


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
        self.assertTrue(s.endswith("_Steamer_Cable_Holder_v1"), s)

    def test_a_preset_named_file_leaves_a_plain_timestamp(self):
        s = self.d.session_for(T, SLICED, Path("0.2mm layer, 2 walls, 15% infill.gcode.3mf"))
        self.assertEqual(s, "2026-09-02_2017")

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
        self.assertTrue(s2.endswith("_B"), s2)


if __name__ == "__main__":
    unittest.main()
