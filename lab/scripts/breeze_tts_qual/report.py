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

TTFA_ACCEPTABLE_S = 0.250
RTF_REQUIRED = 1.0
VRAM_ACCEPTABLE_GIB = 9.0
SHELF_CANDIDATE_NAMES = (
    "C0", "C1", "C2", "C3", "C4", "B_winner",
    "E1", "E2", "E3", "E4", "E5", "E_winner",
)
_TABLE_ORDER = (
    "A",
    "B_depth",
    "B_codec",
    "B_backbone_decode",
    "B_backbone_prefill",
    "B_winner",
    "C0",
    "C1",
    "C2",
    "C3",
    "C4",
    "E1",
    "E2",
    "E3",
    "E4",
    "E5",
    "D",
)
_HYBRID_NAMES = ("C0", "C1", "C2", "C3", "C4")
_E_NAMES = ("E1", "E2", "E3", "E4", "E5")
_STAGE_KEYS = (
    "text encoder",
    "backbone prefill",
    "first backbone decode",
    "depth decode",
    "codec",
    "pcm emission",
)
_SHELF_MEANING = (
    "SHELF means the best config is characterized and the bead is parked "
    "for a later resource-allocation decision. It is not permission to "
    "integrate, propose a pin swap, or spawn a follow-on wiring bead. "
    "Recommendation is against this experiment's own A, not leftover."
)
_LEFTOVER_COMMENTARY = (
    "Leftover qwentts first-usable is ~70 ms / ~2.4 GB (commentary only; "
    "not a pin-swap gate). A win vs leftover still parks the bead."
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


def choose_recommendation(rows: list[dict], *, quality_ok: bool) -> str:
    by = {r["configuration"]: r for r in rows if not r.get("not_run")}
    a = by["A"]

    def shelf_quality(row: dict) -> bool:
        return (
            quality_ok
            and float(row["p50_ttfa_s"]) < TTFA_ACCEPTABLE_S
            and float(row["rtf"]) < RTF_REQUIRED
            and float(row["peak_vram_gib"]) <= VRAM_ACCEPTABLE_GIB
        )

    for name in SHELF_CANDIDATE_NAMES:
        row = by.get(name)
        if row is not None and shelf_quality(row):
            return "SHELF"

    non_a = [r for name, r in by.items() if name != "A"]
    rtf_ok = any(float(r["rtf"]) < RTF_REQUIRED for r in non_a)
    material_win = any(
        float(r["p50_ttfa_s"]) < float(a["p50_ttfa_s"])
        and float(r["peak_vram_gib"]) <= VRAM_ACCEPTABLE_GIB
        for r in non_a
    )
    if (not non_a) or (not rtf_ok) or (not quality_ok) or (not material_win):
        return "REJECT"
    return "PROMISING — NEEDS ONE MORE EXPERIMENT"


def _maybe_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def peak_vram_gib(row: dict) -> float | None:
    peak = _maybe_float(row.get("peak_vram_gib"))
    if peak is not None:
        return peak
    peak = _maybe_float(row.get("peak_allocated_gib"))
    if peak is not None:
        return peak
    mb = _maybe_float(row.get("peak_allocated_mb"))
    if mb is not None:
        return mb / 1024.0
    return None


def p50_ttfa_s(row: dict) -> float | None:
    value = _maybe_float(row.get("p50_ttfa_s"))
    if value is not None:
        return value
    classes = row.get("classes") or {}
    short = classes.get("short") if isinstance(classes, dict) else None
    if isinstance(short, dict):
        value = _maybe_float(short.get("p50_ttfa_s"))
        if value is not None:
            return value
    return _maybe_float(row.get("first_nonsilent_s"))


def quality_ok_from_notes(direction_notes: dict | None) -> bool:
    if not isinstance(direction_notes, dict):
        return True
    labels: list[str] = []
    for key, value in direction_notes.items():
        if key in {"listened", "baseline", "labels"}:
            continue
        if isinstance(value, dict):
            labels.extend(str(item) for item in value.values())
        elif value is not None:
            labels.append(str(value))
    if any(label == "weakened" for label in labels):
        return False
    return True


def _quality_notes(name: str, row: dict, direction_notes: dict | None) -> str:
    parts: list[str] = []
    stop = row.get("stop_reason")
    if stop:
        parts.append(str(stop))
    if row.get("unsafe_vram") and str(stop) != "unsafe_vram":
        parts.append("unsafe_vram")
    if row.get("not_run") and not stop:
        parts.append("not_run")
    if isinstance(direction_notes, dict):
        notes = direction_notes.get(name)
        if isinstance(notes, dict):
            unique = sorted({str(v) for v in notes.values()})
            if unique == ["comparable"]:
                parts.append("comparable vs A")
            elif unique:
                parts.append(", ".join(unique) + " vs A")
        elif name == "A":
            parts.append("baseline")
    elif name == "A":
        parts.append("baseline")
    return "; ".join(parts)


def _in_envelope(row: dict) -> bool:
    if row.get("not_run") or row.get("unsafe_vram"):
        return False
    peak = peak_vram_gib(row)
    ttfa = p50_ttfa_s(row)
    if peak is None or ttfa is None:
        return False
    return peak <= VRAM_ACCEPTABLE_GIB


def _best_named(configs: dict, names: tuple[str, ...]) -> str | None:
    best_name: str | None = None
    best_ttfa: float | None = None
    for name in names:
        row = configs.get(name)
        if not isinstance(row, dict) or not _in_envelope(row):
            continue
        ttfa = p50_ttfa_s(row)
        if ttfa is None:
            continue
        if best_ttfa is None or ttfa < best_ttfa:
            best_name = name
            best_ttfa = ttfa
    return best_name


def _recommendation_row(name: str, src: dict) -> dict | None:
    ttfa = p50_ttfa_s(src)
    rtf = _maybe_float(src.get("rtf"))
    if rtf is None:
        classes = src.get("classes") or {}
        short = classes.get("short") if isinstance(classes, dict) else None
        if isinstance(short, dict):
            rtf = _maybe_float(short.get("rtf"))
    peak = peak_vram_gib(src)
    if ttfa is None or rtf is None or peak is None:
        return None
    return {
        "configuration": name,
        "p50_ttfa_s": ttfa,
        "rtf": rtf,
        "peak_vram_gib": peak,
        "not_run": bool(src.get("not_run")),
    }


def _table_row(name: str, src: dict | None, direction_notes: dict | None) -> dict:
    src = src or {}
    jitter = src.get("stream_jitter_p95_s")
    if jitter is None:
        jitter = src.get("gap_p95_s")
    p95 = src.get("p95_ttfa_s")
    classes = src.get("classes") or {}
    short = classes.get("short") if isinstance(classes, dict) else None
    if p95 is None and isinstance(short, dict):
        p95 = short.get("p95_ttfa_s")
    if jitter is None and isinstance(short, dict):
        jitter = short.get("gap_p95_s")
    def _cell(value: Any) -> Any:
        if value is None:
            return ""
        return value

    rtf = src.get("rtf")
    if rtf is None and isinstance(short, dict):
        rtf = short.get("rtf")
    notes = _quality_notes(name, src, direction_notes)
    return {
        "configuration": name,
        "peak_vram_gib": _cell(peak_vram_gib(src)),
        "p50_ttfa_s": _cell(p50_ttfa_s(src)),
        "p95_ttfa_s": _cell(_maybe_float(p95)),
        "rtf": _cell(_maybe_float(rtf)),
        "stream_jitter_p95_s": _cell(_maybe_float(jitter)),
        "quality_notes": notes,
        "not_run": bool(src.get("not_run")),
        "stop_reason": src.get("stop_reason"),
    }


def _stage_trace(metrics: dict) -> dict:
    configs = metrics.get("configs") or {}
    for row in configs.values():
        if not isinstance(row, dict):
            continue
        timing = row.get("stage_trace") or row.get("collect_timing") or row.get("timing")
        if isinstance(timing, dict) and any(
            key in timing for key in ("text_encoder", "text encoder", "backbone_prefill", "depth_decode", "codec")
        ):
            return {"trace_unavailable": False, "stages": _sanitize(timing)}
    coarse: dict[str, dict] = {}
    for name in ("A", "C2", "E2", "E1", "B_depth"):
        row = configs.get(name)
        if not isinstance(row, dict):
            continue
        clocks = {
            key: row.get(key)
            for key in ("first_model_output_s", "first_codec_frame_s", "first_pcm_s", "first_nonsilent_s")
            if row.get(key) is not None
        }
        if clocks:
            coarse[name] = clocks
    return {
        "trace_unavailable": True,
        "reason": (
            "full-30 metrics.json does not persist collect_timing stage "
            "breakdown (" + ", ".join(_STAGE_KEYS) + ")."
        ),
        "coarse_clocks": coarse,
    }


def _fmt(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


def compact_from_metrics(metrics: dict, *, qual_root: str | Path, bead: str = "sc-breeze-hybrid-81p") -> tuple[str, dict]:
    configs = metrics.get("configs") or {}
    direction_notes = metrics.get("direction_notes") if isinstance(metrics.get("direction_notes"), dict) else {}
    table_rows = [_table_row(name, configs.get(name), direction_notes) for name in _TABLE_ORDER]
    rec_rows = []
    rec_names = ("A",) + SHELF_CANDIDATE_NAMES
    seen: set[str] = set()
    for name in rec_names:
        if name in seen:
            continue
        seen.add(name)
        src = configs.get(name)
        if not isinstance(src, dict):
            continue
        mapped = _recommendation_row(name, src)
        if mapped is not None:
            rec_rows.append(mapped)
    quality_ok = quality_ok_from_notes(direction_notes)
    recommendation = choose_recommendation(rec_rows, quality_ok=quality_ok)
    best_e = _best_named(configs, _E_NAMES)
    best_hybrid = _best_named(configs, _HYBRID_NAMES)
    e_rejected = best_e is None
    stage_trace = _stage_trace(metrics)

    extra_lines = [
        _SHELF_MEANING if recommendation == "SHELF" else (
            "This recommendation is against this experiment's own A, not leftover. "
            "It is not permission to integrate, propose a pin swap, or spawn a follow-on wiring bead."
        ),
        "",
        "## Best ≤9 GB E vs best ≤9 GB hybrid",
        "",
        f"best_e_le_9gib: {best_e or 'none'}",
        f"best_hybrid_le_9gib: {best_hybrid or 'none'}",
    ]
    if e_rejected:
        extra_lines.append("e_rejected_for_our_purposes: true")
    else:
        extra_lines.append("e_rejected_for_our_purposes: false")
        e_row = _table_row(best_e, configs.get(best_e), direction_notes) if best_e else None
        h_row = _table_row(best_hybrid, configs.get(best_hybrid), direction_notes) if best_hybrid else None
        if e_row and h_row:
            extra_lines.extend(
                [
                    "",
                    (
                        f"{best_e} p50 TTFA {_fmt(e_row['p50_ttfa_s'])}s / RTF {_fmt(e_row['rtf'])} / "
                        f"peak {_fmt(e_row['peak_vram_gib'])} GiB vs {best_hybrid} p50 TTFA "
                        f"{_fmt(h_row['p50_ttfa_s'])}s / RTF {_fmt(h_row['rtf'])} / peak "
                        f"{_fmt(h_row['peak_vram_gib'])} GiB."
                    ),
                ]
            )
    extra_lines.extend(["", "## Leftover (commentary only)", "", _LEFTOVER_COMMENTARY, "", "## First-audio stage trace", ""])
    if stage_trace.get("trace_unavailable"):
        extra_lines.append(f"trace_unavailable: {stage_trace.get('reason')}")
        coarse = stage_trace.get("coarse_clocks") or {}
        for name, clocks in coarse.items():
            extra_lines.append(
                f"{name}: first_model_output_s={_fmt(clocks.get('first_model_output_s'))} "
                f"first_codec_frame_s={_fmt(clocks.get('first_codec_frame_s'))} "
                f"first_pcm_s={_fmt(clocks.get('first_pcm_s'))} "
                f"first_nonsilent_s={_fmt(clocks.get('first_nonsilent_s'))}"
            )
    else:
        extra_lines.append(str(stage_trace.get("stages") or {}))
    extra_md = "\n".join(extra_lines)

    md, payload = render_report(
        rows=table_rows,
        recommendation=recommendation,
        bead=bead,
        qual_root=qual_root,
    )
    md = md.rstrip() + "\n\n" + extra_md + "\n"
    payload.update(
        {
            "best_e_le_9gib": best_e,
            "best_hybrid_le_9gib": best_hybrid,
            "e_rejected_for_our_purposes": e_rejected,
            "stage_trace": _sanitize(stage_trace),
            "leftover_commentary": _LEFTOVER_COMMENTARY,
            "shelf_meaning": _SHELF_MEANING if recommendation == "SHELF" else None,
            "quality_ok": quality_ok,
        }
    )
    return md, _sanitize(payload)

