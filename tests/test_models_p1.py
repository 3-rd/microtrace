"""Phase 1 数据模型测试（HypothesisSet, DiagnosisClaim, DiagnosisPattern）"""
import pytest
from microtrace.context.models import (
    Hypothesis,
    HypothesisSet,
    HypothesisStatus,
    DiagnosisClaim,
    DiagnosisPattern,
    ConfidenceTier,
    PatternStatus,
    JudgmentCategory,
    Context,
)


class TestHypothesis:
    """Hypothesis 四态生命周期测试"""

    def test_default_status_is_candidate(self):
        h = Hypothesis(statement="test")
        assert h.status == HypothesisStatus.CANDIDATE

    def test_add_supporting_evidence(self):
        h = Hypothesis(statement="test")
        h.add_supporting_evidence("ev1")
        assert "ev1" in h.evidence_for

    def test_add_contradicting_evidence(self):
        h = Hypothesis(statement="test")
        h.add_contradicting_evidence("ev1")
        assert "ev1" in h.evidence_against

    def test_no_duplicate_evidence(self):
        h = Hypothesis(statement="test")
        h.add_supporting_evidence("ev1")
        h.add_supporting_evidence("ev1")
        assert len(h.evidence_for) == 1

    def test_to_brief(self):
        h = Hypothesis(
            statement="NPE caused by missing null check",
            category=JudgmentCategory.A,
            status=HypothesisStatus.CANDIDATE,
            confidence=0.7,
        )
        brief = h.to_brief()
        assert "[candidate]" in brief
        assert "A" in brief
        assert "0.70" in brief


class TestHypothesisSet:
    """HypothesisSet 集合操作测试"""

    def test_empty_set(self):
        hs = HypothesisSet()
        assert hs.confirmed == []
        assert hs.best is None
        assert hs.to_brief() == "（无假设）"

    def test_add_and_get(self):
        hs = HypothesisSet()
        h = Hypothesis(statement="H1")
        hs.add(h)
        assert hs.get(h.id) == h

    def test_lifecycle_candidate_to_confirmed(self):
        hs = HypothesisSet()
        h = Hypothesis(statement="H1", status=HypothesisStatus.CANDIDATE)
        hs.add(h)
        hs.set_focus(h.id)
        assert hs.get(h.id).status == HypothesisStatus.INVESTIGATING
        hs.confirm(h.id)
        assert hs.get(h.id).status == HypothesisStatus.CONFIRMED

    def test_lifecycle_candidate_to_ruled_out(self):
        hs = HypothesisSet()
        h = Hypothesis(statement="H1", status=HypothesisStatus.CANDIDATE)
        hs.add(h)
        hs.set_focus(h.id)
        hs.rule_out(h.id, "evidence contradicts")
        updated = hs.get(h.id)
        assert updated.status == HypothesisStatus.RULED_OUT
        assert updated.ruled_out_reason == "evidence contradicts"

    def test_best_returns_highest_confidence_confirmed(self):
        hs = HypothesisSet()
        h1 = Hypothesis(statement="H1", status=HypothesisStatus.CONFIRMED, confidence=0.8)
        h2 = Hypothesis(statement="H2", status=HypothesisStatus.CONFIRMED, confidence=0.9)
        hs.add(h1)
        hs.add(h2)
        assert hs.best == h2

    def test_confirmed_filter(self):
        hs = HypothesisSet()
        hs.add(Hypothesis(statement="H1", status=HypothesisStatus.CANDIDATE))
        hs.add(Hypothesis(statement="H2", status=HypothesisStatus.CONFIRMED, confidence=0.8))
        hs.add(Hypothesis(statement="H3", status=HypothesisStatus.RULED_OUT))
        assert len(hs.confirmed) == 1
        assert len(hs.candidates) == 1
        assert len(hs.ruled_out) == 1


class TestDiagnosisClaim:
    """DiagnosisClaim 证据锚定验证测试"""

    def test_is_valid_with_evidence(self):
        claim = DiagnosisClaim(
            category=JudgmentCategory.A,
            statement="Root cause identified",
            evidence_refs=["ev1", "ev2"],
            confidence_tier=ConfidenceTier.LIKELY,
        )
        assert claim.is_valid is True
        assert claim.evidence_count == 2

    def test_is_valid_fails_without_evidence(self):
        claim = DiagnosisClaim(
            category=JudgmentCategory.A,
            statement="No evidence",
            evidence_refs=[],
        )
        assert claim.is_valid is False


class TestDiagnosisPattern:
    """DiagnosisPattern 三态生命周期测试"""

    def test_default_status_active(self):
        import time
        p = DiagnosisPattern(
            symptom_signature="test",
            diagnosis_template="X",
            created_at=time.time(),
        )
        assert p.status == PatternStatus.ACTIVE

    def test_mark_stale_then_archive(self):
        import time
        p = DiagnosisPattern(
            symptom_signature="test",
            diagnosis_template="X",
            created_at=time.time(),
        )
        p.mark_stale()
        assert p.status == PatternStatus.STALE
        p.archive()
        assert p.status == PatternStatus.ARCHIVED

    def test_record_success_revives(self):
        import time
        p = DiagnosisPattern(
            symptom_signature="test",
            diagnosis_template="X",
            status=PatternStatus.STALE,
            success_count=5,
            failure_count=2,
            created_at=time.time(),
        )
        p.record_success()
        assert p.status == PatternStatus.ACTIVE
        assert p.success_count == 6


class TestContextPhase1:
    """Context Phase 1 新增字段测试"""

    def test_default_hypotheses_is_empty(self):
        ctx = Context()
        assert len(ctx.hypotheses.hypotheses) == 0

    def test_add_hypothesis(self):
        ctx = Context()
        h = Hypothesis(statement="test")
        ctx.add_hypothesis(h)
        assert len(ctx.hypotheses.hypotheses) == 1

    def test_increment_hop(self):
        ctx = Context()
        assert ctx.current_hop == 0
        ctx.increment_hop()
        assert ctx.current_hop == 1
        ctx.increment_hop()
        assert ctx.current_hop == 2

    def test_default_confidence_tier(self):
        ctx = Context()
        assert ctx.confidence_tier == ConfidenceTier.SUSPECTED
