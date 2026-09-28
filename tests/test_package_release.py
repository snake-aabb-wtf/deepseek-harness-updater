"""Verify that release assets are self-contained and match their checksum file."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile

from scripts.package_release import package_release, sha256


class PackageReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "dist").mkdir()
        (self.root / "docs").mkdir()
        (self.root / "dist/HarnessUpdater.exe").write_bytes(b"fake executable\n")
        (self.root / "docs/使用说明.txt").write_text("测试说明\n", encoding="utf-8")

    def test_archive_and_manifest_match_deliverables(self) -> None:
        executable, archive, checksums = package_release(self.root, "v0.1.0", "0.1.0")

        self.assertEqual(archive.name, "HarnessUpdater-0.1.0-Windows-x64.zip")
        with ZipFile(archive) as bundle:
            self.assertEqual(bundle.namelist(), ["HarnessUpdater.exe", "使用说明.txt"])
            self.assertEqual(bundle.read("HarnessUpdater.exe"), executable.read_bytes())
            self.assertEqual(bundle.read("使用说明.txt"), (self.root / "docs/使用说明.txt").read_bytes())
        self.assertEqual(
            checksums.read_text(encoding="ascii"),
            f"{sha256(executable)}  {executable.name}\n{sha256(archive)}  {archive.name}\n",
        )

    def test_mismatched_version_does_not_publish_files(self) -> None:
        with self.assertRaisesRegex(ValueError, "不一致"):
            package_release(self.root, "v0.2.0", "0.1.0")
        self.assertFalse((self.root / "dist/SHA256SUMS.txt").exists())


if __name__ == "__main__":
    unittest.main()
