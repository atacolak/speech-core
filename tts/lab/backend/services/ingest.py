"""URL ingest: yt-dlp audio-only in, one media source out. not on the voicecat path.

A URL is a source, never a voice: this module downloads audio and hands the file
to `create_media_source`. It never transcribes, never takes a processor lease and
never loads a model. Missing yt-dlp or ffmpeg fails closed instead of fabricating
a source.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs, urlparse

import numpy as np

from tts.lab.backend.audio import WORKING_RATE, decode_to_wav
from tts.lab.backend.services.sources import create_media_source
from tts.lab.backend.store.artifacts import ArtifactStore
from tts.packets import new_id
from tts.wav import is_riff_wav, read_wav

DOWNLOADER = "yt-dlp"
FFMPEG = "ffmpeg"
PEAKS_NAME = "peaks.json"
# One abs-max per window. A 1 h source stays a ~20 kB artifact the bench can draw.
PEAKS_BUCKETS = 2000


class IngestUnavailable(RuntimeError):
    """yt-dlp or ffmpeg is missing. Fail closed, never a fake source."""


class DownloadFailed(ValueError):
    """yt-dlp refused the URL or wrote nothing usable. The caller's URL, not ours."""


class DownloadFn(Protocol):
    """URL + scratch dir in; the audio written there plus its reported metadata out."""

    def __call__(self, url: str, dest_dir: Path) -> dict[str, Any]: ...


def require_tool(name: str) -> str:
    """The absolute path of `name`, or `IngestUnavailable`. Never a fallback."""
    path = shutil.which(name)
    if not path:
        raise IngestUnavailable(f"{name} is not on PATH")
    return path


def check_source_url(url: str) -> str:
    """The trimmed http(s) URL, or ValueError. A downloader only speaks http(s)."""
    candidate = url.strip()
    parsed = urlparse(candidate)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"not an http(s) url: {url!r}")
    return candidate


# The short-link hosts carry the video id in the first path segment.
_SHORT_HOSTS = frozenset({"youtu.be", "www.youtu.be"})
YOUTUBE_HOSTS = _SHORT_HOSTS | frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtube-nocookie.com",
        "www.youtube-nocookie.com",
    }
)
# Path prefixes that carry the video id: /shorts/<id>, /embed/<id>, /live/<id>, /v/<id>.
_ID_PATHS = frozenset({"shorts", "embed", "live", "v"})


def video_id_of(url: str) -> str:
    """The youtube video id `url` names, or "". The `v` query or a path carries it."""
    parsed = urlparse(url)
    segments = [part for part in parsed.path.split("/") if part]
    if segments and parsed.netloc.lower() in _SHORT_HOSTS:
        return segments[0]
    if len(segments) >= 2 and segments[0].lower() in _ID_PATHS:
        return segments[1]
    return (parse_qs(parsed.query).get("v") or [""])[0].strip()


def is_youtube_url(url: str) -> bool:
    """A youtube page that names a video: watch, shorts, embed, live, /v, short link."""
    return urlparse(url).netloc.lower() in YOUTUBE_HOSTS and bool(video_id_of(url))


def source_title(metadata_title: Any, form_title: str, url: str) -> str:
    """yt-dlp's title, else the form's, else the video id, else the URL itself."""
    for candidate in (metadata_title, form_title):
        text = str(candidate or "").strip()
        if text:
            return text
    return video_id_of(url) or url


def peaks_of(samples: np.ndarray, *, buckets: int = PEAKS_BUCKETS) -> list[float]:
    """Abs-max per equal window of int16 PCM, normalized to 0..1.

    Only one window is converted at a time, so a long source never doubles in RAM.
    """
    total = int(samples.size)
    if total == 0:
        return []
    count = min(int(buckets), total)
    edges = np.linspace(0, total, count + 1).astype(int)
    return [
        round(float(np.max(np.abs(samples[start:end].astype(np.float32))) / 32767.0), 5)
        for start, end in zip(edges[:-1], edges[1:])
        if end > start
    ]


def write_peaks(wav_path: Path | str, dest: Path | str) -> Path:
    """Write the `{"peaks": [...]}` waveform artifact for `wav_path`."""
    _, samples = read_wav(wav_path)
    out = Path(dest)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"peaks": peaks_of(samples)}))
    return out


def _last_line(text: str, fallback: str) -> str:
    lines = (text or "").strip().splitlines()
    return lines[-1] if lines else fallback


def _load_info(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        info = json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}
    return info if isinstance(info, dict) else {}


def _downloaded_audio(dest: Path) -> Path:
    for path in sorted(dest.glob("source.*")):
        if not path.name.endswith(".info.json"):
            return path
    raise DownloadFailed(f"{DOWNLOADER} downloaded no audio")


def default_download(url: str, dest_dir: Path) -> dict[str, Any]:
    """yt-dlp audio-only into `dest_dir`. The network boundary: tests fake it out."""
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            require_tool(DOWNLOADER),
            "--no-playlist",
            "-x",
            "--write-info-json",
            "-o",
            str(dest / "source.%(ext)s"),
            url,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise DownloadFailed(
            _last_line(result.stderr or result.stdout, f"{DOWNLOADER} failed")
        )
    info = _load_info(dest / "source.info.json")
    return {
        "path": _downloaded_audio(dest),
        "title": info.get("title"),
        "duration_s": info.get("duration"),
        "origin": info.get("webpage_url"),
    }


# Module hook. Tests swap in a fake downloader: no network, no yt-dlp.
download_hook: DownloadFn = default_download


def _as_wav(audio: Path, tmp: Path) -> Path:
    """A RIFF wav for the store, converting whatever container the site served."""
    if is_riff_wav(audio):
        return audio
    try:
        return decode_to_wav(audio, tmp / "normalized.wav", sample_rate=WORKING_RATE)
    except RuntimeError as exc:
        raise DownloadFailed(str(exc)) from exc


def ingest_url(
    store: ArtifactStore,
    *,
    url: str,
    title: str = "",
    meta: dict[str, Any] | None = None,
    download_fn: DownloadFn | None = None,
) -> str:
    """Download `url` as audio and register the source row. Never transcribes."""
    source_url = check_source_url(url)
    require_tool(DOWNLOADER)
    require_tool(FFMPEG)
    tmp = store.root / "tmp" / f"ingest-{new_id('ing')}"
    try:
        downloaded = (download_fn or download_hook)(source_url, tmp)
        audio = _as_wav(Path(str(downloaded["path"])), tmp)
        peaks = write_peaks(audio, tmp / PEAKS_NAME)
        return create_media_source(
            store,
            kind="youtube" if is_youtube_url(source_url) else "file",
            origin=str(downloaded.get("origin") or source_url),
            title=source_title(downloaded.get("title"), title, source_url),
            audio_path=audio,
            waveform_path=peaks,
            duration_s=downloaded.get("duration_s"),
            meta=meta,
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
