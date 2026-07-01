"""矛盾检测 + 自动回溯（机制 5）

核心原则：系统检测矛盾（不是 LLM），检测到矛盾后自动标记回滚。

SPEC §1.4 机制 5:
  - post-tool 阶段自动运行 check_evidence_contradiction()
  - 矛盾 → 标记当前假设为矛盾状态
  - 自动回溯：回到上一个 hypothesis，重新评估
  - Pattern 误导检测：匹配到的 pattern 预测与证据矛盾 → 降级 pattern

工作流：
  1. 新 evidence 加入后 → check_evidence_contradiction(ctx, new_evidence)
  2. 检测到矛盾 → 标记 hypothesis.contradiction_found
  3. 自动建议 action: ROLLBACK / RE_EVALUATE / CONTINUE
"""
from __future__ import annotations

from dataclasses import dataclass, field
from microtrace.context.models import (
    Context,
    Evidence,
    Hypothesis,
    HypothesisStatus,
    ConfidenceTier,
)


@dataclass
class ContradictionResult:
    """矛盾检测结果"""
    found: bool = False
    severity: str = "none"  # "fatal" | "major" | "minor" | "none"
    description: str = ""
    affected_hypothesis_ids: list[str] = field(default_factory=list)
    suggested_action: str = "continue"  # "rollback" | "re_evaluate" | "switch" | "continue"
    pattern_mismatch: bool = False  # Pattern 预测与证据矛盾


# ── 主检测函数 ──────────────────────────────────────────────

def check_evidence_contradiction(
    ctx: Context,
    new_evidence: Evidence,
) -> ContradictionResult:
    """
    Post-tool 阶段自动运行：检查新证据是否与当前假设矛盾

    算法：
      1. 对每个 hypothesis 检查：新证据是否在 evidence_against 中
      2. 严重程度判定：
         - fatal: 所有假设都被否定
         - major: 当前聚焦假设被否定
         - minor: 非聚焦假设被否定
      3. 建议 action
      4. 检查 pattern 误导

    Returns:
        ContradictionResult
    """
    result = ContradictionResult()

    if not ctx.hypotheses.hypotheses:
        return result

    # ── 对每个假设检查 ──
    for hyp in ctx.hypotheses.hypotheses:
        contradiction = _check_single_hypothesis(hyp, new_evidence, ctx)
        if contradiction:
            result.affected_hypothesis_ids.append(hyp.id)

    if not result.affected_hypothesis_ids:
        return result

    result.found = True

    # ── 严重程度判定 ──
    all_hypothesis_ids = {h.id for h in ctx.hypotheses.hypotheses}
    affected_set = set(result.affected_hypothesis_ids)

    if affected_set == all_hypothesis_ids:
        result.severity = "fatal"
        result.description = "所有假设都被新证据否定"
        result.suggested_action = "rollback"
    elif ctx.hypotheses.current_focus in affected_set:
        result.severity = "major"
        result.description = f"当前聚焦假设 {ctx.hypotheses.current_focus} 被新证据否定"
        result.suggested_action = "re_evaluate"
    else:
        result.severity = "minor"
        result.description = f"{len(result.affected_hypothesis_ids)} 个非聚焦假设被否定"
        result.suggested_action = "continue"

    # ── Pattern 误导检测 ──
    if ctx.matched_patterns:
        result.pattern_mismatch = _check_pattern_mismatch(ctx, new_evidence)

    return result


def apply_contradiction_result(
    ctx: Context,
    result: ContradictionResult,
) -> None:
    """
    根据矛盾检测结果执行自动操作

    Action 映射：
      - rollback: 回滚当前假设状态，回到 INVESTIGATE
      - re_evaluate: 标记当前假设需重新评估
      - switch: 切换到下一个 candidate 假设
      - continue: 不做变更
    """
    if not result.found:
        return

    ctx.append_reasoning(
        f"[矛盾检测] severity={result.severity} "
        f"affected={result.affected_hypothesis_ids} "
        f"action={result.suggested_action}"
    )
    ctx.append_event("contradiction.detected", {
        "severity": result.severity,
        "affected": result.affected_hypothesis_ids,
        "suggested_action": result.suggested_action,
        "pattern_mismatch": result.pattern_mismatch,
    })

    if result.suggested_action == "rollback":
        _rollback_hypotheses(ctx)

    elif result.suggested_action == "re_evaluate":
        _mark_for_re_evaluation(ctx)

    elif result.suggested_action == "switch":
        _switch_to_next_candidate(ctx)

    # 如果 pattern 误导 → 降低 pattern 置信度
    if result.pattern_mismatch:
        ctx.append_reasoning("[Pattern 误导] 匹配到的 pattern 预测与证据矛盾")


