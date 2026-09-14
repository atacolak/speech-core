#!/usr/bin/env python3
"""Source URL ingest: yt-dlp audio in, a peaks artifact out, never ASR.

A URL is a SOURCE (kind=youtube for youtube hosts), never a voice. The downloader
is faked here: these tests never touch the network, never take a processor lease
and never run the analyzer.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO))

from tts.lab.backend.app import create_app
from tts.lab.backend.audio import WORKING_RATE
from tts.lab.backend.services.ingest import DownloadFailed
from tts.lab.backend.tests.test_source_lab_schema import _RecordingAnalyzer, _wav
from tts.wav import read_wav

INGEST_MODULE = "tts.lab.backend.services.ingest"
DOWNLOAD_HOOK = f"{INGEST_MODULE}.download_hook"
ANALYZE_HOOK = "tts.lab.backend.services.speakers.analyze_hook"
WATCH_URL = "https://www.youtube.com/watch?v=fixture"
_real_run = subprocess.run
PEAKS_BUCKETS = 2000


class _FakeDownloader:
    """Fake yt-dlp: writes the fixture audio into the scratch dir, reports metadata."""

    def __init__(
        self,
        *,
        seconds: float = 6.0,
        title: str | None = "Westworld 01",
        error: Exception | None = None,
    ) -> None:
        self.seconds = seconds
        self.title = title
        self.error = error
        self.duration_s = 12.5
        self.calls: list[tuple[str, Path]] = []

    def __call__(self, url: str, dest_dir: Path) -> dict:
        dest_dir = Path(dest_dir)
        self.calls.append((url, dest_dir))
        if self.error is not None:
            raise self.error
        path = dest_dir / "source.wav"
        _wav(path, seconds=self.seconds)
        return {
            "path": path,
            "title": self.title,
            "duration_s": self.duration_s,
            "origin": url,
        }


class _GarbageDownloader(_FakeDownloader):
    """A download that is not audio at all: the store must not keep it."""

    def __call__(self, url: str, dest_dir: Path) -> dict:
        dest_dir = Path(dest_dir)
        self.calls.append((url, dest_dir))
        path = dest_dir / "source.m4a"
        path.write_bytes(b"not audio")
        return {
            "path": path,
            "title": self.title,
            "duration_s": None,
            "origin": url,
        }


class _M4aDownloader(_FakeDownloader):
    """The container yt-dlp usually hands back: source.m4a, not source.wav."""

    def __call__(self, url: str, dest_dir: Path) -> dict:
        dest_dir = Path(dest_dir)
        self.calls.append((url, dest_dir))
        wav = dest_dir / "source.wav"
        _wav(wav, seconds=self.seconds)
        out = dest_dir / "source.m4a"
        subprocess.run(
            [
                shutil.which("ffmpeg") or "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-i",
                str(wav),
                "-c:a",
                "aac",
                "-b:a",
                "64k",
                str(out),
            ],
            check=True,
        )
        wav.unlink()
        return {
            "path": out,
            "title": self.title,
            "duration_s": self.duration_s,
            "origin": url,
        }


class _IngestCase(unittest.TestCase):
    """A lab app on a throwaway root, plus the fakes a source ingest needs."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.client = TestClient(create_app(root=self.root, leftover_parked=True))
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.client.close)

    def _ingest(self, downloader: _FakeDownloader, url: str = WATCH_URL, **form: str):
        analyzer = _RecordingAnalyzer()
        with patch(DOWNLOAD_HOOK, downloader), patch(ANALYZE_HOOK, analyzer):
            response = self.client.post("/api/sources", data={"url": url, **form})
        return response, analyzer

    def _artifact_text(self, artifact_id: str, suffix: str) -> str:
        return (self.root / "objects" / f"{artifact_id}{suffix}").read_text()


