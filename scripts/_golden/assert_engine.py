"""Semantic assertion DSL v1 evaluation engine.

Implements spec §8: require/forbid/count/exactly-one-close, partial-order,
predicates, sample/numeric windows, transcript normalization, invariants,
unstable field blacklist enforcement.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from .constants import (
    ALLOWED_CLOSE_SOURCES,
    EXIT_ASSERTION_FAILED,
    EXIT_PASS,
    FORBIDDEN_NORMAL_CLOSE_SOURCES,
    UNSTABLE_FIELD_PATTERNS,
    UNSTABLE_WHOLE_FIELD_NAMES,
    Event,
    EventStream,
)


# ── assertion result types ───────────────────────────────────────────────────

class AssertionViolation:
    """A single violated assertion."""

    def __init__(self, rule: str, detail: str, evidence: Optional[List[Dict[str, Any]]] = None):
        self.rule = rule
        self.detail = detail
        self.evidence = evidence or []

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rule": self.rule,
            "detail": self.detail,
            "evidence": self.evidence,
        }


class AssertionResult:
    """Overall assertion evaluation result."""

    def __init__(self):
        self.passed: List[str] = []
        self.violations: List[AssertionViolation] = []
        self.warnings: List[str] = []

    @property
    def all_passed(self) -> bool:
        return len(self.violations) == 0

    def add_pass(self, name: str) -> None:
        self.passed.append(name)

    def add_violation(self, rule: str, detail: str, evidence: Optional[List[Dict]] = None) -> None:
        self.violations.append(AssertionViolation(rule, detail, evidence))

    def add_warning(self, msg: str) -> None:
        self.warnings.append(msg)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "violations": [v.to_dict() for v in self.violations],
            "warnings": self.warnings,
            "all_passed": self.all_passed,
        }


# ── selector matching ────────────────────────────────────────────────────────

def matches_selector(event: Event, selector: Dict[str, Any]) -> bool:
    """Check if event matches a named or inline selector."""
    event_type = selector.get("event")
    if event_type:
        observed = event.get("event") or event.get("type")
        if observed != event_type:
            return False
    where = selector.get("where", {})
    for key, expected_val in where.items():
        actual_val = _resolve_path(event, key)
        if actual_val != expected_val:
            return False
    return True


def _resolve_path(obj: Any, path: str) -> Any:
    """Resolve dotted path in nested dict."""
    parts = path.split(".")
    for part in parts:
        if isinstance(obj, dict):
            obj = obj.get(part)
        else:
            return None
    return obj


# ── event binding ────────────────────────────────────────────────────────────

def bind_events(
    events: EventStream,
    selector: Dict[str, Any],
    bind_name: Optional[str] = None,
) -> List[Event]:
    """Find all events matching a selector, optionally binding them."""
    matched = [e for e in events if matches_selector(e, selector)]
    return matched


# ── main DSL evaluator ───────────────────────────────────────────────────────

class AssertionEvaluator:
    """Evaluate assertion DSL v1 against an event stream."""

    def __init__(self, dsl: Dict[str, Any], events: EventStream,
                 scenario_dir: Optional[Path] = None):
        self.dsl = dsl
        self.events = events
        self.scenario_dir = scenario_dir
        self.result = AssertionResult()
        self._selectors: Dict[str, Dict[str, Any]] = {}
        self._bindings: Dict[str, List[Event]] = {}
        self._tolerances = dsl.get("tolerances", {}).get("synthetic", {})

        # Resolve selectors
        selectors = dsl.get("selectors", {})
        if isinstance(selectors, dict):
            self._selectors = selectors

    def evaluate(self) -> AssertionResult:
        """Run all assertions and return result."""
        self._check_unstable_fields_in_dsl()
        self._eval_require()
        self._eval_forbid()
        self._eval_count()
        self._eval_order()
        self._eval_partial_order()
        self._eval_numeric()
        self._eval_transcript()
        self._eval_invariants()
        self._eval_ownership()
        self._eval_sample_windows()
        self._eval_gaps()
        self._eval_late_revisions()
        return self.result

    # ── unstable field guard ─────────────────────────────────────────────

    def _check_unstable_fields_in_dsl(self) -> None:
        """Warn or error if DSL mandates equality on unstable fields."""
        def _check_where(where: Dict[str, Any], context: str):
            for key in where:
                if key in UNSTABLE_WHOLE_FIELD_NAMES or any(
                    p in key for p in UNSTABLE_FIELD_PATTERNS
                ):
                    self.result.add_warning(
                        f"{context}: field '{key}' is unstable and equality assertion "
                        f"may be platform-dependent"
                    )

        for rule_name, selector in self._selectors.items():
            if isinstance(selector, dict) and "where" in selector:
                _check_where(selector["where"], f"selector '{rule_name}'")

        for req in self.dsl.get("require", []):
            if isinstance(req, dict) and "where" in req:
                _check_where(req["where"], "require")
        for fb in self.dsl.get("forbid", []):
            if isinstance(fb, dict) and "where" in fb:
                _check_where(fb["where"], "forbid")

    # ── require ──────────────────────────────────────────────────────────

    def _eval_require(self) -> None:
        for i, req in enumerate(self.dsl.get("require", [])):
            if not isinstance(req, dict):
                continue
            event_type = req.get("event")
            where = req.get("where", {})
            count = req.get("count")

            matched = [e for e in self.events if matches_selector(e, req)]

            rule_name = f"require[{i}] {event_type}"
            if count is not None:
                if len(matched) != count:
                    self.result.add_violation(
                        rule_name,
                        f"Expected {count} events, found {len(matched)}",
                        matched[:5],
                    )
                else:
                    self.result.add_pass(rule_name)
            else:
                if len(matched) == 0:
                    self.result.add_violation(
                        rule_name,
                        f"Required event not found",
                    )
                else:
                    self.result.add_pass(rule_name)

    # ── forbid ───────────────────────────────────────────────────────────

    def _eval_forbid(self) -> None:
        for i, fb in enumerate(self.dsl.get("forbid", [])):
            if not isinstance(fb, dict):
                continue
            event_type = fb.get("event")
            where = fb.get("where", {})
            matched = [e for e in self.events if matches_selector(e, fb)]
            rule_name = f"forbid[{i}] {event_type}"
            if matched:
                self.result.add_violation(
                    rule_name,
                    f"Forbidden event found: {len(matched)} occurrences",
                    matched[:5],
                )
            else:
                self.result.add_pass(rule_name)

    # ── count ────────────────────────────────────────────────────────────

    def _eval_count(self) -> None:
        clauses = self.dsl.get("count", [])
        if isinstance(clauses, dict):
            clauses = [clauses]
        for i, clause in enumerate(clauses):
            if not isinstance(clause, dict):
                continue
            event_type = clause.get("event")
            where = clause.get("where", {})
            expected = clause.get("count")
            if expected is None:
                continue
            matched = [e for e in self.events if matches_selector(e, clause)]
            rule_name = f"count[{i}] {event_type}"
            if len(matched) != expected:
                self.result.add_violation(
                    rule_name,
                    f"Expected count {expected}, got {len(matched)}",
                    matched[:5],
                )
            else:
                self.result.add_pass(rule_name)

    # ── order ────────────────────────────────────────────────────────────

    def _eval_order(self) -> None:
        """Verify total ordering constraints (causally related events)."""
        order_rules = self.dsl.get("order", [])
        if not order_rules:
            return

        # Index events by type
        events_by_type: Dict[str, List[int]] = {}
        for i, e in enumerate(self.events):
            t = e.get("event") or e.get("type")
            if t:
                events_by_type.setdefault(t, []).append(i)

        for oi, chain in enumerate(order_rules):
            if not isinstance(chain, list) or len(chain) < 2:
                continue
            # Verify each successive pair
            for j in range(len(chain) - 1):
                a_type = chain[j]
                b_type = chain[j + 1]
                a_indices = events_by_type.get(a_type, [])
                b_indices = events_by_type.get(b_type, [])

                # Every occurrence of 'a' should precede at least one 'b' after it,
                # and the last 'a' should precede the first 'b' (causal chain).
                if a_indices and b_indices:
                    if max(a_indices) >= min(b_indices):
                        self.result.add_violation(
                            f"order[{oi}]: {a_type} -> {b_type}",
                            f"Order violation: last {a_type} at index {max(a_indices)} "
                            f"is not before first {b_type} at index {min(b_indices)}",
                        )
                    else:
                        self.result.add_pass(f"order[{oi}]: {a_type} -> {b_type}")
                elif a_indices and not b_indices:
                    self.result.add_violation(
                        f"order[{oi}]: {a_type} -> {b_type}",
                        f"{b_type} not found (preceded by {a_type})",
                    )
                # If 'a' not found but 'b' found, order is trivially fine
                # If neither found, skip

    # ── partial_order ────────────────────────────────────────────────────

    def _eval_partial_order(self) -> None:
        """Verify partial-order constraints with optional same-session/turn matching."""
        rules = self.dsl.get("partial_order", [])
        for pi, rule in enumerate(rules):
            if not isinstance(rule, dict):
                continue
            before_sel = rule.get("before", {})
            after_sel = rule.get("after", {})
            match_scope = rule.get("match", "same_session")

            before_events = [e for e in self.events if matches_selector(e, before_sel)]
            after_events = [e for e in self.events if matches_selector(e, after_sel)]

            rule_name = f"partial_order[{pi}]"
            if not before_events:
                self.result.add_violation(
                    rule_name, f"No 'before' events matching {before_sel}"
                )
                continue
            if not after_events:
                self.result.add_violation(
                    rule_name, f"No 'after' events matching {after_sel}"
                )
                continue

            violated = False
            for bef in before_events:
                bef_idx = self.events.index(bef)
                for aft in after_events:
                    aft_idx = self.events.index(aft)
                    if bef_idx >= aft_idx:
                        # Check if they're in the same scope
                        if match_scope == "same_turn":
                            bef_turn = bef.get("turn_id")
                            aft_turn = aft.get("turn_id")
                            if bef_turn == aft_turn and bef_turn is not None:
                                violated = True
                        elif match_scope == "same_session":
                            violated = True
            if violated:
                self.result.add_violation(
                    rule_name, "Partial order violation: before event(s) appear after after event(s)"
                )
            else:
                self.result.add_pass(rule_name)

    # ── numeric ──────────────────────────────────────────────────────────

    def _eval_numeric(self) -> None:
        for ni, num in enumerate(self.dsl.get("numeric", [])):
            if not isinstance(num, dict):
                continue
            event_type = num.get("event")
            field = num.get("field")
            expected = num.get("expected_samples")
            tolerance = num.get("tolerance_samples")
            relation = num.get("relation", "==")
            value_from = num.get("value_from")

            matched = [e for e in self.events if matches_selector(e, {"event": event_type})]
            rule_name = f"numeric[{ni}] {event_type}.{field}"

            if not matched:
                self.result.add_violation(rule_name, f"No {event_type} events")
                continue

            for event in matched:
                actual = _resolve_path(event, field)
                if actual is None:
                    self.result.add_violation(
                        rule_name, f"Field '{field}' not present in event", [event]
                    )
                    continue

                if value_from:
                    # Dynamic value from another event
                    try:
                        # Parse value_from like "event(vad_speech_end).decision_sample"
                        ref = _resolve_value_from(value_from, self.events)
                        if ref is None:
                            self.result.add_violation(
                                rule_name, f"Could not resolve value_from: {value_from}"
                            )
                            continue
                    except Exception:
                        self.result.add_violation(
                            rule_name, f"Error resolving value_from: {value_from}"
                        )
                        continue

                if relation == "==" and tolerance is not None:
                    if abs(actual - expected) > tolerance:
                        self.result.add_violation(
                            rule_name,
                            f"Expected {expected} ± {tolerance}, got {actual} "
                            f"(diff={abs(actual - expected)})",
                            [event],
                        )
                    else:
                        self.result.add_pass(rule_name)
                elif relation == ">=":
                    if actual < (expected if not value_from else ref):
                        self.result.add_violation(
                            rule_name,
                            f"Expected >= {expected if not value_from else ref}, got {actual}",
                            [event],
                        )
                    else:
                        self.result.add_pass(rule_name)
                elif relation == "<=":
                    if actual > (expected if not value_from else ref):
                        self.result.add_violation(
                            rule_name,
                            f"Expected <= {expected if not value_from else ref}, got {actual}",
                            [event],
                        )
                    else:
                        self.result.add_pass(rule_name)

    # ── transcript ───────────────────────────────────────────────────────

    def _eval_transcript(self) -> None:
        tx = self.dsl.get("transcript")
        if not tx:
            return

        normalize_mode = tx.get("normalize", "lowercase_strip_punctuation_whitespace")
        require_any = tx.get("require_any", [])

        # Collect all transcript text from token events
        all_texts = []
        for e in self.events:
            evt_type = e.get("event") or e.get("type")
            if evt_type in ("transcript_token_committed", "transcript_update"):
                text = e.get("text", "")
                if text:
                    all_texts.append(text)

        combined = " ".join(all_texts)
        normalized = _normalize_text(combined, normalize_mode)

        for phrase in require_any:
            norm_phrase = _normalize_text(phrase, normalize_mode)
            if norm_phrase in normalized:
                self.result.add_pass(f"transcript: contains '{phrase}'")
            else:
                self.result.add_violation(
                    f"transcript: requires '{phrase}'",
                    f"Not found in normalized transcript: '{normalized[:200]}'",
                )

    # ── invariants ───────────────────────────────────────────────────────

    def _eval_invariants(self) -> None:
        invariants = self.dsl.get("invariants", [])
        for inv in invariants:
            if inv == "balanced_turns":
                self._check_balanced_turns()
            elif inv == "monotonic_audio_seq":
                self._check_monotonic_audio_seq()
            elif inv == "contiguous_source_samples_except_declared_gaps":
                self._check_contiguous_samples()

    def _check_balanced_turns(self) -> None:
        """Every turn_started has exactly one turn_closed."""
        started = [e for e in self.events if (e.get("event") or e.get("type")) == "turn_started"]
        closed = [e for e in self.events if (e.get("event") or e.get("type")) == "turn_closed"]

        # Build turn_ids
        started_ids = set()
        for e in started:
            tid = e.get("turn_id") or e.get("turn_index")
            if tid is not None:
                started_ids.add(tid)

        closed_ids = set()
        for e in closed:
            tid = e.get("turn_id") or e.get("turn_index")
            if tid is not None:
                closed_ids.add(tid)

        unbalanced_started = started_ids - closed_ids
        if unbalanced_started:
            self.result.add_violation(
                "invariant:balanced_turns",
                f"Turns started but not closed: {unbalanced_started}",
            )
        else:
            self.result.add_pass("invariant:balanced_turns")

    def _check_monotonic_audio_seq(self) -> None:
        """Audio frame sequences are monotonically increasing."""
        seqs = []
        for e in self.events:
            seq = e.get("seq")
            if seq is not None:
                seqs.append((self.events.index(e), seq))
        for i in range(1, len(seqs)):
            if seqs[i][1] <= seqs[i-1][1]:
                self.result.add_violation(
                    "invariant:monotonic_audio_seq",
                    f"Non-monotonic sequence at indices {seqs[i-1][0]} ({seqs[i-1][1]}) "
                    f"-> {seqs[i][0]} ({seqs[i][1]})",
                )
                return
        if seqs:
            self.result.add_pass("invariant:monotonic_audio_seq")

    def _check_contiguous_samples(self) -> None:
        """Sample ranges are contiguous (no undeclared gaps)."""
        frames = []
        for e in self.events:
            ss = e.get("sample_start") or e.get("source_sample_start")
            sc = e.get("sample_count")
            if ss is not None and sc is not None:
                frames.append((self.events.index(e), int(ss), int(sc)))
        if not frames:
            return
        frames.sort(key=lambda x: x[1])
        for i in range(1, len(frames)):
            prev_end = frames[i-1][1] + frames[i-1][2]
            curr_start = frames[i][1]
            if curr_start != prev_end:
                # Check if this gap is declared
                gap_declared = any(
                    "preceding_source_gap" in str(self.events[frames[i][0]])
                    for _ in [1]  # run once
                )
                # Actually check properly
                evt = self.events[frames[i][0]]
                has_declared_gap = (
                    evt.get("preceding_source_gap") is not None
                    or evt.get("sequence_gap") is not None
                    or evt.get("sample_gap") is not None
                )
                if not has_declared_gap:
                    self.result.add_violation(
                        "invariant:contiguous_source_samples",
                        f"Undeclared gap: expected sample {prev_end}, got {curr_start} "
                        f"(delta={curr_start - prev_end})",
                    )
                    return
        self.result.add_pass("invariant:contiguous_source_samples_except_declared_gaps")

    # ── ownership ────────────────────────────────────────────────────────

    def _eval_ownership(self) -> None:
        ownership = self.dsl.get("ownership")
        if not ownership:
            return

        key = ownership.get("key", "turn_id")
        require_exactly_one = ownership.get("require_exactly_one_close_per_started_turn", False)
        forbid_late = ownership.get("forbid_late_mutation_after_close", False)

        if require_exactly_one:
            self._check_exactly_one_close(key)
        if forbid_late:
            self._check_no_late_mutation(key)

    def _check_exactly_one_close(self, key: str) -> None:
        """Each turned_started turn has exactly one turn_closed."""
        # Group events by turn key
        turns: Dict[Any, Dict[str, List]] = {}
        for e in self.events:
            evt_type = e.get("event") or e.get("type")
            if evt_type not in ("turn_started", "turn_closed"):
                continue
            k = e.get(key) or e.get("turn_index")
            if k is None:
                continue
            if k not in turns:
                turns[k] = {"started": [], "closed": []}
            if "started" in evt_type:
                turns[k]["started"].append(e)
            elif "closed" in evt_type:
                turns[k]["closed"].append(e)

        for turn_key, groups in turns.items():
            n_started = len(groups["started"])
            n_closed = len(groups["closed"])
            if n_started > 0 and n_closed != 1:
                self.result.add_violation(
                    f"ownership:exactly_one_close turn_key={turn_key}",
                    f"Turn started {n_started} times, closed {n_closed} times",
                )
        if not self.result.violations:
            self.result.add_pass("ownership:require_exactly_one_close_per_started_turn")

    def _check_no_late_mutation(self, key: str) -> None:
        """After turn_closed, no event mutates that turn's boundary."""
        closed_turns: Dict[Any, int] = {}  # turn_key -> index of close
        for i, e in enumerate(self.events):
            evt_type = e.get("event") or e.get("type")
            if "closed" in (evt_type or ""):
                k = e.get(key) or e.get("turn_index")
                if k is not None:
                    closed_turns[k] = i

        for turn_key, close_idx in closed_turns.items():
            for i in range(close_idx + 1, len(self.events)):
                e = self.events[i]
                evt_type = e.get("event") or e.get("type")
                k = e.get(key) or e.get("turn_index")
                if k == turn_key and evt_type in (
                    "turn_closed", "turn_eou", "turn_semantic_decision",
                    "turn_eou_candidate", "smart_turn_decision"
                ):
                    self.result.add_violation(
                        f"ownership:no_late_mutation turn_key={turn_key}",
                        f"Event {evt_type} at index {i} mutates closed turn "
                        f"(closed at index {close_idx})",
                        [e],
                    )
        self.result.add_pass("ownership:forbid_late_mutation_after_close")

    # ── sample windows ───────────────────────────────────────────────────

    def _eval_sample_windows(self) -> None:
        windows = self.dsl.get("sample_window", [])
        for wi, win in enumerate(windows):
            if not isinstance(win, dict):
                continue
            name = win.get("name", f"sample_window[{wi}]")
            event_type = win.get("event")
            forbid_event = win.get("forbid_event")
            field = win.get("field", "decision_sample")
            min_from = win.get("min_from")
            max_from = win.get("max_from")

            if event_type and min_from and max_from:
                matched = [e for e in self.events if matches_selector(e, {"event": event_type})]
                min_val = _resolve_value_from(min_from, self.events)
                max_val = _resolve_value_from(max_from, self.events)

                if min_val is None or max_val is None:
                    self.result.add_violation(
                        name, f"Cannot resolve window bounds: min_from={min_from}, max_from={max_from}"
                    )
                    continue

                for event in matched:
                    actual = _resolve_path(event, field)
                    if actual is not None and (actual < min_val or actual > max_val):
                        self.result.add_violation(
                            name,
                            f"Expected {field} in [{min_val}, {max_val}], got {actual}",
                            [event],
                        )
                self.result.add_pass(name)

            if forbid_event:
                win_range = win.get("window")
                if win_range and len(win_range) == 2:
                    r0 = _resolve_value_from(win_range[0], self.events)
                    r1 = _resolve_value_from(win_range[1], self.events)
                    forbidden = [e for e in self.events
                                 if matches_selector(e, {"event": forbid_event})]
                    if r0 is not None and r1 is not None:
                        for e in forbidden:
                            actual = _resolve_path(e, field)
                            if actual is not None and r0 <= actual <= r1:
                                self.result.add_violation(
                                    name,
                                    f"Forbidden event {forbid_event} in window [{r0}, {r1}]",
                                    [e],
                                )

    # ── gaps ─────────────────────────────────────────────────────────────

    def _eval_gaps(self) -> None:
        gaps_policy = self.dsl.get("gaps", {}).get("expected")
        if gaps_policy is None:
            return
        gap_events = [e for e in self.events
                      if (e.get("event") or e.get("type")) in ("audio_gap", "audio_sample_gap")]
        if gaps_policy == "none" and gap_events:
            self.result.add_violation(
                "gaps:expected=none",
                f"Found {len(gap_events)} gap events",
                gap_events[:3],
            )
        elif gaps_policy == "none":
            self.result.add_pass("gaps:expected=none")

    # ── late revisions ───────────────────────────────────────────────────

    def _eval_late_revisions(self) -> None:
        policy = self.dsl.get("late_revisions", {}).get("policy")
        if not policy:
            return

        # Find late revisions: transcript events after close with a new turn_id
        closed_indices = [
            i for i, e in enumerate(self.events)
            if "closed" in ((e.get("event") or e.get("type")) or "")
        ]
        if not closed_indices:
            return

        last_close = max(closed_indices)
        late_transcripts = [
            e for e in self.events[last_close:]
            if (e.get("event") or e.get("type")) in (
                "transcript_token_committed", "transcript_update"
            )
        ]

        if policy == "forbid_after_close" and late_transcripts:
            self.result.add_violation(
                "late_revisions:forbid_after_close",
                f"Found {len(late_transcripts)} transcript events after close",
                late_transcripts[:3],
            )
        elif policy == "forbid_after_close":
            self.result.add_pass("late_revisions:forbid_after_close")


