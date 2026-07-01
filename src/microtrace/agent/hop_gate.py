"""逐跳验证门控规则引擎（机制 2）

核心原则：Gate 由规则引擎判断（不是 LLM），证据不够不推进。

SPEC §1.4 机制 2:
  - PASS: 证据充分，允许推进到下一 hop
  - HOLD: 证据不足，留在当前 hop
  - BACKTRACK: 新证据矛盾，回滚到上一 hop
  - FAIL: 致命矛盾，标记诊断失败

Gate 插入位置（CLAUDE.md Q4 方案 B）：
  run_session() 外层，agent_iteration 返回后检查
"""
from __future__ import annotations

from microtrace.context.models import (
    Context,
    Evidence,
    EvidenceImportance,
    GateResult,
    Hypothesis,
    HypothesisStatus,
)


# ── 阈值常量 ───────────────────────────────────────────────

MIN_EVIDENCE_PER_HOP = 1       # 每跳至少 1 条新证据
MIN_CRITICAL_PER_HOP = 1       # 每跳至少 1 条 critical（可配置为 0）
MAX_HOPS_WITHOUT_CRITICAL = 2  # 连续 N 跳无 critical → HOLD


# ── 主 Gate 函数 ───────────────────────────────────────────

def check_hop_gate(ctx: Context) -> GateResult:
    """
    逐跳验证 Gate（规则引擎，非 LLM）

    检查顺序：
      1. 致命矛盾检查 → FAIL
      2. 当前跳证据充分性 → PASS / HOLD
      3. 新证据 vs 当前假设一致性 → BACKTRACK / PASS

    Returns:
        GateResult: PASS / HOLD / BACKTRACK / FAIL
    """
    hop = ctx.current_hop
    if hop == 0:
        # 第 0 跳（INTAKE 刚完成）：默认 PASS
        return GateResult.PASS

    # 获取当前跳内产生的新 evidence
    hop_evidence = _get_evidence_for_hop(ctx, hop)

    # ── 检查 1：致命矛盾 ──
    fatal = _check_fatal_contradiction(ctx, hop_evidence)
    if fatal:
        ctx.append_reasoning(f"[GATE Hop {hop}] FAIL: 致命矛盾检测")
        return GateResult.FAIL

    # ── 检查 2：证据充分性 ──
    if not _has_sufficient_evidence(ctx, hop_evidence):
        ctx.append_reasoning(
            f"[GATE Hop {hop}] HOLD: 证据不足 "
            f"(total={len(hop_evidence)}, critical={_count_critical(hop_evidence)})"
        )
        return GateResult.HOLD

    # ── 检查 3：证据一致性 ──
    if _has_contradiction(ctx, hop_evidence):
        ctx.append_reasoning(
            f"[GATE Hop {hop}] BACKTRACK: 新证据与当前假设矛盾"
        )
        return GateResult.BACKTRACK

    # ── 全部通过 ──
    ctx.append_reasoning(
        f"[GATE Hop {hop}] PASS: "
        f"evidence={len(hop_evidence)}, critical={_count_critical(hop_evidence)}"
    )
    return GateResult.PASS


def get_gate_action(gate_result: GateResult) -> str:
    """
    Gate 判定 → 下一步 action 映射
    """
    mapping = {
        GateResult.PASS: "continue",
        GateResult.HOLD: "gather_more_evidence",
        GateResult.BACKTRACK: "rollback_hypothesis",
        GateResult.FAIL: "mark_failed",
    }
    return mapping.get(gate_result, "gather_more_evidence")


# ── 内部检查函数 ───────────────────────────────────────────

def _get_evidence_for_hop(ctx: Context, hop: int) -> list[Evidence]:
    """获取指定 hop 内产生的 evidence（用 discovered_at_iteration 近似）"""
    # Phase 1 简化：用 iteration 范围近似 hop
    # 后续可加 hop_start_iteration 字段做精确映射
    min_iter = _hop_to_min_iteration(ctx, hop)
    max_iter = ctx.iteration
    return [
        ev for ev in ctx.evidence
        if min_iter <= ev.discovered_at_iteration <= max_iter
    ]


