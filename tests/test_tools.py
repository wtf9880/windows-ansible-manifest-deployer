from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from tools.common import ManifestError, load_manifest, parse_checksum_list


class CommonTests(unittest.TestCase):
    def test_parse_checksum_list(self) -> None:
        text = "a" * 64 + "  other.zip\n" + "B" * 64 + " *wanted.zip\n"
        self.assertEqual(parse_checksum_list(text, "wanted.zip"), "b" * 64)

    def test_missing_checksum(self) -> None:
        with self.assertRaises(ManifestError):
            parse_checksum_list("a" * 64 + "  other.zip\n", "wanted.zip")

    def test_all_repository_manifests_validate(self) -> None:
        for path in sorted(Path("manifests").glob("*.yaml")):
            with self.subTest(path=path):
                manifest = load_manifest(path)
                self.assertIn(manifest["deploy"]["type"], {"portable_zip", "copy", "installer"})

    def test_rejects_cache_path_traversal(self) -> None:
        manifest = {
            "schema": 1,
            "name": "bad",
            "cache_filename": "../bad.exe",
            "source": {},
            "locked": {"sha256": "a" * 64},
            "deploy": {},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.yaml"
            path.write_text(yaml.safe_dump(manifest), encoding="utf-8")
            with self.assertRaises(ManifestError):
                load_manifest(path)


if __name__ == "__main__":
    unittest.main()
