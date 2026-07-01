"""置信度分层规则引擎（机制 4）

核心原则：tier 由规则引擎计算，不接受 LLM 自评。

SPEC §1.4 机制 4:
  - Certain (>0.9, 3+ independent sources)
  - Likely (0.7-0.9, 2+ sources)
  - Suspected (0.4-0.7, 1+ source)
  - Ruled-out (disproven)

tier → action 硬映射:
  - certain     → conclude（可以输出结论）
  - likely      → seek_one_more（补一个证据）
  - suspected   → must_investigate_further（证据远不够）
  - ruled_out   → switch_hypothesis（换假设）
"""
from __future__ import annotations

from microtrace.context.models import (
    ConfidenceTier,
    Hypothesis,
    Evidence,
    EvidenceSource,
    HypothesisStatus,
)


# ── 阈值常量 ─────────────────────────────────────────────────

CERTAIN_CONFIDENCE = 0.9
CERTAIN_MIN_SOURCES = 3

LIKELY_CONFIDENCE = 0.7
LIKELY_MIN_SOURCES = 2

SUSPECTED_CONFIDENCE = 0.4
SUSPECTED_MIN_SOURCES = 1


# ── 主计算函数 ──────────────────────────────────────────────

def compute_confidence_tier(
    hypothesis: Hypothesis,
    evidence_pool: list[Evidence],
) -> ConfidenceTier:
    """
    规则引擎计算置信度分层（不接受 LLM 自评）

    算法：
      1. 如果 status == RULED_OUT → RULED_OUT
      2. 统计独立证据源数量
      3. 按 confidence + source count 查表
      4. 边界情况：高 confidence 但 0 证据 → 降为 SUSPECTED
    """
    # 规则 0：已排除
    if hypothesis.status == HypothesisStatus.RULED_OUT:
        return ConfidenceTier.RULED_OUT

    # 统计支持该假设的独立证据源
    supporting_evidence = _get_supporting_evidence(hypothesis, evidence_pool)
    independent_sources = _count_independent_sources(supporting_evidence)
    confidence = hypothesis.confidence

    # 规则 1：certain
    if confidence >= CERTAIN_CONFIDENCE and independent_sources >= CERTAIN_MIN_SOURCES:
        return ConfidenceTier.CERTAIN

    # 规则 2：likely
    if confidence >= LIKELY_CONFIDENCE and independent_sources >= LIKELY_MIN_SOURCES:
        return ConfidenceTier.LIKELY

    # 规则 3：suspected
    if confidence >= SUSPECTED_CONFIDENCE and independent_sources >= SUSPECTED_MIN_SOURCES:
        return ConfidenceTier.SUSPECTED

    # 规则 4：边界情况 — 高置信度但没证据 → 不可信
    if confidence >= LIKELY_CONFIDENCE and independent_sources == 0:
        return ConfidenceTier.SUSPECTED

    # 规则 5：兜底 — 无证据 / 低置信度
    if independent_sources == 0:
        return ConfidenceTier.SUSPECTED

    # 全部不满足 → suspected
    return ConfidenceTier.SUSPECTED


def tier_to_action(tier: ConfidenceTier) -> str:
    """
    confidence tier → 下一步行动硬映射（不接受 LLM 决策）

    映射表：
      certain   → "conclude"
      likely    → "seek_one_more"
      suspected → "must_investigate_further"
      ruled_out → "switch_hypothesis"
    """
    mapping = {
        ConfidenceTier.CERTAIN: "conclude",
        ConfidenceTier.LIKELY: "seek_one_more",
        ConfidenceTier.SUSPECTED: "must_investigate_further",
        ConfidenceTier.RULED_OUT: "switch_hypothesis",
    }
    return mapping.get(tier, "must_investigate_further")


def is_ready_to_conclude(tier: ConfidenceTier) -> bool:
    """是否可以向用户输出结论"""
    return tier == ConfidenceTier.CERTAIN


def needs_more_evidence(tier: ConfidenceTier) -> bool:
    """是否需要更多证据"""
    return tier in (ConfidenceTier.SUSPECTED, ConfidenceTier.LIKELY)


# ── 辅助函数 ────────────────────────────────────────────────

def _get_supporting_evidence(
    hypothesis: Hypothesis,
    evidence_pool: list[Evidence],
) -> list[Evidence]:
    """提取该假设的支持证据"""
    evidence_map = {ev.id: ev for ev in evidence_pool}
    result = []
    for ev_id in hypothesis.evidence_for:
        ev = evidence_map.get(ev_id)
        if ev:
            result.append(ev)
    return result


def _count_independent_sources(evidence_list: list[Evidence]) -> int:
    """
    统计独立证据源数量

    独立源定义：不同 EvidenceSource 值即为独立源。
    同一 source 的多条 evidence 算同一个独立源。
    例如：3 条 code evidence + 1 条 log evidence = 2 个独立源
    """
    sources: set[str] = set()
    for ev in evidence_list:
        src = ev.source
        if isinstance(src, EvidenceSource):
            src = src.value
        sources.add(src)
    return len(sources)


def format_tier_explanation(
    tier: ConfidenceTier,
    hypothesis: Hypothesis,
    independent_sources: int,
) -> str:
    """生成 tier 判定解释（用于 reasoning_trace）"""
    return (
        f"[置信度分层] hypothesis={hypothesis.statement[:60]} "
        f"tier={tier.value} "
        f"confidence={hypothesis.confidence:.2f} "
        f"independent_sources={independent_sources} "
        f"evidence_for={len(hypothesis.evidence_for)} "
        f"evidence_against={len(hypothesis.evidence_against)}"
    )
