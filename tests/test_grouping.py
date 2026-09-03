"""One folder per print.

There is no print id, job name, or session marker anywhere the printer writes —
only filenames and mtimes. So sessions are inferred from the gap between files,
which works because the measured separation is not close:

    within one print   9-18 minutes   (22 segments of a 4-hour print)
    between prints     805 minutes

The heuristic's known limit is that two prints started inside the window merge.
Nothing in the data can fix that, and the tests below pin the behaviour rather
than pretend otherwise.
"""

import datetime as dt
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from bambu_drain import config
from bambu_drain.config import Rule
from bambu_drain.drain import Drainer, dest_relpath, session_name
from bambu_drain.imagefs import match_rule
from bambu_drain.ledger import Ledger

VIDEO = Rule("**/*.mp4", "video", group="print")
THUMB = Rule("**/*.jpg", "thumbnails", group="print")
LAPSE = Rule("timelapse/video_*.mp4", "", group="print", rename="timelapse.mp4")
MODEL = Rule("**/*.3mf", "models")

T = dt.datetime(2026, 9, 2, 21, 13).timestamp()


class TestLayout(unittest.TestCase):
    def test_print_files_go_under_their_session(self):
        self.assertEqual(
            dest_relpath(VIDEO, Path("ipcam/rec.1.mp4"), T, "2026-09-02_2113"),
            "prints/2026-09-02_2113/video/rec.1.mp4")

    def test_thumbnails_sit_beside_the_video_in_the_same_print(self):
        self.assertEqual(
            dest_relpath(THUMB, Path("ipcam/thumbnail/rec.1.jpg"), T, "2026-09-02_2113"),
            "prints/2026-09-02_2113/thumbnails/rec.1.jpg")

    def test_the_timelapse_is_renamed_and_sits_at_the_print_root(self):
        # The folder is already named for its date; video_<ts>.mp4 adds nothing.
        self.assertEqual(
            dest_relpath(LAPSE, Path("timelapse/video_2026-09-02_21-21-33.mp4"),
                         T, "2026-09-02_2113"),
            "prints/2026-09-02_2113/timelapse.mp4")

    def test_non_print_files_keep_the_dated_layout(self):
        # A sliced model does not belong to any single print.
        self.assertEqual(dest_relpath(MODEL, Path("cache/benchy.3mf"), T),
                         "models/2026/09/benchy.3mf")

    def test_ungrouped_rule_ignores_a_session(self):
        self.assertTrue(
            dest_relpath(MODEL, Path("a.3mf"), T, "2026-09-02_2113").startswith("models/"))

    def test_session_name_is_date_and_time(self):
        self.assertEqual(session_name(T), "2026-09-02_2113")


class TestRuleOrdering(unittest.TestCase):
    """The timelapse rule must beat the generic *.mp4 rule."""

    RULES = (LAPSE, VIDEO, THUMB, MODEL)

    def test_the_assembled_timelapse_matches_its_specific_rule(self):
        r = match_rule("timelapse/video_2026-09-02_21-21-33.mp4", self.RULES)
        self.assertEqual(r.rename, "timelapse.mp4")

    def test_a_chamber_segment_matches_the_generic_rule(self):
        r = match_rule("ipcam/ipcam-record.2026-09-02_21-13-14.1.mp4", self.RULES)
        self.assertEqual(r.dest, "video")

    def test_the_shipped_example_config_orders_them_correctly(self):
        cfg = config.load(Path(__file__).resolve().parent.parent / "config.example.toml")
        r = match_rule("timelapse/video_2026-09-02_21-21-33.mp4", cfg.drain.rules)
        self.assertEqual(r.rename, "timelapse.mp4",
                         "the generic *.mp4 rule is shadowing the timelapse rule")


