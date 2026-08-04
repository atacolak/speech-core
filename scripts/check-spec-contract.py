#!/usr/bin/env python3
"""Check Speech Core's spec/ contract guarantees (placement, deltas, archive, spine).

Guarantees (see spec/changes/spec-validator/proposal.md):
  1. Placement — spec/specs/ files are ACCEPTED; no PROPOSED material there
  2. Delta targets resolve — MODIFIED/REMOVED/RENAMED match live requirements;
     ADDED ids are new
  3. Archive completeness — dated folder, proposal shipped, tasks checked, outcome.md
  4. No leaks — unarchived ADDED requirement ids do not appear in spec/specs/
  5. Manifest presence — every active change folder carries valid change.toml
  6. Digest current — recomputed intent digest matches change.toml
  7. Archive transaction completion — archived change.toml has existing
     implementation_commit (legacy archives without a manifest are skipped)
  8. Lens freshness — report-only stale flags when a lens check date predates a
     cited chapter revision

Non-goals: prose style, WHEN/THEN body quality, content judgment.
Exit 0 when there are zero failing findings; exit 1 otherwise.
Lens-freshness notes are warnings and do not fail the run.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
import tomllib
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "spec"
SPECS_DIR = SPEC / "specs"
CHANGES_DIR = SPEC / "changes"
ARCHIVE_DIR = SPEC / "archive"
FLOWS_DIR = SPEC / "flows"

STATUS_ROW_RE = re.compile(
    r"^\|\s*\*\*Status\*\*\s*\|\s*(.+?)\s*\|\s*$",
    re.IGNORECASE | re.MULTILINE,
)
REQ_HEADER_RE = re.compile(
    r"^###\s+([A-Za-z][A-Za-z0-9_.-]*(?:\.[0-9]+)?)\s+[—–-]\s+",
    re.MULTILINE,
)
DELTA_SECTION_RE = re.compile(
    r"^##\s+(ADDED|MODIFIED|REMOVED|RENAMED)\s+Requirements\s*$",
    re.IGNORECASE | re.MULTILINE,
)
RENAME_FROM_TO_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(?:FROM|from)\s*:\s*`?([A-Za-z][A-Za-z0-9_.-]*)`?"
    r".*?(?:TO|to)\s*:\s*`?([A-Za-z][A-Za-z0-9_.-]*)`?",
    re.MULTILINE,
)
TASK_UNCHECKED_RE = re.compile(r"^\s*[-*]\s+\[\s\]\s+", re.MULTILINE)
TASK_CHECKED_RE = re.compile(r"^\s*[-*]\s+\[[xX]\]\s+", re.MULTILINE)
ARCHIVE_NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-.+$")
LENS_CHECKED_RE = re.compile(
    r"(?im)^\|\s*\*\*Checked\*\*\s*\|\s*(\d{4}-\d{2}-\d{2})\s*\|"
    r"|^\s*Checked(?:\s+date)?\s*:\s*(\d{4}-\d{2}-\d{2})\s*$"
)
LENS_CHAPTERS_RE = re.compile(
    r"(?im)^\|\s*\*\*Chapters\*\*\s*\|\s*(.+?)\s*\|"
    r"|^\s*Chapters\s*:\s*(.+)\s*$"
)
CHAPTER_CITE_RE = re.compile(
    r"(?:\*\s*)?chapter:\s*`?([a-z0-9][a-z0-9_-]*)`?"
    r"|chapter\s+`([a-z0-9][a-z0-9_-]+)`",
    re.IGNORECASE,
)

REQUIRED_MANIFEST_FIELDS = (
    "schema",
    "id",
    "class",
    "status",
    "canonical_base_branch",
    "canonical_base_commit",
    "intent_revision",
    "intent_digest",
    "affected_capabilities",
)
VALID_CLASSES = {"behavior", "tooling", "process"}
VALID_STATUSES = {"proposed", "accepted"}


@dataclass
class Report:
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def fail(self, msg: str) -> None:
        self.failures.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)


def rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(path)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def iter_md_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(
        p
        for p in directory.rglob("*")
        if p.is_file()
        and p.suffix == ".md"
        and p.name != ".gitkeep"
        and not any(part.startswith(".") for part in p.relative_to(directory).parts)
    )


def iter_change_folders(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    folders = []
    for child in sorted(directory.iterdir()):
        if not child.is_dir():
            continue
        if child.name.startswith("."):
            continue
        # ignore empty placeholder dirs that only hold .gitkeep
        meaningful = [
            p
            for p in child.rglob("*")
            if p.is_file() and p.name != ".gitkeep"
        ]
        if meaningful:
            folders.append(child)
    return folders


def parse_status(text: str) -> str | None:
    match = STATUS_ROW_RE.search(text)
    if not match:
        return None
    return match.group(1).strip()


def status_is_accepted(status: str | None) -> bool:
    if not status:
        return False
    return bool(re.search(r"\bACCEPTED\b", status, re.IGNORECASE))


def status_is_proposed(status: str | None) -> bool:
    if not status:
        return False
    return bool(re.search(r"\bPROPOSED\b", status, re.IGNORECASE))


def status_is_shipped(status: str | None) -> bool:
    if not status:
        return False
    return bool(re.search(r"\bSHIPPED\b", status, re.IGNORECASE))


def extract_requirement_ids(text: str) -> list[str]:
    return REQ_HEADER_RE.findall(text)


def load_accepted_requirements() -> dict[str, str]:
    """Map requirement id -> chapter path (relative)."""
    mapping: dict[str, str] = {}
    for path in iter_md_files(SPECS_DIR):
        text = read_text(path)
        for req_id in extract_requirement_ids(text):
            mapping[req_id] = rel(path)
    return mapping


def parse_delta_sections(text: str) -> dict[str, list[str]]:
    """Return {ADDED|MODIFIED|REMOVED|RENAMED: [ids...]} from a delta file."""
    sections: dict[str, list[str]] = defaultdict(list)
    matches = list(DELTA_SECTION_RE.finditer(text))
    if not matches:
        return sections

    for i, match in enumerate(matches):
        kind = match.group(1).upper()
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end]
        if kind == "RENAMED":
            pairs = RENAME_FROM_TO_RE.findall(body)
            if pairs:
                for src, dst in pairs:
                    sections[kind].append(f"{src}->{dst}")
            else:
                # fall back to requirement headers in the renamed section
                for req_id in extract_requirement_ids(body):
                    sections[kind].append(req_id)
        else:
            sections[kind].extend(extract_requirement_ids(body))
    return sections


def compute_intent_digest(change_dir: Path) -> str:
    """Recompute intent digest per spec/change-toml.md (intent_rev 1 procedure)."""
    paths: list[str] = []
    for name in ("proposal.md", "tasks.md"):
        if (change_dir / name).is_file():
            paths.append(name)
    specs = change_dir / "specs"
    if specs.is_dir():
        for path in sorted(specs.rglob("*")):
            if path.is_file():
                paths.append(str(path.relative_to(change_dir)).replace("\\", "/"))
    # LC_ALL=C sort ≈ byte-wise ascending on UTF-8 paths
    paths = sorted(set(paths), key=lambda s: s.encode("utf-8"))
    lines: list[str] = []
    for rel_path in paths:
        digest = hashlib.sha256((change_dir / rel_path).read_bytes()).hexdigest()
        lines.append(f"{digest}  {rel_path}")
    blob = ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")
    return "sha256:" + hashlib.sha256(blob).hexdigest()


def git_commit_exists(commit: str) -> bool:
    if not commit or not re.fullmatch(r"[0-9a-fA-F]{7,40}", commit.strip()):
        return False
    try:
        subprocess.check_output(
            ["git", "-C", str(ROOT), "cat-file", "-t", commit.strip()],
            stderr=subprocess.DEVNULL,
            text=True,
        )
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


def git_path_commit_date(path: Path) -> date | None:
    """Best-effort last commit date for a path; None if unavailable."""
    try:
        out = subprocess.check_output(
            [
                "git",
                "-C",
                str(ROOT),
                "log",
                "-1",
                "--format=%cs",
                "--",
                str(path.relative_to(ROOT)),
            ],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except (OSError, subprocess.CalledProcessError, ValueError):
        return None
    if not out:
        return None
    try:
        return date.fromisoformat(out)
    except ValueError:
        return None


def load_manifest(path: Path) -> tuple[dict | None, str | None]:
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError:
        return None, "missing"
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return None, f"invalid TOML: {exc}"
    if not isinstance(data, dict):
        return None, "manifest root must be a table"
    return data, None


def validate_manifest_fields(
    report: Report, manifest_path: Path, data: dict, *, expect_id: str
) -> None:
    where = rel(manifest_path)
    for key in REQUIRED_MANIFEST_FIELDS:
        if key not in data:
            report.fail(f"{where}: missing required field `{key}`")

    schema = data.get("schema")
    if schema is not None and schema != "ata.spec-change/v1":
        report.fail(f"{where}: schema must be \"ata.spec-change/v1\" (got {schema!r})")

    mid = data.get("id")
    if mid is not None and mid != expect_id:
        report.fail(f"{where}: id {mid!r} must match folder name {expect_id!r}")

    mclass = data.get("class")
    if mclass is not None and mclass not in VALID_CLASSES:
        report.fail(
            f"{where}: class must be one of {sorted(VALID_CLASSES)} (got {mclass!r})"
        )

    status = data.get("status")
    if status is not None and str(status).lower() not in VALID_STATUSES:
        report.fail(
            f"{where}: status must be one of {sorted(VALID_STATUSES)} (got {status!r})"
        )

    digest = data.get("intent_digest")
    if digest is not None:
        if not isinstance(digest, str) or not digest.startswith("sha256:"):
            report.fail(f"{where}: intent_digest must be a sha256:… string")
        elif len(digest) != len("sha256:") + 64:
            report.fail(f"{where}: intent_digest has unexpected length")

    rev = data.get("intent_revision")
    if rev is not None and not isinstance(rev, int):
        report.fail(f"{where}: intent_revision must be an integer")

    caps = data.get("affected_capabilities")
    if caps is not None and not isinstance(caps, list):
        report.fail(f"{where}: affected_capabilities must be a list")


def check_placement(report: Report) -> None:
    if not SPECS_DIR.is_dir():
        report.fail(f"missing accepted-truth directory: {rel(SPECS_DIR)}")
        return
    for path in iter_md_files(SPECS_DIR):
        text = read_text(path)
        status = parse_status(text)
        where = rel(path)
        if status is None:
            report.fail(f"{where}: missing Status header row in the chapter table")
            continue
        if status_is_proposed(status):
            report.fail(
                f"{where}: PROPOSED material must not live under spec/specs/ "
                f"(status={status!r})"
            )
        elif not status_is_accepted(status):
            report.fail(
                f"{where}: every file under spec/specs/ must carry status ACCEPTED "
                f"(status={status!r})"
            )


def check_deltas(report: Report, accepted: dict[str, str]) -> dict[str, set[str]]:
    """Validate active change deltas. Returns map change_id -> ADDED ids."""
    added_by_change: dict[str, set[str]] = {}
    for change_dir in iter_change_folders(CHANGES_DIR):
        change_id = change_dir.name
        added_by_change[change_id] = set()
        specs_root = change_dir / "specs"
        if not specs_root.is_dir():
            continue
        for path in iter_md_files(specs_root):
            text = read_text(path)
            sections = parse_delta_sections(text)
            where = rel(path)

            for req_id in sections.get("MODIFIED", []):
                if req_id not in accepted:
                    report.fail(
                        f"{where}: MODIFIED target `{req_id}` does not match a live "
                        f"requirement in spec/specs/"
                    )
            for req_id in sections.get("REMOVED", []):
                if req_id not in accepted:
                    report.fail(
                        f"{where}: REMOVED target `{req_id}` does not match a live "
                        f"requirement in spec/specs/"
                    )
            for entry in sections.get("RENAMED", []):
                if "->" in entry:
                    src, _dst = entry.split("->", 1)
                else:
                    src = entry
                if src not in accepted:
                    report.fail(
                        f"{where}: RENAMED source `{src}` does not match a live "
                        f"requirement in spec/specs/"
                    )
            for req_id in sections.get("ADDED", []):
                added_by_change[change_id].add(req_id)
                if req_id in accepted:
                    report.fail(
                        f"{where}: ADDED requirement `{req_id}` already exists in "
                        f"accepted chapter {accepted[req_id]}"
                    )
    return added_by_change


def check_no_leaks(
    report: Report, added_by_change: dict[str, set[str]], accepted: dict[str, str]
) -> None:
    for change_id, added in added_by_change.items():
        for req_id in sorted(added):
            if req_id in accepted:
                report.fail(
                    f"leak: requirement `{req_id}` introduced by unarchived change "
                    f"`{change_id}` appears in accepted truth {accepted[req_id]} "
                    f"(deltas apply only via steward merge)"
                )


def check_archive_completeness(report: Report) -> None:
    if not ARCHIVE_DIR.is_dir():
        return
    for folder in iter_change_folders(ARCHIVE_DIR):
        where = rel(folder)
        if not ARCHIVE_NAME_RE.match(folder.name):
            report.fail(
                f"{where}: archive folder must be named YYYY-MM-DD-<name>"
            )

        proposal = folder / "proposal.md"
        tasks = folder / "tasks.md"
        outcome = folder / "outcome.md"

        if not proposal.is_file():
            report.fail(f"{where}: missing proposal.md")
        else:
            status = parse_status(read_text(proposal))
            if not status_is_shipped(status):
                report.fail(
                    f"{rel(proposal)}: archived proposal must carry status shipped "
                    f"(status={status!r})"
                )

        if not tasks.is_file():
            report.fail(f"{where}: missing tasks.md")
        else:
            task_text = read_text(tasks)
            unchecked = TASK_UNCHECKED_RE.findall(task_text)
            if unchecked:
                report.fail(
                    f"{rel(tasks)}: archive tasks.md still has {len(unchecked)} "
                    f"unchecked box(es)"
                )
            elif not TASK_CHECKED_RE.search(task_text):
                # empty checklist is suspicious for a shipped archive
                report.fail(
                    f"{rel(tasks)}: archive tasks.md has no checked items"
                )

        if not outcome.is_file():
            report.fail(f"{where}: missing outcome.md")


def check_active_manifests(report: Report) -> None:
    for change_dir in iter_change_folders(CHANGES_DIR):
        manifest_path = change_dir / "change.toml"
        data, err = load_manifest(manifest_path)
        where = rel(change_dir)
        if err == "missing":
            report.fail(f"{where}: missing change.toml (ata.spec-change/v1 manifest)")
            continue
        if err:
            report.fail(f"{rel(manifest_path)}: {err}")
            continue
        assert data is not None
        validate_manifest_fields(
            report, manifest_path, data, expect_id=change_dir.name
        )

        # Digest current (guarantee 6) for every present manifest.
        recorded = data.get("intent_digest")
        if isinstance(recorded, str) and recorded.startswith("sha256:"):
            actual = compute_intent_digest(change_dir)
            if actual != recorded:
                report.fail(
                    f"{rel(manifest_path)}: intent_digest is stale — "
                    f"recorded {recorded}, recomputed {actual}"
                )


def check_archive_transactions(report: Report) -> None:
    for folder in iter_change_folders(ARCHIVE_DIR):
        manifest_path = folder / "change.toml"
        if not manifest_path.is_file():
            continue
        data, err = load_manifest(manifest_path)
        if err:
            report.fail(f"{rel(manifest_path)}: {err}")
            continue
        assert data is not None
        m = re.match(r"^\d{4}-\d{2}-\d{2}-(.+)$", folder.name)
        expect_id = m.group(1) if m else folder.name
        # Field checks without wrong expect_id first
        for key in REQUIRED_MANIFEST_FIELDS:
            if key not in data:
                report.fail(f"{rel(manifest_path)}: missing required field `{key}`")
        schema = data.get("schema")
        if schema is not None and schema != "ata.spec-change/v1":
            report.fail(
                f"{rel(manifest_path)}: schema must be \"ata.spec-change/v1\" "
                f"(got {schema!r})"
            )
        mid = data.get("id")
        if mid is not None and mid != expect_id:
            report.fail(
                f"{rel(manifest_path)}: id {mid!r} must match archive change id "
                f"{expect_id!r}"
            )
        mclass = data.get("class")
        if mclass is not None and mclass not in VALID_CLASSES:
            report.fail(
                f"{rel(manifest_path)}: class must be one of {sorted(VALID_CLASSES)} "
                f"(got {mclass!r})"
            )
        commit = data.get("implementation_commit")
        if not commit:
            report.fail(
                f"{rel(manifest_path)}: archived change.toml missing "
                f"implementation_commit"
            )
        elif not git_commit_exists(str(commit)):
            report.fail(
                f"{rel(manifest_path)}: implementation_commit {commit!r} does not "
                f"exist in this repository"
            )


def parse_lens_meta(text: str) -> tuple[date | None, list[str]]:
    checked: date | None = None
    m = LENS_CHECKED_RE.search(text)
    if m:
        raw = m.group(1) or m.group(2)
        try:
            checked = date.fromisoformat(raw)
        except ValueError:
            checked = None

    chapters: list[str] = []
    cm = LENS_CHAPTERS_RE.search(text)
    if cm:
        raw = cm.group(1) or cm.group(2) or ""
        chapters = [
            part.strip().strip("`").removesuffix(".md")
            for part in re.split(r"[,;]", raw)
            if part.strip()
        ]
    if not chapters:
        found = []
        for match in CHAPTER_CITE_RE.finditer(text):
            name = match.group(1) or match.group(2)
            if name:
                found.append(name.strip().removesuffix(".md"))
        # de-dupe preserving order
        seen: set[str] = set()
        for name in found:
            key = name.lower()
            if key not in seen:
                seen.add(key)
                chapters.append(name)
    return checked, chapters


def resolve_chapter_path(name: str) -> Path | None:
    candidates = [
        SPECS_DIR / f"{name}.md",
        SPECS_DIR / name / "spec.md",
        SPECS_DIR / name,
    ]
    # also allow underscore/hyphen swap
    alt = name.replace("_", "-")
    if alt != name:
        candidates.append(SPECS_DIR / f"{alt}.md")
    alt2 = name.replace("-", "_")
    if alt2 != name:
        candidates.append(SPECS_DIR / f"{alt2}.md")
    for path in candidates:
        if path.is_file():
            return path
    # fuzzy: unique prefix match on chapter stems
    if SPECS_DIR.is_dir():
        stems = {p.stem: p for p in SPECS_DIR.glob("*.md")}
        key = name.lower()
        hits = [p for stem, p in stems.items() if key in stem.lower() or stem.lower() in key]
        if len(hits) == 1:
            return hits[0]
    return None


def check_lens_freshness(report: Report) -> None:
    if not FLOWS_DIR.is_dir():
        return
    for path in iter_md_files(FLOWS_DIR):
        text = read_text(path)
        checked, chapters = parse_lens_meta(text)
        where = rel(path)
        if not chapters:
            report.warn(
                f"{where}: lens freshness skipped — no cited chapters recorded or detected"
            )
            continue
        if checked is None:
            report.warn(
                f"{where}: lens freshness skipped — no Checked date recorded "
                f"(cites: {', '.join(chapters)})"
            )
            continue
        for chapter in chapters:
            chapter_path = resolve_chapter_path(chapter)
            if chapter_path is None:
                report.warn(
                    f"{where}: lens cites unknown chapter `{chapter}` "
                    f"(freshness not evaluated)"
                )
                continue
            chapter_date = git_path_commit_date(chapter_path)
            if chapter_date is None:
                # fall back to mtime
                chapter_date = datetime.fromtimestamp(
                    chapter_path.stat().st_mtime
                ).date()
            if chapter_date > checked:
                report.warn(
                    f"{where}: stale — cites `{chapter}` last revised {chapter_date.isoformat()} "
                    f"after lens checked {checked.isoformat()}"
                )


def run(report: Report | None = None) -> Report:
    report = report or Report()

    if not SPEC.is_dir():
        report.fail(f"missing spec directory: {rel(SPEC)}")
        return report

    check_placement(report)
    accepted = load_accepted_requirements()
    added_by_change = check_deltas(report, accepted)
    check_no_leaks(report, added_by_change, accepted)
    check_archive_completeness(report)
    check_active_manifests(report)
    check_archive_transactions(report)
    check_lens_freshness(report)
    return report


def main(argv: list[str] | None = None) -> int:
    _ = argv
    report = run()
    for warning in report.warnings:
        print(f"speech-core-spec: warning: {warning}", file=sys.stderr)
    if report.failures:
        for failure in report.failures:
            print(f"speech-core-spec: {failure}", file=sys.stderr)
        print(
            f"speech-core-spec: failed with {len(report.failures)} finding(s)"
            + (
                f", {len(report.warnings)} warning(s)"
                if report.warnings
                else ""
            ),
            file=sys.stderr,
        )
        return 1

    warn_note = (
        f" — {len(report.warnings)} freshness warning(s)"
        if report.warnings
        else ""
    )
    print(
        "speech-core-spec: ok — guarantees 1-8 checked"
        f"{warn_note}; zero failing findings"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