class SourceUrlIngest(_IngestCase):
    def test_a_youtube_url_ingests_audio_without_any_analysis(self) -> None:
        downloader = _FakeDownloader()

        response, analyzer = self._ingest(downloader)

        self.assertEqual(response.status_code, 200, response.text)
        source = response.json()
        self.assertEqual(source["kind"], "youtube")
        self.assertEqual(source["origin"], WATCH_URL)
        self.assertEqual(source["title"], "Westworld 01")
        self.assertEqual(source["duration_s"], downloader.duration_s)
        self.assertIsNotNone(source["audio_artifact_id"])
        self.assertIsNotNone(source["waveform_artifact_id"])
        self.assertNotEqual(source["audio_artifact_id"], source["waveform_artifact_id"])
        self.assertEqual(source["coverage"], [])
        self.assertEqual(source["speakers"], [])
        self.assertEqual(source["clips"], [])
        self.assertEqual(source["analyses"], [])
        self.assertEqual(analyzer.paths, [])
        self.assertNotIn("SPEAKER_", response.text)
        self.assertEqual([url for url, _ in downloader.calls], [WATCH_URL])
        # The scratch dir the downloader wrote into is gone once the source owns the bytes.
        self.assertFalse(downloader.calls[0][1].exists())

        audio = self.client.get(f"/api/sources/{source['id']}/audio")
        self.assertEqual(audio.status_code, 200, audio.text)
        self.assertEqual(audio.content[:4], b"RIFF")

    def test_the_peaks_artifact_summarizes_the_downloaded_audio(self) -> None:
        response, _ = self._ingest(_FakeDownloader())
        self.assertEqual(response.status_code, 200, response.text)

        peaks = json.loads(
            self._artifact_text(response.json()["waveform_artifact_id"], ".json")
        )

        self.assertEqual(len(peaks["peaks"]), PEAKS_BUCKETS)
        # One abs-max per window of a constant 0.1-amplitude tone: every bucket carries it.
        self.assertGreater(min(peaks["peaks"]), 0.09)
        self.assertLessEqual(max(peaks["peaks"]), 0.1)


class SourceUrlIngestTools(_IngestCase):
    """A host without the ingest tools answers 501 and writes nothing."""

    def _post_without(self, tool: str):
        downloader = _FakeDownloader()
        only_tools = lambda name: None if name == tool else f"/usr/bin/{name}"  # noqa: E731
        with patch(DOWNLOAD_HOOK, downloader), patch("shutil.which", only_tools):
            response = self.client.post("/api/sources", data={"url": WATCH_URL})
        return response, downloader

    def test_a_missing_downloader_is_501_and_ingests_nothing(self) -> None:
        response, downloader = self._post_without("yt-dlp")

        self.assertEqual(response.status_code, 501, response.text)
        self.assertEqual(response.json()["detail"]["code"], "ingest_unavailable")
        self.assertEqual(downloader.calls, [])
        self.assertEqual(self.client.get("/api/sources").json()["items"], [])

    def test_a_missing_ffmpeg_is_501_and_ingests_nothing(self) -> None:
        response, downloader = self._post_without("ffmpeg")

        self.assertEqual(response.status_code, 501, response.text)
        self.assertEqual(response.json()["detail"]["code"], "ingest_unavailable")
        self.assertEqual(downloader.calls, [])
        self.assertEqual(self.client.get("/api/sources").json()["items"], [])


class SourceUrlIngestRefused(_IngestCase):
    """A URL that cannot become a source fails closed before anything is written."""

    def test_a_url_that_is_not_http_is_refused_before_the_downloader_runs(self) -> None:
        for bad in ("not a url", "/media/ww.wav", "ftp://example.test/ww.wav"):
            with self.subTest(bad=bad):
                downloader = _FakeDownloader()
                with patch(DOWNLOAD_HOOK, downloader):
                    response = self.client.post("/api/sources", data={"url": bad})

                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(downloader.calls, [])
                self.assertEqual(self.client.get("/api/sources").json()["items"], [])

    def test_a_refused_download_leaves_no_source_behind(self) -> None:
        downloader = _FakeDownloader(error=DownloadFailed("ERROR: video unavailable"))
        with patch(DOWNLOAD_HOOK, downloader):
            response = self.client.post("/api/sources", data={"url": WATCH_URL})

        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("video unavailable", response.json()["detail"])
        self.assertEqual(self.client.get("/api/sources").json()["items"], [])

    @unittest.skipUnless(shutil.which("ffmpeg"), "decoding the garbage fixture needs ffmpeg")
    def test_audio_that_cannot_be_decoded_leaves_no_source_behind(self) -> None:
        with patch(DOWNLOAD_HOOK, _GarbageDownloader()):
            response = self.client.post("/api/sources", data={"url": WATCH_URL})

        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.client.get("/api/sources").json()["items"], [])


class SourceUrlIngestTitle(_IngestCase):
    """A source needs a title: metadata, else the form, else the video id."""

    def test_the_form_title_beats_a_titleless_download(self) -> None:
        response, _ = self._ingest(_FakeDownloader(title="  "), title="westworld rough cut")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["title"], "westworld rough cut")

    def test_a_titleless_source_is_titled_by_the_video_id(self) -> None:
        response, _ = self._ingest(_FakeDownloader(title=None))

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["title"], "fixture")

    def test_a_short_link_is_titled_by_the_id_in_its_path(self) -> None:
        for link in (
            "https://youtu.be/fixture",
            "https://www.youtube.com/shorts/fixture",
        ):
            with self.subTest(link=link):
                response, _ = self._ingest(_FakeDownloader(title=""), url=link)

                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["title"], "fixture")


