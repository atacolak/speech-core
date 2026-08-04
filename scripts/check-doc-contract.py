#!/usr/bin/env python3
"""Check Speech Core's required product surfaces and local documentation links."""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")

REQUIRED = {
    "README.md": ("## Start here", "rig.toml", "CHARTER.md"),
    "CHARTER.md": ("## Product authority boundaries", "## Product invariants", "## Amendment authority"),
    "docs/README.md": ("## Canonical product surfaces",),
    "spec/README.md": ("# spec/ — Speech Core's source of truth", "accepted truth"),
    "spec/decisions.md": ("revisit when",),
    "docs/current-state.md": ("# Speech Core current state",),
    "docs/evolution/ACTIVE.md": ("## Current reality", "## Immediate delivery sequence"),
    "rig.toml": ('schema = "ata.reference-rig/v1"', 'prefix = "sc"'),
}

FORBIDDEN_TRACKED = {
    "governance/ROLES.md": "generic roles are city-owned",
    "docs/memory/hindsight-v1.md": "generic memory machinery is city-owned",
    "docs/session-handoff.md": "stale handoffs cannot compete with current state",
    "docs/evolution/00-programme-charter.md": "the root charter is the only product charter",
    "docs/evolution/01-current-baseline.md": "current state has one canonical home",
    "docs/evolution/09-proposed-bead-graph.md": "plans do not pre-create work",
}

errors: list[str] = []

for rel, markers in REQUIRED.items():
    path = ROOT / rel
    if not path.is_file():
        errors.append(f"missing product surface: {rel}")
        continue
    text = path.read_text(encoding="utf-8")
    for marker in markers:
        if marker not in text:
            errors.append(f"{rel} missing marker: {marker}")

try:
    tracked = set(
        subprocess.check_output(
            ["git", "-C", str(ROOT), "ls-files"], text=True
        ).splitlines()
    )
except (OSError, subprocess.CalledProcessError) as exc:
    errors.append(f"cannot inspect tracked paths: {exc}")
    tracked = set()

for rel, reason in FORBIDDEN_TRACKED.items():
    if rel in tracked and (ROOT / rel).exists():
        errors.append(f"forbidden tracked duplicate surface: {rel} ({reason})")

try:
    with (ROOT / "rig.toml").open("rb") as handle:
        manifest = tomllib.load(handle)
    if manifest.get("name") != "speech-core":
        errors.append("rig.toml name must be speech-core")
    roles = manifest.get("roles", {})
    if roles.get("authority") != "city":
        errors.append("rig.toml roles.authority must be city")
    if roles.get("standard") != "ata.city-roles/reference-rig-v1":
        errors.append("rig.toml must pin ata.city-roles/reference-rig-v1")
    memory = manifest.get("memory", {})
    if memory.get("authority") != "reviewed-history-only":
        errors.append("rig.toml memory.authority must be reviewed-history-only")
    if memory.get("standard") != "hindsight-v1":
        errors.append("rig.toml memory.standard must be hindsight-v1")
    if memory.get("bank") != "speech-core":
        errors.append("rig.toml memory.bank must be speech-core")
    if memory.get("status") != "active":
        errors.append('rig.toml memory.status must be "active" (city-managed bank is live)')
except (OSError, tomllib.TOMLDecodeError) as exc:
    errors.append(f"invalid rig.toml: {exc}")

for path in ROOT.rglob("*.md"):
    if any(part in {".git", "target", "vendor", ".beads", ".pi"} for part in path.parts):
        continue
    text = path.read_text(encoding="utf-8", errors="replace")
    for raw in LINK_RE.findall(text):
        target = raw.strip().split()[0].strip("<>")
        if not target or target.startswith("#") or target.startswith("//") or SCHEME_RE.match(target):
            continue
        split = urlsplit(target)
        rel = unquote(split.path)
        if not rel:
            continue
        resolved = (path.parent / rel).resolve()
        try:
            resolved.relative_to(ROOT)
        except ValueError:
            continue
        if not resolved.exists():
            errors.append(f"broken local link in {path.relative_to(ROOT)}: {target}")

if errors:
    for error in errors:
        print(f"speech-core-docs: {error}", file=sys.stderr)
    print(f"speech-core-docs: failed with {len(errors)} error(s)", file=sys.stderr)
    raise SystemExit(1)

print("speech-core-docs: ok — required product markers, forbidden tracked paths, manifest identity, and local links pass")
