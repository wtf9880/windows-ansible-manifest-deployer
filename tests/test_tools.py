from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from tools.common import ManifestError, load_manifest, parse_checksum_list, selected_paths

TASK_KEYWORDS = {
    "action", "args", "become", "become_user", "changed_when", "delegate_to",
    "environment", "failed_when", "ignore_errors", "loop", "loop_control",
    "name", "no_log", "notify", "register", "run_once", "tags", "vars",
    "when", "with_items",
}


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
                tasks = manifest.get("tasks")
                self.assertIsInstance(tasks, list)
                self.assertTrue(tasks, f"{path}: tasks must not be empty")
                for index, task in enumerate(tasks):
                    with self.subTest(task=index):
                        self.assertIsInstance(task, dict)
                        self.assertIn("name", task)
                        module_keys = [key for key in task if key not in TASK_KEYWORDS]
                        self.assertTrue(module_keys, f"{path}: task {index} defines no module")

    def test_selected_paths_filters_by_stem_or_filename(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for stem in ("alpha", "beta"):
                (root / f"{stem}.yaml").write_text("schema: 1\n", encoding="utf-8")
            self.assertEqual([p.stem for p in selected_paths(root, [])], ["alpha", "beta"])
            self.assertEqual([p.stem for p in selected_paths(root, ["alpha"])], ["alpha"])
            self.assertEqual([p.stem for p in selected_paths(root, ["beta.yaml"])], ["beta"])
            with self.assertRaises(ManifestError):
                selected_paths(root, ["gamma"])

    def test_requires_cache_filename_in_downloader(self) -> None:
        manifest = {
            "schema": 1,
            "name": "bad",
            "source": [],
            "locked": [{"sha256": "a" * 64}],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.yaml"
            path.write_text(yaml.safe_dump(manifest), encoding="utf-8")
            with self.assertRaises(ManifestError):
                load_manifest(path, is_updater=False)

    def test_requires_cache_filename_in_lockupdater(self) -> None:
        manifest = {
            "schema": 1,
            "name": "bad",
            "source": [{"kind": "static_file", "url": "https://example.com/a.zip"}],
            "locked": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.yaml"
            path.write_text(yaml.safe_dump(manifest), encoding="utf-8")
            with self.assertRaises(ManifestError):
                load_manifest(path, is_updater=True)


if __name__ == "__main__":
    unittest.main()
