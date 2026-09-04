"""Rebuilding a timelapse on the Pi, from footage that is still there.

The printer often keeps the assembled timelapse on its own internal storage, so
a print arrives as hours of chamber video and nothing to watch. Rebuilding on
the Mac instead would mean pulling gigabytes back out of iCloud — slow, and the
thing that has already cost time twice.
"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from bambu_drain import config, render
from bambu_drain.ledger import Ledger

MODAL = 252_000_000


class TestPlan(unittest.TestCase):
    """Sampling arithmetic, in KEYFRAMES rather than frames.

    A 750s segment carries ~774 keyframes — roughly one per second — measured on
    the real files.
    """

    def _plan(self, segments, per_segment_keyframes, length=60.0, fps=30):
        with mock.patch.object(render, "count_keyframes",
                               return_value=per_segment_keyframes):
            return render.plan([Path(f"s{i}.mp4") for i in range(segments)], length, fps)

    def test_a_44_hour_print_lands_near_60_seconds(self):
        every, frames = self._plan(21, 774)
        self.assertAlmostEqual(frames / 30, 60, delta=4)

    def test_the_last_segment_is_not_counted_as_full(self):
        """A print's final segment is short — that is how the end is detected.

        Counting it as full over-estimated the total and the body came out
        short. Negligible over twenty segments, 50% wrong over two.
        """
        many, _ = self._plan(20, 774)
        few, _ = self._plan(2, 774)
        self.assertGreater(many, few, "more footage must sample more sparsely")

        # The estimate must not treat the short final segment as full: two
        # segments hold ~1161 keyframes, not 1548.
        _, frames_two = self._plan(2, 774)
        self.assertLess(frames_two, 774 * 2)
        self.assertAlmostEqual(frames_two, 774 * 1.5, delta=60)

    def test_short_footage_is_not_padded_to_the_target(self):
        """Two segments cannot make 60s at 30fps — 1161 keyframes is 38.7s.

        Sampling every keyframe is the floor; the output is however long the
        footage allows rather than the length requested.
        """
        every, frames = self._plan(2, 774)
        self.assertEqual(every, 1, "must not skip frames when footage is scarce")
        self.assertLess(frames / 30, 60)

    def test_it_samples_roughly_every_ninth_keyframe(self):
        every, _ = self._plan(21, 774)
        self.assertAlmostEqual(every, 9, delta=1)

    def test_a_short_print_never_samples_below_every_keyframe(self):
        every, _ = self._plan(1, 100)
        self.assertEqual(every, 1)

    def test_no_segments_is_not_a_crash(self):
        self.assertEqual(render.plan([], 60, 30), (1, 0))

    def test_a_segment_with_no_keyframes_does_not_divide_by_zero(self):
        every, frames = self._plan(3, 0)
        self.assertEqual(every, 1)


class TestSelection(unittest.TestCase):
    """Which sessions get rendered. All three conditions matter."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.led = Ledger(self.root / "l.db")

    def tearDown(self):
        self.led.close()
        self.tmp.cleanup()

    def _file(self, session, name, dest, staged=True, ends=False):
        p = self.root / name
        p.write_bytes(b"x")
        self.led.record_drained(f"sha-{session}-{name}", name, dest, 1,
                                p if staged else Path("/gone"),
                                session=session, src_mtime=1.0, ends_session=ends)
        if not staged:
            self.led.record_shipped(f"sha-{session}-{name}", verified=True)
            self.led.clear_staging(f"sha-{session}-{name}")

    def test_a_closed_staged_session_with_no_timelapse_is_selected(self):
        self._file("S", "ipcam-record.x.1.mp4", "prints/S/video/a.mp4")
        self._file("S", "ipcam-record.x.2.mp4", "prints/S/video/b.mp4", ends=True)
        self.assertEqual(self.led.sessions_needing_render(), ["S"])

    def test_an_unfinished_print_is_not_rendered(self):
        # Rendering a print that is still running would be worse than useless.
        self._file("S", "ipcam-record.x.1.mp4", "prints/S/video/a.mp4")
        self.assertEqual(self.led.sessions_needing_render(), [])

    def test_a_session_that_already_has_a_timelapse_is_skipped(self):
        self._file("S", "ipcam-record.x.1.mp4", "prints/S/video/a.mp4", ends=True)
        self._file("S", "video_x.mp4", "prints/S/timelapse.mp4")
        self.assertEqual(self.led.sessions_needing_render(), [])

    def test_an_already_rendered_session_is_not_rendered_twice(self):
        self._file("S", "ipcam-record.x.1.mp4", "prints/S/video/a.mp4", ends=True)
        self._file("S", render.OUT_NAME, f"prints/S/{render.OUT_NAME}")
        self.assertEqual(self.led.sessions_needing_render(), [])

    def test_a_session_whose_segments_already_shipped_is_skipped(self):
        # Nothing left on the Pi to render from.
        self._file("S", "ipcam-record.x.1.mp4", "prints/S/video/a.mp4",
                   staged=False, ends=True)
        self.assertEqual(self.led.sessions_needing_render(), [])

    def test_staged_segments_exclude_thumbnails_and_the_timelapse(self):
        self._file("S", "ipcam-record.x.1.mp4", "prints/S/video/a.mp4", ends=True)
        self._file("S", "ipcam-record.x.1.jpg", "prints/S/thumbnails/a.jpg")
        segs = self.led.staged_segments("S")
        self.assertEqual(len(segs), 1)
        self.assertTrue(str(segs[0]).endswith(".mp4"))

    def test_the_destination_folder_is_derived_from_a_sibling(self):
        self._file("S", "ipcam-record.x.1.mp4", "prints/2026-09-02_1613/video/a.mp4",
                   ends=True)
        self.assertEqual(self.led.session_dest_dir("S"), "prints/2026-09-02_1613")


