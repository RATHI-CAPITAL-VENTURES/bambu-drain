"""`doctor` must notice a feature that is enabled but cannot work.

This project has now shipped the same class of bug four times: something
enabled in config whose runtime requirement was absent, which loaded cleanly
and silently did nothing.

    the `sudo bambu-drain` wrapper that could not import its own package
    `ends_session` missing from a deployed config
    `ends_session_if_short` missing from the next one
    ffmpeg missing while `[render] enabled = true`

Each was found by reading logs or a ledger, not by a check. These tests pin the
checks that would have caught the last two.
"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from bambu_drain import cli, config


def _cfg(tmp: Path, **over):
    raw = {
        "gadget": {"image": str(tmp / "s.img")},
        "drain": {"staging": str(tmp / "st")},
        "ship": {"host": "mac", "dest": "/tmp/a"},
        "render": {"enabled": True},
        "rule": [{"glob": "**/*.mp4", "dest": "video", "group": "print",
                  "ends_session_if_short": 0.95}],
    }
    for k, v in over.items():
        raw[k] = v
    return config.from_dict(raw)


class _Doctor:
    """Runs cmd_doctor and records every check label and pass/fail."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.results = {}

    def run(self):
        import types
        args = types.SimpleNamespace(config=Path("/nonexistent"))
        with mock.patch.object(cli, "_wire") as wire:
            shipper = mock.Mock()
            shipper.reachable.return_value = True
            gadget = mock.Mock()
            gadget.exists = True
            gadget.media_present = True
            wire.return_value = (self.cfg, mock.Mock(), gadget, mock.Mock(), shipper)
            real_print = print
            captured = []
            with mock.patch("builtins.print", lambda *a, **k: captured.append(" ".join(str(x) for x in a))):
                try:
                    cli.cmd_doctor(args)
                except Exception:
                    pass
            for line in captured:
                if line.startswith("[ok ]") or line.startswith("[FAIL]"):
                    self.results[line[6:].strip()] = line.startswith("[ok ]")
        return self.results


class TestRenderDependency(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _labels(self, available, enabled=True):
        cfg = _cfg(self.root, render={"enabled": enabled})
        with mock.patch("bambu_drain.render.available", return_value=available):
            return _Doctor(cfg).run()

    def test_missing_ffmpeg_is_reported_when_render_is_enabled(self):
        res = self._labels(available=False)
        label = next((k for k in res if "ffmpeg" in k), None)
        self.assertIsNotNone(label, "doctor must check ffmpeg when render is on")
        self.assertFalse(res[label], "a missing ffmpeg must FAIL, not pass quietly")

    def test_present_ffmpeg_passes(self):
        res = self._labels(available=True)
        label = next((k for k in res if "ffmpeg" in k), None)
        self.assertIsNotNone(label)
        self.assertTrue(res[label])

    def test_it_is_not_checked_when_render_is_disabled(self):
        # Not a requirement if the feature is off; a false alarm is also noise.
        res = self._labels(available=False, enabled=False)
        self.assertIsNone(next((k for k in res if "ffmpeg" in k), None))


class TestSessionClosingRule(unittest.TestCase):
    """The config-drift check that took a day to notice by hand."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_grouping_without_any_closing_rule_fails(self):
        cfg = _cfg(self.root, rule=[{"glob": "**/*.mp4", "dest": "video",
                                     "group": "print"}])
        with mock.patch("bambu_drain.render.available", return_value=True):
            res = _Doctor(cfg).run()
        label = next((k for k in res if "closes print sessions" in k), None)
        self.assertIsNotNone(label)
        self.assertFalse(res[label], "no rule closes sessions — must FAIL")

    def test_a_short_segment_rule_satisfies_it(self):
        with mock.patch("bambu_drain.render.available", return_value=True):
            res = _Doctor(_cfg(self.root)).run()
        label = next((k for k in res if "closes print sessions" in k), None)
        self.assertTrue(res[label])


if __name__ == "__main__":
    unittest.main()
