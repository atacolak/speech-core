#!/usr/bin/env python3
"""Focused tests for scripts/check-spec-contract.py (guarantees 1-8)."""

from __future__ import annotations

import hashlib
import importlib.util
import shutil
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "check-spec-contract.py"


def load_module():
    spec = importlib.util.spec_from_file_location("check_spec_contract", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


MOD = load_module()


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).strip() + "\n", encoding="utf-8")


def chapter(status: str, reqs: list[str]) -> str:
    req_blocks = "\n".join(
        f"### {rid} — title\nWHEN x THEN y.\n" for rid in reqs
    )
    # Keep every line indented so textwrap.dedent can strip a shared margin.
    indented_reqs = textwrap.indent(req_blocks, "    ")
    return f"""
    | | |
    |---|---|
    | **Status** | {status} |

    ## 2. What does it do?

{indented_reqs}
    """


def make_digest(change_dir: Path) -> str:
    return MOD.compute_intent_digest(change_dir)


def write_manifest(
    change_dir: Path,
    *,
    change_id: str = "demo",
    digest: str | None = None,
    extra: str = "",
) -> None:
    if digest is None:
        # ensure proposal/tasks exist first
        digest = make_digest(change_dir)
    write(
        change_dir / "change.toml",
        f"""
        schema = "ata.spec-change/v1"
        id = "{change_id}"
        class = "tooling"
        status = "accepted"
        canonical_base_branch = "feature/assistant-self-asr"
        canonical_base_commit = "baa2785950a1a98ec33504c2175f46d148782645"
        intent_revision = 1
        intent_digest = "{digest}"
        affected_capabilities = []
        {extra}
        """,
    )


class SpecContractFixtureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="spec-contract-"))
        self.root = self.tmp / "repo"
        self.spec = self.root / "spec"
        (self.spec / "specs").mkdir(parents=True)
        (self.spec / "changes").mkdir(parents=True)
        (self.spec / "archive").mkdir(parents=True)
        (self.spec / "flows").mkdir(parents=True)
        # point module paths at the fixture tree
        self._orig = {
            "ROOT": MOD.ROOT,
            "SPEC": MOD.SPEC,
            "SPECS_DIR": MOD.SPECS_DIR,
            "CHANGES_DIR": MOD.CHANGES_DIR,
            "ARCHIVE_DIR": MOD.ARCHIVE_DIR,
            "FLOWS_DIR": MOD.FLOWS_DIR,
        }
        MOD.ROOT = self.root
        MOD.SPEC = self.spec
        MOD.SPECS_DIR = self.spec / "specs"
        MOD.CHANGES_DIR = self.spec / "changes"
        MOD.ARCHIVE_DIR = self.spec / "archive"
        MOD.FLOWS_DIR = self.spec / "flows"

    def tearDown(self) -> None:
        for key, value in self._orig.items():
            setattr(MOD, key, value)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_check(self) -> MOD.Report:
        return MOD.run()

    def test_clean_tree_passes(self) -> None:
        write(
            self.spec / "specs" / "alpha.md",
            chapter("ACCEPTED — 2026-08-04", ["alpha.1", "alpha.2"]),
        )
        change = self.spec / "changes" / "demo"
        write(
            change / "proposal.md",
            """
            | | |
            |---|---|
            | **Status** | ACCEPTED |
            """,
        )
        write(change / "tasks.md", "- [ ] do the thing\n")
        write(
            change / "specs" / "alpha" / "spec.md",
            """
            ## ADDED Requirements

            ### alpha.3 — new thing
            WHEN a THEN b.
            """,
        )
        write_manifest(change, change_id="demo")
        # archive complete, no manifest (legacy)
        arch = self.spec / "archive" / "2026-08-04-legacy"
        write(
            arch / "proposal.md",
            """
            | | |
            |---|---|
            | **Status** | SHIPPED 2026-08-04 |
            """,
        )
        write(arch / "tasks.md", "- [x] done\n")
        write(arch / "outcome.md", "shipped.\n")

        report = self.run_check()
        self.assertEqual(report.failures, [], report.failures)

    def test_proposed_in_specs_fails_placement(self) -> None:
        write(
            self.spec / "specs" / "alpha.md",
            chapter("PROPOSED — draft", ["alpha.1"]),
        )
        report = self.run_check()
        self.assertTrue(any("PROPOSED" in f for f in report.failures), report.failures)

    def test_modified_unknown_target_fails(self) -> None:
        write(
            self.spec / "specs" / "alpha.md",
            chapter("ACCEPTED — 2026-08-04", ["alpha.1"]),
        )
        change = self.spec / "changes" / "demo"
        write(change / "proposal.md", "| **Status** | ACCEPTED |\n")
        write(change / "tasks.md", "- [ ] x\n")
        write(
            change / "specs" / "alpha" / "spec.md",
            """
            ## MODIFIED Requirements

            ### missing.9 — not real
            body
            """,
        )
        write_manifest(change, change_id="demo")
        report = self.run_check()
        self.assertTrue(
            any("MODIFIED target `missing.9`" in f for f in report.failures),
            report.failures,
        )

    def test_modified_cross_chapter_target_fails(self) -> None:
        """G2: MODIFIED must hit the delta path's target chapter, not any global id."""
        write(
            self.spec / "specs" / "alpha.md",
            chapter("ACCEPTED — 2026-08-04", ["alpha.1"]),
        )
        write(
            self.spec / "specs" / "beta.md",
            chapter("ACCEPTED — 2026-08-04", ["beta.1"]),
        )
        change = self.spec / "changes" / "demo"
        write(change / "proposal.md", "| **Status** | ACCEPTED |\n")
        write(change / "tasks.md", "- [ ] x\n")
        # Delta path targets alpha, but MODIFIED cites beta.1 which lives only in beta.
        write(
            change / "specs" / "alpha" / "spec.md",
            """
            ## MODIFIED Requirements

            ### beta.1 — wrong chapter
            body
            """,
        )
        write_manifest(change, change_id="demo")
        report = self.run_check()
        self.assertTrue(
            any(
                "MODIFIED target `beta.1`" in f
                and "not in target accepted chapter" in f
                for f in report.failures
            ),
            report.failures,
        )

    def test_added_existing_id_fails(self) -> None:
        write(
            self.spec / "specs" / "alpha.md",
            chapter("ACCEPTED — 2026-08-04", ["alpha.1"]),
        )
        change = self.spec / "changes" / "demo"
        write(change / "proposal.md", "| **Status** | ACCEPTED |\n")
        write(change / "tasks.md", "- [ ] x\n")
        write(
            change / "specs" / "alpha" / "spec.md",
            """
            ## ADDED Requirements

            ### alpha.1 — already live
            body
            """,
        )
        write_manifest(change, change_id="demo")
        report = self.run_check()
        self.assertTrue(
            any("ADDED requirement `alpha.1`" in f for f in report.failures),
            report.failures,
        )

    def test_archive_missing_outcome_fails(self) -> None:
        write(
            self.spec / "specs" / "alpha.md",
            chapter("ACCEPTED — 2026-08-04", ["alpha.1"]),
        )
        arch = self.spec / "archive" / "2026-08-04-broken"
        write(
            arch / "proposal.md",
            """
            | | |
            |---|---|
            | **Status** | SHIPPED |
            """,
        )
        write(arch / "tasks.md", "- [x] done\n")
        report = self.run_check()
        self.assertTrue(
            any("missing outcome.md" in f for f in report.failures),
            report.failures,
        )

    def test_archive_unchecked_tasks_fails(self) -> None:
        write(
            self.spec / "specs" / "alpha.md",
            chapter("ACCEPTED — 2026-08-04", ["alpha.1"]),
        )
        arch = self.spec / "archive" / "2026-08-04-open"
        write(
            arch / "proposal.md",
            """
            | | |
            |---|---|
            | **Status** | SHIPPED |
            """,
        )
        write(arch / "tasks.md", "- [ ] still open\n- [x] done\n")
        write(arch / "outcome.md", "nope\n")
        report = self.run_check()
        self.assertTrue(
            any("unchecked box" in f for f in report.failures),
            report.failures,
        )

    def test_stale_digest_fails(self) -> None:
        write(
            self.spec / "specs" / "alpha.md",
            chapter("ACCEPTED — 2026-08-04", ["alpha.1"]),
        )
        change = self.spec / "changes" / "demo"
        write(change / "proposal.md", "| **Status** | ACCEPTED |\n")
        write(change / "tasks.md", "- [ ] x\n")
        write_manifest(
            change,
            change_id="demo",
            digest="sha256:" + ("0" * 64),
        )
        report = self.run_check()
        self.assertTrue(
            any("intent_digest is stale" in f for f in report.failures),
            report.failures,
        )

    def test_missing_manifest_fails(self) -> None:
        write(
            self.spec / "specs" / "alpha.md",
            chapter("ACCEPTED — 2026-08-04", ["alpha.1"]),
        )
        change = self.spec / "changes" / "demo"
        write(change / "proposal.md", "| **Status** | ACCEPTED |\n")
        write(change / "tasks.md", "- [ ] x\n")
        report = self.run_check()
        self.assertTrue(
            any("missing change.toml" in f for f in report.failures),
            report.failures,
        )

    def test_digest_matches_shell_procedure(self) -> None:
        change = self.spec / "changes" / "demo"
        write(change / "proposal.md", "hello\n")
        write(change / "tasks.md", "world\n")
        write(change / "specs" / "a.md", "delta\n")
        # independent recompute
        paths = ["proposal.md", "specs/a.md", "tasks.md"]
        lines = []
        for rel_path in paths:
            digest = hashlib.sha256((change / rel_path).read_bytes()).hexdigest()
            lines.append(f"{digest}  {rel_path}")
        blob = ("\n".join(lines) + "\n").encode()
        expected = "sha256:" + hashlib.sha256(blob).hexdigest()
        self.assertEqual(MOD.compute_intent_digest(change), expected)

    def test_lens_stale_is_warning_not_failure(self) -> None:
        write(
            self.spec / "specs" / "alpha.md",
            chapter("ACCEPTED — 2026-08-04", ["alpha.1"]),
        )
        # chapter mtime "now"; lens checked in the past
        write(
            self.spec / "flows" / "walk.md",
            """
            | | |
            |---|---|
            | **Checked** | 2000-01-01 |
            | **Chapters** | alpha |

            chapter: alpha
            """,
        )
        report = self.run_check()
        self.assertEqual(report.failures, [], report.failures)
        self.assertTrue(
            any("stale" in w for w in report.warnings),
            report.warnings,
        )

    def test_archive_missing_implementation_commit_fails(self) -> None:
        """G7: archived change.toml without implementation_commit fails."""
        write(
            self.spec / "specs" / "alpha.md",
            chapter("ACCEPTED — 2026-08-04", ["alpha.1"]),
        )
        arch = self.spec / "archive" / "2026-08-04-demo"
        write(
            arch / "proposal.md",
            """
            | | |
            |---|---|
            | **Status** | SHIPPED |
            """,
        )
        write(arch / "tasks.md", "- [x] done\n")
        write(arch / "outcome.md", "shipped.\n")
        write(
            arch / "change.toml",
            """
            schema = "ata.spec-change/v1"
            id = "demo"
            class = "tooling"
            status = "accepted"
            canonical_base_branch = "feature/assistant-self-asr"
            canonical_base_commit = "baa2785950a1a98ec33504c2175f46d148782645"
            intent_revision = 1
            intent_digest = "sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
            affected_capabilities = []
            """,
        )
        report = self.run_check()
        self.assertTrue(
            any(
                "missing implementation_commit" in f for f in report.failures
            ),
            report.failures,
        )

    def test_archive_nonexistent_implementation_commit_fails(self) -> None:
        """G7: archived change.toml with unknown implementation_commit fails."""
        write(
            self.spec / "specs" / "alpha.md",
            chapter("ACCEPTED — 2026-08-04", ["alpha.1"]),
        )
        arch = self.spec / "archive" / "2026-08-04-demo"
        write(
            arch / "proposal.md",
            """
            | | |
            |---|---|
            | **Status** | SHIPPED |
            """,
        )
        write(arch / "tasks.md", "- [x] done\n")
        write(arch / "outcome.md", "shipped.\n")
        write(
            arch / "change.toml",
            """
            schema = "ata.spec-change/v1"
            id = "demo"
            class = "tooling"
            status = "accepted"
            canonical_base_branch = "feature/assistant-self-asr"
            canonical_base_commit = "baa2785950a1a98ec33504c2175f46d148782645"
            intent_revision = 1
            intent_digest = "sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
            affected_capabilities = []
            implementation_commit = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
            """,
        )
        report = self.run_check()
        self.assertTrue(
            any(
                "implementation_commit" in f and "does not exist" in f
                for f in report.failures
            ),
            report.failures,
        )


if __name__ == "__main__":
    unittest.main()
