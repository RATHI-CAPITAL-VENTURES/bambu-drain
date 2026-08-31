import tempfile
import unittest
from pathlib import Path

from bambu_drain import config


BASE = {
    "gadget": {"image": "/tmp/x.img", "fs": "exfat"},
    "rule": [{"glob": "**/*.mp4", "dest": "timelapse", "delete": True}],
}


class TestConfig(unittest.TestCase):
    def test_paths_become_path_objects(self):
        cfg = config.from_dict(BASE)
        self.assertIsInstance(cfg.gadget.image, Path)
        self.assertEqual(cfg.gadget.image, Path("/tmp/x.img"))

    def test_rules_required(self):
        raw = {"gadget": {}, "rule": []}
        with self.assertRaisesRegex(ValueError, "drain nothing"):
            config.from_dict(raw)

    def test_rejects_unknown_filesystem(self):
        raw = dict(BASE, gadget={"fs": "btrfs"})
        with self.assertRaisesRegex(ValueError, "exfat or fat32"):
            config.from_dict(raw)

    def test_rejects_oversized_inquiry_strings(self):
        # The kernel truncates silently; a surprised printer is worse than a
        # loud config error.
        raw = dict(BASE, gadget={"vendor": "waytoolongvendor"})
        with self.assertRaisesRegex(ValueError, "vendor"):
            config.from_dict(raw)
        raw = dict(BASE, gadget={"product": "x" * 17})
        with self.assertRaisesRegex(ValueError, "product"):
            config.from_dict(raw)

    def test_ledger_sits_beside_the_image(self):
        cfg = config.from_dict(dict(BASE, gadget={"image": "/srv/bd/stick.img"}))
        self.assertEqual(cfg.ledger_path, Path("/srv/bd/ledger.db"))

    def test_example_config_parses(self):
        example = Path(__file__).resolve().parent.parent / "config.example.toml"
        cfg = config.load(example)
        self.assertTrue(cfg.drain.rules)
        # The firmware rule must never delete: the printer needs the file to
        # stay put to apply the update.
        fw = [r for r in cfg.drain.rules if r.dest == "firmware"]
        self.assertTrue(fw and not fw[0].delete)


if __name__ == "__main__":
    unittest.main()
