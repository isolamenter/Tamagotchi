from __future__ import annotations

import json
import tarfile
import tempfile
import unittest
from pathlib import Path

from dafeiyu.tools.release import build_release


class ReleaseTests(unittest.TestCase):
    def test_archive_manifest_and_sensitive_file_exclusions(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "dafeiyu.tar.gz"
            manifest = build_release(path)
            with tarfile.open(path, "r:gz") as archive:
                names = archive.getnames()
                self.assertEqual(json.load(archive.extractfile("manifest.json")), manifest)
                self.assertIn("dafeiyu/repositories/schema.sql", names)
                self.assertIn("dafeiyu/web/workspace.js", names)
                self.assertIn("config/persona.toml", names)
                self.assertFalse(
                    any(
                        ".env" in n
                        or n.endswith((".db", ".pyc"))
                        or n.startswith(("tests/", ".docs/"))
                        for n in names
                    )
                )
            with self.assertRaises(FileExistsError):
                build_release(path)