class TestRenderGuards(unittest.TestCase):
    def test_no_segments_raises_rather_than_producing_an_empty_file(self):
        with self.assertRaises(render.RenderError):
            render.render([], Path("/tmp/x.mp4"))

    def test_missing_ffmpeg_is_reported_clearly(self):
        with mock.patch.object(render, "available", return_value=False):
            with self.assertRaises(render.RenderError) as e:
                render.render([Path("a.mp4")], Path("/tmp/x.mp4"))
            self.assertIn("ffmpeg", str(e.exception))


if __name__ == "__main__":
    unittest.main()


class TestBookends(unittest.TestCase):
    """Real-time clips at each end.

    At one frame per ~9 seconds of print, the first minute — levelling, purge,
    first layer — lasts about 0.2 seconds and is effectively invisible. Same at
    the end. The bookends play those at normal speed.
    """

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.segs = []
        for i in range(3):
            p = self.root / f"ipcam-record.x.{i}.mp4"
            p.write_bytes(b"x" * 100)
            self.segs.append(p)
        self.out = self.root / "out.mp4"

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, head=5.0, tail=5.0):
        """Capture the ffmpeg argument lists each encode pass would use."""
        calls = []

        def fake_encode(args, out, fps, crf):
            calls.append(args)
            Path(out).write_bytes(b"y" * 10)
            return True

        with mock.patch.object(render, "_encode", side_effect=fake_encode), \
             mock.patch.object(render, "count_keyframes", return_value=700), \
             mock.patch.object(render, "available", return_value=True), \
             mock.patch.object(render, "motion_start", return_value=0.0), \
             mock.patch.object(render.subprocess, "run") as sub:
            sub.return_value = mock.Mock(returncode=0, stderr="")
            # concat writes the final file; emulate it
            def _concat(cmd, **kw):
                Path(cmd[-1]).write_bytes(b"z" * 10)
                return mock.Mock(returncode=0, stderr="")
            sub.side_effect = _concat
            render.render(self.segs, self.out, 60.0, 30, 23, head, tail)
        return calls

    def test_three_passes_head_body_tail(self):
        calls = self._run()
        self.assertEqual(len(calls), 3)

    def test_the_head_comes_from_the_first_segment(self):
        head = self._run()[0]
        self.assertIn(str(self.segs[0]), head)
        self.assertIn("-t", head)

    def test_the_tail_comes_from_the_LAST_segment(self):
        tail = self._run()[2]
        self.assertIn(str(self.segs[-1]), tail)
        self.assertIn("-sseof", tail)

    def test_the_tail_seeks_from_the_end_not_the_start(self):
        tail = self._run(tail=5.0)[2]
        self.assertEqual(tail[tail.index("-sseof") + 1], "-5.0")

    def test_the_body_decodes_keyframes_only(self):
        body = self._run()[1]
        self.assertIn("-skip_frame", body)
        self.assertEqual(body[body.index("-skip_frame") + 1], "nokey")

    def test_bookends_can_be_switched_off(self):
        self.assertEqual(len(self._run(head=0, tail=0)), 1)

    def test_head_only(self):
        calls = self._run(head=5.0, tail=0)
        self.assertEqual(len(calls), 2)
        self.assertIn("-t", calls[0])

    def test_the_output_still_exists_with_bookends_off(self):
        self._run(head=0, tail=0)
        self.assertTrue(self.out.exists())


