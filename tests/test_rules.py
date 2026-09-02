import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from bambu_drain import imagefs
from bambu_drain.config import Rule
from bambu_drain.drain import dest_relpath, _unique


RULES = (
    Rule("**/*.mp4", "timelapse"),
    Rule("**/*.3mf", "models"),
    Rule("**/*.bbl.bin", "firmware", delete=False),
)


class TestRuleMatching(unittest.TestCase):
    def test_first_match_wins(self):
        self.assertEqual(imagefs.match_rule("timelapse/a.mp4", RULES).dest, "timelapse")
        self.assertEqual(imagefs.match_rule("x/y/b.3mf", RULES).dest, "models")

    def test_bare_filename_at_root_matches(self):
        # "**/*.mp4" does not match a root-level "a.mp4" under fnmatch, so
        # match_rule also tries the basename. Regression guard for the case
        # where the printer writes straight to the root of the stick.
        self.assertIsNotNone(imagefs.match_rule("a.mp4", RULES))

    def test_unmatched_file_is_left_alone(self):
        self.assertIsNone(imagefs.match_rule("notes.txt", RULES))


class TestCandidates(unittest.TestCase):
    def test_skips_files_younger_than_min_age(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            fresh = root / "fresh.mp4"
            fresh.write_bytes(b"x")
            old = root / "old.mp4"
            old.write_bytes(b"x")
            import os
            past = time.time() - 3600
            os.utime(old, (past, past))

            found = {p.name for p, _, _ in imagefs.candidates(root, RULES, 600)}
            self.assertEqual(found, {"old.mp4"})

    def test_skips_dotfiles_and_system_volume_information(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            (root / "System Volume Information").mkdir()
            junk = root / "System Volume Information" / "a.mp4"
            junk.write_bytes(b"x")
            import os
            past = time.time() - 3600
            os.utime(junk, (past, past))
            self.assertEqual(list(imagefs.candidates(root, RULES, 600)), [])


class TestDestPaths(unittest.TestCase):
    def test_dest_relpath_is_dated(self):
        # 2026-08-31T12:00:00Z
        ts = 1788134400.0  # 2026-08-31T12:00:00Z
        rel = dest_relpath(Rule("*.mp4", "timelapse"), Path("/x/a.mp4"), ts)
        self.assertTrue(rel.startswith("timelapse/"))
        self.assertTrue(rel.endswith("/a.mp4"))
        self.assertEqual(len(rel.split("/")), 4)

    def test_unique_disambiguates_by_hash(self):
        with TemporaryDirectory() as d:
            p = Path(d) / "a.mp4"
            self.assertEqual(_unique(p, "deadbeefcafe"), p)
            p.write_bytes(b"x")
            self.assertEqual(_unique(p, "deadbeefcafe").name, "a-deadbeef.mp4")


if __name__ == "__main__":
    unittest.main()
