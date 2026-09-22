#!/usr/bin/env python3
"""Lab SPA paths. leftover hop frozen. not on the voicecat path."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO))

from tts.lab.backend.app import create_app
from tts.lab.backend.web import DEFAULT_WEB_DIST


class LabShellPaths(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.client = TestClient(create_app(root=Path(self.tmp.name)))

    def tearDown(self) -> None:
        self.client.close()
        self.tmp.cleanup()

    def test_public_paths_serve_the_spa_index(self) -> None:
        if not (DEFAULT_WEB_DIST / "index.html").is_file():
            self.skipTest("lab web dist is not built")
        for path in ("/", "/desk", "/generate", "/conversations", "/lab"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertIn("<div id=\"root\"></div>", response.text)
                self.assertEqual(response.headers.get("cache-control"), "no-store, max-age=0")

    def test_api_conversations_is_not_the_spa(self) -> None:
        response = self.client.get("/api/conversations")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("items", response.json())


if __name__ == "__main__":
    unittest.main()
