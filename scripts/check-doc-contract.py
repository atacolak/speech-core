#!/usr/bin/env python3
"""Fail when Speech Core's canonical documentation contract drifts."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

REQUIRED = {
    "README.md": ["## Start here", "docs/README.md", "CHARTER.md"],
    "CHARTER.md": ["## 1. Purpose", "## 3. Claim-scoped authority", "## 8. Amendment and decision authority"],
    "docs/README.md": ["## Canonical homes", "## Cold-entry reading order"],
    "docs/current-state.md": ["**status:** maintained current-implementation guide"],
    "governance/ROLES.md": ["## Rig manager", "## Worker", "## Independent reviewer"],
    "docs/evolution/README.md": ["not a second charter", "## Status semantics"],
    "docs/evolution/ACTIVE.md": ["## Current reality", "## Immediate delivery sequence"],
    "docs/memory/hindsight-v1.md": ["## 3. Terminology", "## 12. Acceptance"],
}

FORBIDDEN_PATHS = {
    ".pi": "repo-local agent/session memory is runtime residue",
    ".checkpoints": "session checkpoints are runtime residue",
    "docs/session-handoff.md": "the README and docs map replace stale handoff copies",
    "docs/evolution/00-programme-charter.md": "the root charter is the only charter",
    "docs/evolution/01-current-baseline.md": "current state has one canonical home",
    "docs/evolution/09-proposed-bead-graph.md": "plans do not pre-create work",
}

ALLOWED_SCHEMES = ("http://", "https://", "mailto:")
LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")

def fail(message: str) -> None:
    print(f"doc-contract: {message}", file=sys.stderr)
    errors.append(message)

errors: list[str] = []

for rel, needles in REQUIRED.items():
    path = ROOT / rel
    if not path.is_file():
        fail(f"missing required canonical document: {rel}")
        continue
    text = path.read_text(encoding="utf-8")
    for needle in needles:
        if needle not in text:
            fail(f"{rel} is missing required marker: {needle}")

for rel, reason in FORBIDDEN_PATHS.items():
    if (ROOT / rel).exists():
        fail(f"forbidden live-tree path exists: {rel} ({reason})")

for path in ROOT.rglob("*.md"):
    if any(part in {".git", "target", "vendor"} for part in path.parts):
        continue
    text = path.read_text(encoding="utf-8", errors="replace")
    for raw_target in LINK_RE.findall(text):
        target = raw_target.strip().split()[0].strip("<>")
        if not target or target.startswith("#") or target.startswith(ALLOWED_SCHEMES):
            continue
        file_target = target.split("#", 1)[0]
        if not file_target:
            continue
        resolved = (path.parent / file_target).resolve()
        try:
            resolved.relative_to(ROOT)
        except ValueError:
            continue
        if not resolved.exists():
            fail(f"broken local link in {path.relative_to(ROOT)}: {target}")

if errors:
    print(f"doc-contract: failed with {len(errors)} error(s)", file=sys.stderr)
    raise SystemExit(1)

print("doc-contract: ok — canonical entry points present, forbidden residue absent, local links resolve")
