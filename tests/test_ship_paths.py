import shlex
import unittest

from bambu_drain.ship import remote_path

ICLOUD = "~/Library/Mobile Documents/com~apple~CloudDocs/BambuArchive"


class TestRemotePath(unittest.TestCase):
    """The iCloud destination contains both hazards at once: a leading tilde
    the remote shell must expand, and spaces it must not word-split on."""

    def test_tilde_stays_unquoted_so_the_shell_expands_it(self):
        out = remote_path(ICLOUD, "timelapse/2026/08/a.mp4")
        self.assertTrue(out.startswith("~/"))
        self.assertNotIn("'~", out)

    def test_spaces_are_quoted(self):
        out = remote_path(ICLOUD, "a.mp4")
        self.assertIn("'", out)
        # Everything after the tilde must be inside one quoted run.
        self.assertEqual(out.count("'"), 2)

    def test_absolute_destination_is_fully_quoted(self):
        out = remote_path("/Volumes/Big Disk/Archive", "a.mp4")
        self.assertEqual(out, "'/Volumes/Big Disk/Archive/a.mp4'")

    def test_trailing_slash_does_not_double(self):
        self.assertNotIn("//", remote_path("/tmp/arch/", "a.mp4"))

    def test_shell_metacharacters_cannot_escape(self):
        # The real invariant is not "the text is absent" — it is that the
        # remote shell parses the whole thing as exactly one argument.
        evil = "a; rm -rf ~/.mp4"
        out = remote_path("/tmp/arch", evil)
        self.assertEqual(shlex.split(out), [f"/tmp/arch/{evil}"])

    def test_icloud_path_parses_as_one_argument(self):
        out = remote_path(ICLOUD, "timelapse/2026/08/a.mp4")
        self.assertEqual(len(shlex.split(out)), 1)


if __name__ == "__main__":
    unittest.main()