class SourceUrlIngestKind(_IngestCase):
    """A youtube page that names a video is kind=youtube; any other URL is a file."""

    def test_the_page_decides_whether_a_url_is_a_youtube_source(self) -> None:
        for link, kind in (
            ("https://www.youtube.com/watch?v=fixture", "youtube"),
            ("https://youtu.be/fixture", "youtube"),
            ("https://m.youtube.com/watch?v=fixture", "youtube"),
            ("https://www.youtube.com/shorts/fixture", "youtube"),
            ("https://example.test/ww.wav", "file"),
            ("https://www.youtube.com/feed/subscriptions", "file"),
        ):
            with self.subTest(link=link):
                response, _ = self._ingest(_FakeDownloader(), url=link)

                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["kind"], kind)


@unittest.skipUnless(shutil.which("ffmpeg"), "the m4a fixture is built with local ffmpeg")
class SourceUrlIngestTranscode(_IngestCase):
    """yt-dlp hands back whatever container the site serves: ingest normalizes it."""

    def test_a_non_wav_download_becomes_a_wav_source(self) -> None:
        with patch(DOWNLOAD_HOOK, _M4aDownloader()):
            response = self.client.post("/api/sources", data={"url": WATCH_URL})

        self.assertEqual(response.status_code, 200, response.text)
        audio = self.client.get(f"/api/sources/{response.json()['id']}/audio")
        self.assertEqual(audio.content[:4], b"RIFF")
        stored = next((self.root / "objects").glob(f"{response.json()['audio_artifact_id']}.*"))
        sample_rate, samples = read_wav(stored)
        self.assertEqual(sample_rate, WORKING_RATE)
        self.assertAlmostEqual(samples.size / sample_rate, 6.0, delta=0.2)


def _run_as_ytdlp(argv: list[str], **kwargs):
    """Stand in for the yt-dlp process: honor `-o`, leave the files yt-dlp leaves."""
    if Path(str(argv[0])).name != "yt-dlp":
        return _real_run(argv, **kwargs)
    template = Path(str(argv[argv.index("-o") + 1]))
    dest = template.parent
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "source.info.json").write_text(
        json.dumps(
            {
                "title": "Westworld 01 - The Original",
                "duration": 12.5,
                "webpage_url": WATCH_URL,
            }
        )
    )
    _wav(template.with_suffix(".m4a"), seconds=6.0)
    return subprocess.CompletedProcess(argv, 0, "", "")


@unittest.skipUnless(
    shutil.which("yt-dlp") and shutil.which("ffmpeg"),
    "the real downloader contract needs yt-dlp and ffmpeg on PATH",
)
class RealDownloaderContract(_IngestCase):
    """The real `default_download`, stopped at the subprocess boundary: no network."""

    def test_the_real_downloader_reports_what_ingest_reads(self) -> None:
        with patch(f"{INGEST_MODULE}.subprocess.run", _run_as_ytdlp):
            response = self.client.post("/api/sources", data={"url": WATCH_URL})

        self.assertEqual(response.status_code, 200, response.text)
        source = response.json()
        self.assertEqual(source["kind"], "youtube")
        self.assertEqual(source["title"], "Westworld 01 - The Original")
        self.assertEqual(source["duration_s"], 12.5)
        self.assertEqual(source["origin"], WATCH_URL)
        self.assertIsNotNone(source["waveform_artifact_id"])
        audio = self.client.get(f"/api/sources/{source['id']}/audio")
        self.assertEqual(audio.content[:4], b"RIFF")


class LocalFileIngestStaysOffline(_IngestCase):
    """The upload path is unchanged: no downloader, no network, still 200."""

    def test_a_file_upload_ingests_without_the_downloader(self) -> None:
        downloader = _FakeDownloader()
        wav = _wav(self.root / "austin-2026-01.wav")
        with patch(DOWNLOAD_HOOK, downloader), wav.open("rb") as handle:
            response = self.client.post(
                "/api/sources",
                data={"meta": '{"channel": "ww"}'},
                files={"file": ("austin-2026-01.wav", handle, "audio/wav")},
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["kind"], "file")
        self.assertEqual(response.json()["title"], "austin-2026-01")
        self.assertEqual(response.json()["meta"], {"channel": "ww"})
        self.assertEqual(downloader.calls, [])


if __name__ == "__main__":
    unittest.main()
