"""not on the voicecat path.

Compact benchmark report writer for sc-breeze-hybrid-81p.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

RECOMMENDATIONS = (
    "SHELF",
    "PROMISING — NEEDS ONE MORE EXPERIMENT",
    "REJECT",
)

_COLUMNS: tuple[tuple[str, str], ...] = (
    ("configuration", "configuration"),
    ("peak_vram_gib", "peak VRAM"),
    ("p50_ttfa_s", "p50 TTFA"),
    ("p95_ttfa_s", "p95 TTFA"),
    ("rtf", "RTF"),
    ("stream_jitter_p95_s", "stream jitter"),
    ("quality_notes", "quality notes"),
)


def sanitize_path(path: str | Path) -> str:
    return str(path).replace(str(Path.home()), "~")


def _sanitize(value: Any) -> Any:
    if isinstance(value, Path):
        return sanitize_path(value)
    if isinstance(value, str):
        return sanitize_path(value)
    if isinstance(value, dict):
        return {k: _sanitize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_sanitize(v) for v in value)
    return value


def render_report(
    rows: list[dict],
    recommendation: str,
    bead: str,
    qual_root: str | Path,
) -> tuple[str, dict]:
    if recommendation not in RECOMMENDATIONS:
        raise ValueError(f"unknown recommendation: {recommendation!r}")

    headers = [label for _, label in _COLUMNS]
    lines = [
        f"# Breeze TTS 2 hybrid report ({bead})",
        "",
        f"Recommendation: {recommendation}",
        "",
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        cells = [sanitize_path(row.get(key, "")) if isinstance(row.get(key, ""), (str, Path)) else str(row.get(key, "")) for key, _ in _COLUMNS]
        lines.append("| " + " | ".join(cells) + " |")
    lines.append("")
    lines.append(f"qual_root: {sanitize_path(qual_root)}")
    lines.append("")
    md = "\n".join(lines)

    payload = {
        "bead": bead,
        "recommendation": recommendation,
        "qual_root": sanitize_path(qual_root),
        "rows": _sanitize(rows),
    }
    return md, payload