def _hop_to_min_iteration(ctx: Context, hop: int) -> int:
    """hop → 最小 iteration（简化映射：hop 1 = iter 1）"""
    if hop <= 1:
        return 1
    # 简化：每 hop 约 2 轮
    return (hop - 1) * 2


def _has_sufficient_evidence(
    ctx: Context,
    hop_evidence: list[Evidence],
) -> bool:
    """检查当前 hop 证据是否充分"""
    if len(hop_evidence) < MIN_EVIDENCE_PER_HOP:
        return False

    # 检查是否有 critical evidence
    critical_count = _count_critical(hop_evidence)
    if critical_count < MIN_CRITICAL_PER_HOP:
        # 容忍 MAX_HOPS_WITHOUT_CRITICAL 跳
        recent_hops_critical = _count_recent_critical_hops(ctx)
        if recent_hops_critical >= MAX_HOPS_WITHOUT_CRITICAL:
            return False

    return True


def _has_contradiction(
    ctx: Context,
    hop_evidence: list[Evidence],
) -> bool:
    """
    检查新证据是否与当前假设矛盾

    简化规则：如果当前 hop 的所有 evidence 都与当前聚焦假设无关
    （不在 evidence_for 中），且当前假设仍为 candidate（未确认），
    则可能是方向错了。
    """
    current = ctx.hypotheses.current_focus
    if not current or not ctx.hypotheses.hypotheses:
        return False

    hyp = ctx.hypotheses.get(current)
    if not hyp:
        return False

    # 如果假设已被确认 → 新证据不太可能推翻
    if hyp.status == HypothesisStatus.CONFIRMED:
        return False

    # 检查：新证据中有多少与该假设有关
    hop_ev_ids = {ev.id for ev in hop_evidence}
    supporting = set(hyp.evidence_for)
    contradicting = set(hyp.evidence_against)

    # 新证据既不在支持列表也不在否定列表 → 无关证据多了 → 可能方向不对
    unrelated = hop_ev_ids - supporting - contradicting
    if len(unrelated) > 0 and len(supporting & hop_ev_ids) == 0:
        return True

    # 新证据出现在否定列表 → 矛盾
    if len(contradicting & hop_ev_ids) > 0:
        return True

    return False


def _check_fatal_contradiction(
    ctx: Context,
    hop_evidence: list[Evidence],
) -> bool:
    """
    检查致命矛盾：所有假设都被排除
    """
    if not ctx.hypotheses.hypotheses:
        return False

    all_ruled_out = all(
        h.status == HypothesisStatus.RULED_OUT
        for h in ctx.hypotheses.hypotheses
    )
    if all_ruled_out:
        return True

    # 所有假设都是 candidate（没有任何进展）已经超过一半轮次
    all_candidate = all(
        h.status == HypothesisStatus.CANDIDATE
        for h in ctx.hypotheses.hypotheses
    )
    if all_candidate and ctx.iteration > ctx.max_iterations // 2:
        return True

    return False


def _count_critical(evidence_list: list[Evidence]) -> int:
    """统计 critical evidence 数量"""
    count = 0
    for ev in evidence_list:
        imp = ev.importance
        if isinstance(imp, EvidenceImportance):
            imp = imp.value
        if imp == "critical":
            count += 1
    return count


def _count_recent_critical_hops(ctx: Context) -> int:
    """统计最近连续无 critical 的 hop 数（简化实现）"""
    # Phase 1 简化：检查最近 2 轮 iteration
    recent_evidence = [
        ev for ev in ctx.evidence
        if ev.discovered_at_iteration >= max(1, ctx.iteration - 3)
    ]
    if not recent_evidence:
        return MAX_HOPS_WITHOUT_CRITICAL
    return 0 if _count_critical(recent_evidence) > 0 else MAX_HOPS_WITHOUT_CRITICAL