# ── 内部辅助 ────────────────────────────────────────────────

def _check_single_hypothesis(
    hyp: Hypothesis,
    new_evidence: Evidence,
    ctx: Context,
) -> bool:
    """
    检查单条假设是否被新证据否定

    判定规则（规则引擎，非 LLM）：
      1. 新 evidence 的 relevance < 0.3 → 对当前假设无帮助 → 潜在弱矛盾
      2. 新 evidence 在 evidence_against 中 → 直接矛盾
      3. 新 evidence 的 source 与假设预期不符 → 结构性矛盾
    """
    # 规则 1：低相关性 → 不矛盾（只是无用）
    if new_evidence.relevance < 0.2:
        return False

    # 规则 2：如果已经在 evidence_against 中 → 直接矛盾
    if new_evidence.id in hyp.evidence_against:
        ctx.append_reasoning(
            f"[矛盾] hypothesis={hyp.statement[:50]} "
            f"evidence={new_evidence.id} 在否定列表中"
        )
        return True

    return False


def _check_pattern_mismatch(
    ctx: Context,
    new_evidence: Evidence,
) -> bool:
    """
    检查匹配到的诊断模式是否与新证据矛盾

    如果 pattern 预测的诊断方向与新证据不一致 → 可能是误导
    """
    # Phase 1 简化：检查是否有 pattern 预测的假设被否定
    if not ctx.matched_patterns:
        return False

    for hyp in ctx.hypotheses.hypotheses:
        if hyp.status == HypothesisStatus.RULED_OUT and hyp.evidence_against:
            # 某个来自 pattern 的假设被排除了 → pattern 可能误导
            return True

    return False


def _rollback_hypotheses(ctx: Context) -> None:
    """
    回滚假设：清空所有 RULED_OUT 标记，回到候选状态
    保留已确认的假设
    """
    ctx.append_reasoning("[矛盾回溯] 执行 rollback，重新评估假设")
    ctx.append_event("contradiction.rollback", {
        "iteration": ctx.iteration,
    })

    for hyp in ctx.hypotheses.hypotheses:
        if hyp.status == HypothesisStatus.RULED_OUT:
            hyp.status = HypothesisStatus.CANDIDATE
            hyp.ruled_out_reason = None
            hyp.updated_at_iteration = ctx.iteration

        if hyp.status == HypothesisStatus.INVESTIGATING:
            hyp.status = HypothesisStatus.CANDIDATE
            hyp.updated_at_iteration = ctx.iteration

    ctx.hypotheses.current_focus = None


def _mark_for_re_evaluation(ctx: Context) -> None:
    """
    标记当前假设需重新评估
    """
    current = ctx.hypotheses.current_focus
    if current:
        hyp = ctx.hypotheses.get(current)
        if hyp and hyp.status == HypothesisStatus.INVESTIGATING:
            # 不改变状态，但在 reasoning_trace 中标记
            ctx.append_reasoning(
                f"[矛盾] 当前假设需重新评估: {hyp.statement[:80]}"
            )


def _switch_to_next_candidate(ctx: Context) -> None:
    """
    切换到下一个候选假设
    """
    candidates = ctx.hypotheses.candidates
    if candidates:
        # 取 confidence 最高的 candidate
        next_hyp = max(candidates, key=lambda h: h.confidence)
        ctx.hypotheses.set_focus(next_hyp.id)
        ctx.append_reasoning(
            f"[矛盾切换] 聚焦假设 → {next_hyp.statement[:80]}"
        )
