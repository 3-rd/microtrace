"""逐跳验证 Gate 规则引擎测试（机制 2）"""
import pytest
from microtrace.context.models import (
    Context,
    GateResult,
    Hypothesis,
    HypothesisStatus,
    Evidence,
    EvidenceSource,
    EvidenceImportance,
    ContentType,
)
from microtrace.agent.hop_gate import (
    check_hop_gate,
    get_gate_action,
    _has_sufficient_evidence,
    _has_contradiction,
    _count_critical,
)


def make_context(
    hop: int = 1,
    iteration: int = 2,
    evidence: list[Evidence] | None = None,
    hypotheses: list[Hypothesis] | None = None,
    current_focus: str | None = None,
) -> Context:
    ctx = Context(current_hop=hop, iteration=iteration)
    if evidence:
        ctx.evidence = evidence
    if hypotheses:
        for h in hypotheses:
            ctx.hypotheses.add(h)
        if current_focus:
            ctx.hypotheses.current_focus = current_focus
    return ctx


def make_evidence(
    source: EvidenceSource = EvidenceSource.CODE,
    importance: EvidenceImportance = EvidenceImportance.CRITICAL,
    discovered_at: int = 1,
) -> Evidence:
    return Evidence(
        source=source,
        location="test",
        content="test",
        raw_content="test",
        importance=importance,
        content_type=ContentType.CRITICAL if importance == EvidenceImportance.CRITICAL else ContentType.COMPRESSIBLE,
        discovered_at_iteration=discovered_at,
    )


class TestCheckHopGate:
    """check_hop_gate() 规则引擎测试"""

    def test_hop_zero_default_pass(self):
        """Hop 0（INTAKE 刚完成）→ PASS"""
        ctx = make_context(hop=0)
        result = check_hop_gate(ctx)
        assert result == GateResult.PASS

    def test_hop_one_with_evidence(self):
        """有 critical evidence → PASS"""
        ctx = make_context(
            hop=1,
            iteration=2,
            evidence=[make_evidence(discovered_at=1), make_evidence(discovered_at=2)],
        )
        result = check_hop_gate(ctx)
        assert result == GateResult.PASS

    def test_no_evidence_in_hop(self):
        """Hop 内无 evidence → HOLD"""
        ctx = make_context(hop=2, iteration=3, evidence=[])
        result = check_hop_gate(ctx)
        assert result == GateResult.HOLD

    def test_all_hypotheses_ruled_out(self):
        """所有假设都被排除 → FAIL"""
        h1 = Hypothesis(statement="H1", status=HypothesisStatus.RULED_OUT, ruled_out_reason="test")
        h2 = Hypothesis(statement="H2", status=HypothesisStatus.RULED_OUT, ruled_out_reason="test")
        ctx = make_context(
            hop=2,
            iteration=5,
            hypotheses=[h1, h2],
            evidence=[make_evidence(discovered_at=1)],
        )
        ctx.max_iterations = 8
        result = check_hop_gate(ctx)
        assert result == GateResult.FAIL

    def test_all_candidate_past_half_iterations(self):
        """全 candidate + 已过一半迭代 → FAIL"""
        h1 = Hypothesis(statement="H1", status=HypothesisStatus.CANDIDATE)
        h2 = Hypothesis(statement="H2", status=HypothesisStatus.CANDIDATE)
        ctx = make_context(
            hop=2,
            iteration=6,
            hypotheses=[h1, h2],
            evidence=[make_evidence(discovered_at=1)],
        )
        ctx.max_iterations = 8
        result = check_hop_gate(ctx)
        assert result == GateResult.FAIL


class TestGetGateAction:
    def test_pass_to_continue(self):
        assert get_gate_action(GateResult.PASS) == "continue"

    def test_hold_to_gather(self):
        assert get_gate_action(GateResult.HOLD) == "gather_more_evidence"

    def test_backtrack_to_rollback(self):
        assert get_gate_action(GateResult.BACKTRACK) == "rollback_hypothesis"

    def test_fail_to_mark_failed(self):
        assert get_gate_action(GateResult.FAIL) == "mark_failed"


class TestCountCritical:
    def test_count_mixed(self):
        ev = [
            make_evidence(importance=EvidenceImportance.CRITICAL),
            make_evidence(importance=EvidenceImportance.CRITICAL),
            make_evidence(importance=EvidenceImportance.SUPPORTING),
        ]
        assert _count_critical(ev) == 2

    def test_count_none_critical(self):
        ev = [
            make_evidence(importance=EvidenceImportance.SUPPORTING),
            make_evidence(importance=EvidenceImportance.BACKGROUND),
        ]
        assert _count_critical(ev) == 0
