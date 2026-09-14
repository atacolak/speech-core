#!/usr/bin/env python3
"""React lab shell served from FastAPI. not on the voicecat path."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO))

from tts.lab.backend.app import create_app


class WebServe(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        dist = self.root / "dist"
        (dist / "assets").mkdir(parents=True)
        (dist / "index.html").write_text(
            "<!doctype html><html><head><title>TTS lab</title>"
            '<script type="module" src="/assets/index.js"></script></head>'
            "<body><div id='root'>TTS lab</div></body></html>",
            encoding="utf-8",
        )
        (dist / "assets" / "index.js").write_text("console.log('tts-lab')\n", encoding="utf-8")
        self.client = TestClient(
            create_app(root=self.root, leftover_parked=True, web_dist=dist)
        )

    def tearDown(self) -> None:
        self.client.close()
        self.tmp.cleanup()

    def test_index_and_runtime_share_origin(self) -> None:
        home = self.client.get("/")
        self.assertEqual(home.status_code, 200)
        self.assertIn("TTS lab", home.text)
        runtime = self.client.get("/api/runtime")
        self.assertEqual(runtime.status_code, 200)
        body = runtime.json()
        self.assertEqual(body["selected"], "E2")
        self.assertTrue(body["leftover_parked"])
        self.assertTrue(body["not_a_pin_swap"])
        asset = self.client.get("/assets/index.js")
        self.assertEqual(asset.status_code, 200)
        self.assertIn("tts-lab", asset.text)


if __name__ == "__main__":
    unittest.main()