class TestSessionBoundaries(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        root = Path(self.tmp.name)
        self.led = Ledger(root / "ledger.db")
        self.cfg = config.from_dict({
            "gadget": {"image": str(root / "stick.img")},
            "drain": {"staging": str(root / "staging"), "session_gap_minutes": 45},
            "rule": [{"glob": "**/*.mp4", "dest": "video", "group": "print"}],
        })
        self.d = Drainer(self.cfg, self.led, object())

    def tearDown(self):
        self.led.close()
        self.tmp.cleanup()

    def _record(self, mtime, session):
        self.led.record_drained(f"sha{mtime}", "x.mp4", "p/x.mp4", 1,
                                Path("/s/x.mp4"), session=session, src_mtime=mtime)

    def test_the_first_file_opens_a_session(self):
        self.assertEqual(self.d.session_for(T), "2026-09-02_2113")

    def test_a_segment_14_minutes_later_joins_it(self):
        # The real interval between segments of one print.
        self._record(T, "2026-09-02_2113")
        self.assertEqual(self.d.session_for(T + 14 * 60), "2026-09-02_2113")

    def test_a_segment_18_minutes_later_still_joins(self):
        # The largest within-print gap actually observed.
        self._record(T, "2026-09-02_2113")
        self.assertEqual(self.d.session_for(T + 18 * 60), "2026-09-02_2113")

    def test_the_next_print_805_minutes_later_starts_a_new_one(self):
        self._record(T, "2026-09-02_2113")
        self.assertNotEqual(self.d.session_for(T + 805 * 60), "2026-09-02_2113")

    def test_the_boundary(self):
        self._record(T, "2026-09-02_2113")
        self.assertEqual(self.d.session_for(T + 44 * 60), "2026-09-02_2113")
        self.assertNotEqual(self.d.session_for(T + 46 * 60), "2026-09-02_2113")

    def test_a_four_hour_print_stays_one_session(self):
        # 22 segments, 9-18 min apart, spanning midnight — one folder.
        s = self.d.session_for(T)
        t = T
        for _ in range(22):
            self._record(t, s)
            t += 13 * 60
            self.assertEqual(self.d.session_for(t), s)

    def test_known_limit_two_prints_inside_the_window_merge(self):
        # Documented, not hidden: nothing in the data distinguishes them.
        self._record(T, "2026-09-02_2113")
        self.assertEqual(self.d.session_for(T + 20 * 60), "2026-09-02_2113")


if __name__ == "__main__":
    unittest.main()


LAPSE_CLOSER = Rule("timelapse/video_*.mp4", "", group="print",
                    rename="timelapse.mp4", ends_session=True)


class TestTheFailedPrintAndItsRedo(unittest.TestCase):
    """The real incident, replayed from observed mtimes.

        09:27:10  240.3 MB  seg 1        failed print starts
        09:31:16    0.1 MB  timelapse    its timelapse — tiny, because it failed
        09:31:16   72.2 MB  seg 2        its truncated tail, SAME second
        09:57:42  218.5 MB  seg 3        the redo starts, 26 minutes later
        ...       4 hours of segments, and NO timelapse of its own

    A 26-minute boundary against 18-minute within-print gaps cannot be split by
    time alone without risking fragmenting a print. The timelapse does it.
    """

    def setUp(self):
        self.tmp = TemporaryDirectory()
        root = Path(self.tmp.name)
        self.led = Ledger(root / "ledger.db")
        self.cfg = config.from_dict({
            "gadget": {"image": str(root / "stick.img")},
            "drain": {"staging": str(root / "staging"), "session_gap_minutes": 45},
            "rule": [{"glob": "**/*.mp4", "dest": "video", "group": "print"}],
        })
        self.d = Drainer(self.cfg, self.led, object())
        self.t0 = dt.datetime(2026, 9, 2, 9, 27, 10).timestamp()

    def tearDown(self):
        self.led.close()
        self.tmp.cleanup()

    def _drain(self, mtime, session, closer=False):
        self.led.record_drained(f"sha{mtime}{closer}", "f.mp4", "p/f.mp4", 1,
                                Path("/s/f.mp4"), session=session,
                                src_mtime=mtime, ends_session=closer)

    def test_the_redo_gets_its_own_folder(self):
        s1 = self.d.session_for(self.t0)
        self._drain(self.t0, s1)                                  # seg 1

        t_lapse = self.t0 + 4 * 60 + 6                            # 09:31:16
        self._drain(t_lapse, self.d.session_for(t_lapse))         # seg 2
        self._drain(t_lapse, s1, closer=True)                     # timelapse

        t_redo = self.t0 + 30 * 60 + 32                           # 09:57:42
        s2 = self.d.session_for(t_redo)
        self.assertNotEqual(
            s2, s1,
            "the redo must not be filed with the failed print it replaced")

    def test_without_the_closer_they_would_have_merged(self):
        # Proving the gap alone is not enough — this is the old behaviour.
        s1 = self.d.session_for(self.t0)
        self._drain(self.t0, s1)
        self._drain(self.t0 + 4 * 60 + 6, s1)                     # no closer
        self.assertEqual(self.d.session_for(self.t0 + 30 * 60 + 32), s1)

    def test_a_closer_ends_the_session_even_after_only_seconds(self):
        s1 = self.d.session_for(self.t0)
        self._drain(self.t0, s1, closer=True)
        self.assertNotEqual(self.d.session_for(self.t0 + 5), s1)

    def test_the_long_redo_stays_one_session_despite_having_no_timelapse(self):
        t = self.t0 + 30 * 60
        s = self.d.session_for(t)
        for _ in range(22):
            self._drain(t, s)
            t += 13 * 60
            self.assertEqual(self.d.session_for(t), s)

    def test_ties_put_the_closer_last(self):
        # The final segment and the timelapse are flushed in the same second.
        # If the closer sorted first, the segment would open a new session.
        rules = (VIDEO, LAPSE_CLOSER)
        keyed = sorted(
            [("seg", VIDEO), ("lapse", LAPSE_CLOSER)],
            key=lambda t: (100.0, 1 if t[1].ends_session else 0),
        )
        self.assertEqual([n for n, _ in keyed], ["seg", "lapse"])


class TestSessionNameCollisions(unittest.TestCase):
    """Minute-granular names must not merge two sessions that share a minute."""

    def test_distinct_leaves_a_different_name_alone(self):
        from bambu_drain.drain import _distinct
        self.assertEqual(_distinct("2026-09-02_0930", "2026-09-02_0927"),
                         "2026-09-02_0930")

    def test_distinct_suffixes_an_identical_name(self):
        from bambu_drain.drain import _distinct
        self.assertEqual(_distinct("2026-09-02_0927", "2026-09-02_0927"),
                         "2026-09-02_0927-2")

    def test_distinct_increments_an_existing_suffix(self):
        from bambu_drain.drain import _distinct
        self.assertEqual(_distinct("2026-09-02_0927", "2026-09-02_0927-2"),
                         "2026-09-02_0927-3")

    def test_no_previous_session(self):
        from bambu_drain.drain import _distinct
        self.assertEqual(_distinct("2026-09-02_0927", None), "2026-09-02_0927")


class TestEmptyFileDoesNotClaimTheName(unittest.TestCase):
    """A 0-byte export must not take `timelapse.mp4` from a real one.

    Observed: the printer exported an empty timelapse beside a 15.9 MB one.
    Sorted first, the empty file claimed the canonical name and the real
    timelapse was pushed to `timelapse-b79482df.mp4`.
    """

    def test_an_empty_file_keeps_its_own_name(self):
        rel = dest_relpath(LAPSE, Path("timelapse/video_2026-09-02_21-51-18.mp4"),
                           T, "2026-09-02_1548", size=0)
        self.assertTrue(rel.endswith("video_2026-09-02_21-51-18.mp4"))
        self.assertNotIn("timelapse.mp4", rel)

    def test_a_real_file_still_gets_renamed(self):
        rel = dest_relpath(LAPSE, Path("timelapse/video_2026-09-02_22-05-11.mp4"),
                           T, "2026-09-02_1548", size=15_879_020)
        self.assertTrue(rel.endswith("timelapse.mp4"))

    def test_size_unknown_behaves_as_before(self):
        rel = dest_relpath(LAPSE, Path("timelapse/video_x.mp4"), T, "s")
        self.assertTrue(rel.endswith("timelapse.mp4"))

    def test_rename_free_rules_are_unaffected_by_size(self):
        self.assertEqual(
            dest_relpath(VIDEO, Path("ipcam/rec.1.mp4"), T, "s", size=0),
            "prints/s/video/rec.1.mp4")