class TestMotionStart(unittest.TestCase):
    """Finding where printing actually begins.

    From a real first segment: seconds 1-5 scored 0.000 (the printer parked),
    sustained motion began around 29s, the purge showed up around 41-49s. There
    was also a lone 0.037 blip at t=0 with silence either side — which an
    instantaneous threshold would have believed.
    """

    def _scores(self, values):
        out = "".join(f"scene_score={v}\n" for v in values)
        return mock.Mock(returncode=0, stdout=out, stderr="")

    def _detect(self, values, **kw):
        with mock.patch.object(render.subprocess, "run", return_value=self._scores(values)):
            return render.motion_start(Path("x.mp4"), **kw)

    def test_the_real_shape_lands_at_the_sustained_motion(self):
        # 0.037 blip, then five still seconds, then sustained movement.
        vals = [0.037, 0, 0, 0, 0, 0] + [0.03] * 10
        self.assertEqual(self._detect(vals), 6.0)

    def test_a_lone_blip_is_not_mistaken_for_the_start(self):
        vals = [0.0, 0.5, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
        self.assertEqual(self._detect(vals), 0.0, "one frame is not sustained motion")

    def test_motion_from_the_very_start_means_no_skip(self):
        self.assertEqual(self._detect([0.05] * 10), 0.0)

    def test_a_still_recording_skips_nothing(self):
        self.assertEqual(self._detect([0.0] * 30), 0.0)

    def test_the_cap_bounds_a_late_detection(self):
        # A print that genuinely starts slowly must lose at most `cap`.
        vals = [0.0] * 200 + [0.05] * 10
        self.assertLessEqual(self._detect(vals, cap=120.0), 120.0)

    def test_no_output_at_all_degrades_to_zero(self):
        with mock.patch.object(render.subprocess, "run",
                               return_value=mock.Mock(returncode=1, stdout="", stderr="")):
            self.assertEqual(render.motion_start(Path("x.mp4")), 0.0)

    def test_sustain_length_is_respected(self):
        vals = [0.0, 0.05, 0.05, 0.0, 0.0, 0.05, 0.05, 0.05, 0.05]
        self.assertEqual(self._detect(vals, sustain=4), 5.0)


class TestHeadOffsetIsApplied(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.segs = []
        for i in range(3):
            p = self.root / f"ipcam-record.x.{i}.mp4"
            p.write_bytes(b"x" * 100)
            self.segs.append(p)

    def tearDown(self):
        self.tmp.cleanup()

    def _head_args(self, skip, detected=29.0):
        calls = []

        def fake_encode(args, out, fps, crf):
            calls.append(args)
            Path(out).write_bytes(b"y" * 10)
            return True

        with mock.patch.object(render, "_encode", side_effect=fake_encode), \
             mock.patch.object(render, "count_keyframes", return_value=700), \
             mock.patch.object(render, "available", return_value=True), \
             mock.patch.object(render, "motion_start", return_value=detected), \
             mock.patch.object(render.subprocess, "run") as sub:
            def _c(cmd, **kw):
                Path(cmd[-1]).write_bytes(b"z")
                return mock.Mock(returncode=0, stderr="")
            sub.side_effect = _c
            render.render(self.segs, self.root / "o.mp4", 60.0, 30, 23, 5.0, 5.0,
                          skip_dead_air=skip)
        return calls[0]

    def test_the_head_seeks_past_the_dead_air(self):
        args = self._head_args(skip=True)
        self.assertIn("-ss", args)
        self.assertEqual(args[args.index("-ss") + 1], "29.0")

    def test_it_can_be_switched_off(self):
        self.assertNotIn("-ss", self._head_args(skip=False))

    def test_a_zero_detection_adds_no_seek(self):
        self.assertNotIn("-ss", self._head_args(skip=True, detected=0.0))
