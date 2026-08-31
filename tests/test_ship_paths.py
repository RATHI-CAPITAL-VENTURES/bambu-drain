import shlex
import unittest

from bambu_drain.ship import remote_abs, shell_arg

ICLOUD = "~/Library/Mobile Documents/com~apple~CloudDocs/BambuArchive"
HOME = "/Users/ishan"


class TestRemoteAbs(unittest.TestCase):
    """The destination has a leading tilde and embedded spaces, and it is
    consumed two different ways. Getting the rules backwards is silent: the
    quotes end up in the filename."""

    def test_tilde_is_expanded_not_passed_through(self):
        # rsync never sees a tilde, so it never has to expand one.
        out = remote_abs(ICLOUD, "timelapse/a.mp4", HOME)
        self.assertTrue(out.startswith("/Users/ishan/Library/Mobile Documents/"))
        self.assertNotIn("~/", out)

    def test_rsync_destination_is_not_quoted(self):
        # openrsync (protocol 29) has no --protect-args; a quoted destination
        # lands the quote characters in the filename.
        out = remote_abs(ICLOUD, "a.mp4", HOME)
        self.assertNotIn("'", out)
        self.assertIn(" ", out)  # spaces survive raw

    def test_bare_tilde_destination(self):
        self.assertEqual(remote_abs("~", "a.mp4", HOME), "/Users/ishan/a.mp4")

    def test_absolute_destination_untouched(self):
        self.assertEqual(
            remote_abs("/Volumes/Big Disk/Archive", "a.mp4", HOME),
            "/Volumes/Big Disk/Archive/a.mp4",
        )

    def test_empty_rel_returns_the_base(self):
        # Used for `mkdir -p <base>` and for the retention `find` root.
        self.assertEqual(remote_abs("/tmp/arch/", "", HOME), "/tmp/arch")

    def test_trailing_slash_does_not_double(self):
        self.assertNotIn("//", remote_abs("/tmp/arch/", "a.mp4", HOME))


class TestShellArg(unittest.TestCase):
    """Shell commands (mkdir, shasum, find) DO need quoting."""

    def test_spaces_parse_as_one_argument(self):
        path = remote_abs(ICLOUD, "timelapse/a.mp4", HOME)
        self.assertEqual(shlex.split(shell_arg(path)), [path])

    def test_metacharacters_cannot_escape(self):
        path = remote_abs("/tmp/arch", "a; rm -rf important", HOME)
        self.assertEqual(shlex.split(shell_arg(path)), [path])
        self.assertEqual(len(shlex.split(shell_arg(path))), 1)


if __name__ == "__main__":
    unittest.main()