# ── transcript normalization ─────────────────────────────────────────────────

def _normalize_text(text: str, mode: str) -> str:
    """Normalize transcript text according to mode."""
    if mode == "lowercase_strip_punctuation_whitespace":
        text = text.lower()
        text = re.sub(r"[^\w\s]", "", text)
        text = re.sub(r"\s+", " ", text).strip()
    return text


# ── value_from resolution ────────────────────────────────────────────────────

def _resolve_value_from(expr: str, events: EventStream) -> Optional[int]:
    """Resolve expressions like 'event(vad_speech_end).decision_sample' or
    'event(vad_speech_end).end_sample + samples(2500ms)'."""
    # Parse: event(name).field [+ samples(Nms)]
    expr = expr.strip()
    parts = expr.split("+")
    base_expr = parts[0].strip()
    added_samples = 0
    if len(parts) > 1:
        add_part = parts[1].strip()
        m = re.match(r"samples\((\d+)ms\)", add_part)
        if m:
            from .constants import ms_to_samples
            added_samples = ms_to_samples(int(m.group(1)))

    # Parse event(name).field
    m = re.match(r"event\((\w+)\)\.(\S+)", base_expr)
    if not m:
        return None
    event_name = m.group(1)
    field_name = m.group(2)

    for e in events:
        if (e.get("event") or e.get("type")) == event_name:
            val = _resolve_path(e, field_name)
            if val is not None:
                return int(val) + added_samples
    return None


# ── top-level assertion runner ───────────────────────────────────────────────

def run_assertions(
    events: EventStream,
    dsl: Dict[str, Any],
    scenario_dir: Optional[Path] = None,
) -> Tuple[int, AssertionResult]:
    """Run DSL assertions against events. Returns (exit_code, result)."""
    evaluator = AssertionEvaluator(dsl, events, scenario_dir)
    result = evaluator.evaluate()
    if result.all_passed:
        return EXIT_PASS, result
    else:
        return EXIT_ASSERTION_FAILED, result
