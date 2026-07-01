"""置信度分层规则引擎测试（机制 4）"""
import pytest
from microtrace.context.models import (
    Hypothesis,
    HypothesisStatus,
    Evidence,
    EvidenceSource,
    EvidenceImportance,
    ConfidenceTier,
)
from microtrace.agent.confidence import (
    compute_confidence_tier,
    tier_to_action,
    is_ready_to_conclude,
    needs_more_evidence,
    _count_independent_sources,
)


def make_evidence(source: EvidenceSource, ev_id: str = "ev1") -> Evidence:
    return Evidence(
        id=ev_id,
        source=source,
        location="test",
        content="test content",
        raw_content="test",
        discovered_at_iteration=1,
    )


def make_hypothesis(
    status: HypothesisStatus = HypothesisStatus.CANDIDATE,
    confidence: float = 0.5,
    evidence_for: list[str] | None = None,
) -> Hypothesis:
    return Hypothesis(
        statement="Test hypothesis",
        status=status,
        confidence=confidence,
        evidence_for=evidence_for or [],
    )


class TestComputeConfidenceTier:
    """compute_confidence_tier() 规则引擎测试"""

    def test_ruled_out_returns_ruled_out(self):
        """已排除 → RULED_OUT"""
        hyp = make_hypothesis(status=HypothesisStatus.RULED_OUT)
        tier = compute_confidence_tier(hyp, [])
        assert tier == ConfidenceTier.RULED_OUT

    def test_certain_3_sources_high_confidence(self):
        """3 独立源 + confidence > 0.9 → CERTAIN"""
        hyp = make_hypothesis(
            confidence=0.95,
            evidence_for=["ev1", "ev2", "ev3"],
        )
        evidence = [
            make_evidence(EvidenceSource.CODE, "ev1"),
            make_evidence(EvidenceSource.LOG, "ev2"),
            make_evidence(EvidenceSource.STACK, "ev3"),
        ]
        tier = compute_confidence_tier(hyp, evidence)
        assert tier == ConfidenceTier.CERTAIN

    def test_likely_2_sources_medium_confidence(self):
        """2 独立源 + confidence 0.7-0.9 → LIKELY"""
        hyp = make_hypothesis(
            confidence=0.8,
            evidence_for=["ev1", "ev2"],
        )
        evidence = [
            make_evidence(EvidenceSource.CODE, "ev1"),
            make_evidence(EvidenceSource.LOG, "ev2"),
        ]
        tier = compute_confidence_tier(hyp, evidence)
        assert tier == ConfidenceTier.LIKELY

    def test_suspected_1_source_low_confidence(self):
        """1 源 + confidence 0.4-0.7 → SUSPECTED"""
        hyp = make_hypothesis(
            confidence=0.5,
            evidence_for=["ev1"],
        )
        evidence = [make_evidence(EvidenceSource.CODE, "ev1")]
        tier = compute_confidence_tier(hyp, evidence)
        assert tier == ConfidenceTier.SUSPECTED

    def test_high_confidence_but_zero_evidence(self):
        """高 confidence 但 0 证据 → 降为 SUSPECTED"""
        hyp = make_hypothesis(confidence=0.95, evidence_for=[])
        tier = compute_confidence_tier(hyp, [])
        assert tier == ConfidenceTier.SUSPECTED

    def test_same_source_counts_as_one(self):
        """同一 source 的多条 evidence → 算 1 个独立源"""
        hyp = make_hypothesis(
            confidence=0.95,
            evidence_for=["ev1", "ev2", "ev3"],
        )
        evidence = [
            make_evidence(EvidenceSource.CODE, "ev1"),  # same source
            make_evidence(EvidenceSource.CODE, "ev2"),  # same source
            make_evidence(EvidenceSource.CODE, "ev3"),  # same source
        ]
        tier = compute_confidence_tier(hyp, evidence)
        # 3 evidence but 1 independent source → not CERTAIN
        assert tier in (ConfidenceTier.SUSPECTED, ConfidenceTier.LIKELY)


class TestTierToAction:
    """tier → action 硬映射测试"""

    def test_certain_to_conclude(self):
        assert tier_to_action(ConfidenceTier.CERTAIN) == "conclude"

    def test_likely_to_seek_one_more(self):
        assert tier_to_action(ConfidenceTier.LIKELY) == "seek_one_more"

    def test_suspected_to_must_investigate(self):
        assert tier_to_action(ConfidenceTier.SUSPECTED) == "must_investigate_further"

    def test_ruled_out_to_switch(self):
        assert tier_to_action(ConfidenceTier.RULED_OUT) == "switch_hypothesis"


class TestIndependentSources:
    """独立源计数测试"""

    def test_empty(self):
        assert _count_independent_sources([]) == 0

    def test_two_sources(self):
        ev = [
            make_evidence(EvidenceSource.CODE),
            make_evidence(EvidenceSource.LOG),
        ]
        assert _count_independent_sources(ev) == 2

    def test_three_same_source(self):
        ev = [
            make_evidence(EvidenceSource.CODE),
            make_evidence(EvidenceSource.CODE),
            make_evidence(EvidenceSource.CODE),
        ]
        assert _count_independent_sources(ev) == 1

    def test_five_different_sources(self):
        ev = [
            make_evidence(EvidenceSource.CODE),
            make_evidence(EvidenceSource.LOG),
            make_evidence(EvidenceSource.STACK),
            make_evidence(EvidenceSource.USER),
            make_evidence(EvidenceSource.TOOL_OUTPUT),
        ]
        assert _count_independent_sources(ev) == 5


class TestReadinessChecks:
    def test_ready_to_conclude_only_certain(self):
        assert is_ready_to_conclude(ConfidenceTier.CERTAIN) is True
        assert is_ready_to_conclude(ConfidenceTier.LIKELY) is False
        assert is_ready_to_conclude(ConfidenceTier.SUSPECTED) is False
        assert is_ready_to_conclude(ConfidenceTier.RULED_OUT) is False

    def test_needs_more_evidence(self):
        assert needs_more_evidence(ConfidenceTier.SUSPECTED) is True
        assert needs_more_evidence(ConfidenceTier.LIKELY) is True
        assert needs_more_evidence(ConfidenceTier.CERTAIN) is False
        assert needs_more_evidence(ConfidenceTier.RULED_OUT) is False
