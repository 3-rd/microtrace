"""矛盾检测 + 自动回溯测试（机制 5）"""
import pytest
from microtrace.context.models import (
    Context,
    Hypothesis,
    HypothesisStatus,
    Evidence,
    EvidenceSource,
    JudgmentCategory,
)
from microtrace.agent.contradiction import (
    check_evidence_contradiction,
    apply_contradiction_result,
    ContradictionResult,
)


def make_context(hypotheses=None, current_focus=None):
    ctx = Context(iteration=2)
    if hypotheses:
        for h in hypotheses:
            ctx.hypotheses.add(h)
        if current_focus:
            ctx.hypotheses.current_focus = current_focus
    return ctx


def make_evidence(relevance: float = 0.5, ev_id: str = "ev1"):
    return Evidence(
        id=ev_id,
        source=EvidenceSource.CODE,
        location="test",
        content="test",
        raw_content="test",
        relevance=relevance,
        discovered_at_iteration=2,
    )


class TestCheckContradiction:
    """check_evidence_contradiction() 测试"""

    def test_no_hypotheses_no_contradiction(self):
        ctx = make_context()
        ev = make_evidence()
        result = check_evidence_contradiction(ctx, ev)
        assert result.found is False

    def test_low_relevance_no_contradiction(self):
        """relevance < 0.2 → 不构成矛盾"""
        h = Hypothesis(statement="H1", status=HypothesisStatus.INVESTIGATING)
        ctx = make_context(hypotheses=[h], current_focus=h.id)
        ev = make_evidence(relevance=0.1)
        result = check_evidence_contradiction(ctx, ev)
        assert result.found is False

    def test_evidence_in_against_list(self):
        """新证据在 evidence_against 中 → 矛盾"""
        h = Hypothesis(
            statement="H1",
            status=HypothesisStatus.INVESTIGATING,
            evidence_against=["ev1"],
        )
        ctx = make_context(hypotheses=[h], current_focus=h.id)
        ev = make_evidence(ev_id="ev1", relevance=0.8)
        result = check_evidence_contradiction(ctx, ev)
        assert result.found is True
        assert h.id in result.affected_hypothesis_ids

    def test_fatal_all_hypotheses_affected(self):
        """所有假设被否定 → fatal"""
        h1 = Hypothesis(
            statement="H1",
            status=HypothesisStatus.INVESTIGATING,
            evidence_against=["ev1"],
        )
        h2 = Hypothesis(
            statement="H2",
            status=HypothesisStatus.CANDIDATE,
            evidence_against=["ev1"],
        )
        ctx = make_context(hypotheses=[h1, h2], current_focus=h1.id)
        ev = make_evidence(ev_id="ev1", relevance=0.8)
        result = check_evidence_contradiction(ctx, ev)
        assert result.found is True
        assert result.severity == "fatal"
        assert result.suggested_action == "rollback"

    def test_major_current_focus_affected(self):
        """当前聚焦假设被否定 → major"""
        h1 = Hypothesis(
            statement="H1",
            status=HypothesisStatus.INVESTIGATING,
            evidence_against=["ev1"],
        )
        h2 = Hypothesis(statement="H2", status=HypothesisStatus.CANDIDATE)
        ctx = make_context(hypotheses=[h1, h2], current_focus=h1.id)
        ev = make_evidence(ev_id="ev1", relevance=0.8)
        result = check_evidence_contradiction(ctx, ev)
        assert result.severity == "major"
        assert result.suggested_action == "re_evaluate"


class TestApplyContradiction:
    """apply_contradiction_result() 测试"""

    def test_no_contradiction_no_change(self):
        ctx = make_context()
        result = ContradictionResult(found=False)
        apply_contradiction_result(ctx, result)
        # 无变化，不 crash

    def test_rollback_resets_hypotheses(self):
        """rollback: RULED_OUT → CANDIDATE"""
        h = Hypothesis(
            statement="H1",
            status=HypothesisStatus.RULED_OUT,
            ruled_out_reason="was wrong",
        )
        ctx = make_context(hypotheses=[h])
        result = ContradictionResult(
            found=True,
            severity="fatal",
            suggested_action="rollback",
            affected_hypothesis_ids=[h.id],
        )
        apply_contradiction_result(ctx, result)

        updated = ctx.hypotheses.get(h.id)
        assert updated.status == HypothesisStatus.CANDIDATE
        assert updated.ruled_out_reason is None
