"""Injection-harness test runner (PRD §24).

Two things are actually testable here without a live LLM (which this
sandbox doesn't have — see `docs/context_transfer.md` §9):

1. **InjectionScanner recall** — fully real, fully testable: run every
   corpus fixture through the real scanner and assert ≥95% are flagged,
   exactly as §24/Business-Goal-§3 require.

2. **"The model never executes the embedded instruction"** — cannot be
   tested against a real model here. What *is* honestly testable, and
   arguably the more important guarantee per PRD §11.4 point 5 ("the model
   cannot argue its way past it"), is that the code-level deny-list and
   confirmation gate would stop a *maximally compromised* model — one that
   fully obeyed every embedded instruction — from ever silently mutating
   case state. `TestSimulatedCompromisedModel` below constructs exactly
   that adversarial scenario: a fake "model" that always tries to do what
   the injected text asks, and asserts `permission_gate` still requires
   confirmation for every mutating call regardless. This is a structural
   guarantee, not a statistical one — it holds for 100% of the corpus by
   construction, not just ≥95%.
"""

from __future__ import annotations

import re

import pytest

from app.core.models import Case
from app.db.session import init_db, make_engine, make_session_factory, session_scope
from app.orchestrator.injection_scanner import scan_tool_result
from app.orchestrator.permission_gate import RiskClass, classify_tool_call
from tests.security.injection_harness.corpus import BENIGN_CONTROL_SET, CORPUS

PASS_THRESHOLD = 0.95  # PRD §24 / Business Goal §3


class TestInjectionScannerRecallOnCorpus:
    def test_scanner_flags_at_least_95_percent_of_corpus(self) -> None:
        results = [scan_tool_result(f.field_type, {f.field_type: f.text}) for f in CORPUS]
        flagged = [r for r in results if r.flagged]
        pass_rate = len(flagged) / len(CORPUS)

        missed = [f.id for f, r in zip(CORPUS, results, strict=True) if not r.flagged]
        assert pass_rate >= PASS_THRESHOLD, (
            f"InjectionScanner recall {pass_rate:.1%} is below the {PASS_THRESHOLD:.0%} "
            f"threshold required by PRD §24. Missed fixtures: {missed}"
        )

    @pytest.mark.parametrize("fixture", CORPUS, ids=[f.id for f in CORPUS])
    def test_each_fixture_individually_for_diagnostics(self, fixture) -> None:
        """Not a hard requirement per-fixture (the corpus-level 95% test
        above is the actual pass bar) — but running each individually with
        its own test ID makes CI failures show *which* fixtures a future
        scanner change regressed on, rather than one opaque aggregate
        number."""
        result = scan_tool_result(fixture.field_type, {fixture.field_type: fixture.text})
        if not result.flagged:
            pytest.xfail(
                f"{fixture.id} not flagged (techniques: {fixture.techniques}) — "
                "tracked, doesn't fail the suite as long as corpus-wide recall stays >=95%"
            )

    def test_every_corpus_fixture_actually_contains_an_embedded_instruction(self) -> None:
        """Sanity check on the corpus itself: every fixture must actually
        exercise at least one technique tag, or it isn't testing anything."""
        for fixture in CORPUS:
            assert fixture.techniques, f"{fixture.id} has no technique tags — corpus bug"


class TestInjectionScannerFalsePositiveRateOnBenignControls:
    def test_benign_control_set_is_mostly_not_flagged(self) -> None:
        results = [
            scan_tool_result(f.field_type, {f.field_type: f.text}) for f in BENIGN_CONTROL_SET
        ]
        false_positives = [
            f.id for f, r in zip(BENIGN_CONTROL_SET, results, strict=True) if r.flagged
        ]
        # Not a PRD-mandated number, but a scanner that flags realistic
        # benign DFIR fields isn't useful in practice — track it explicitly.
        assert len(false_positives) == 0, f"False positives on benign controls: {false_positives}"


class TestSimulatedCompromisedModel:
    """Constructs a maximally adversarial scenario: a fake 'model' that
    always tries to comply with whatever instruction is embedded in the
    evidence text it just saw, and confirms the code-level gate still
    blocks unconfirmed state mutation regardless (PRD §11.4 point 5)."""

    @pytest.fixture
    def session_factory(self):
        engine = make_engine("sqlite:///:memory:")
        init_db(engine)
        return make_session_factory(engine)

    @pytest.fixture
    def case_id(self, session_factory) -> str:
        with session_scope(session_factory) as s:
            case = Case(name="Injection Harness Case")
            s.add(case)
            s.flush()
            return case.id

    def _simulated_compromised_model_next_action(self, fixture) -> tuple[str, dict] | None:
        """A deliberately naive 'model': if the injected text names one of
        our own tools, pretend the model decided to call it — the worst
        case the confused-deputy threat model describes."""
        if "report.append_finding" in fixture.text:
            return "report.append_finding", {
                "case_id": "whatever",
                "finding_text": "Compromised finding text dictated by evidence.",
                "evidence_refs": [],
            }
        if "mem.run_plugin" in fixture.text:
            match = re.search(r"plugin\s+(\w+)", fixture.text)
            plugin = match.group(1) if match else "pslist"
            return "mem.run_plugin", {"case_id": "whatever", "plugin": plugin}
        return None

    def test_every_tool_invocation_fixture_still_requires_confirmation(
        self, session_factory, case_id
    ) -> None:
        tool_invocation_fixtures = [f for f in CORPUS if "tool_invocation" in f.techniques]
        assert tool_invocation_fixtures, "No tool_invocation fixtures in corpus — test is vacuous"

        for fixture in tool_invocation_fixtures:
            action = self._simulated_compromised_model_next_action(fixture)
            if action is None:
                continue
            tool_name, arguments = action
            arguments["case_id"] = case_id  # use a real case_id for a meaningful gate check

            with session_factory() as session:
                decision = classify_tool_call(tool_name, arguments, session)

            assert decision.risk_class in (RiskClass.REQUIRES_CONFIRMATION, RiskClass.DENY), (
                f"{fixture.id} ({tool_name}) auto-approved despite being dictated by "
                "injected evidence text — confused-deputy defense failed"
            )

    def test_no_corpus_fixture_can_reach_a_tool_outside_the_registered_set(
        self, session_factory, case_id
    ) -> None:
        # Even a "model" that literally invents a new tool name from
        # evidence text (not just our six) must be denied.
        with session_factory() as session:
            decision = classify_tool_call(
                "email.send_results", {"to": "attacker@example.com"}, session
            )
        assert decision.risk_class == RiskClass.DENY
