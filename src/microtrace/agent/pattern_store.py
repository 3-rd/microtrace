"""诊断模式存储 + 匹配 + 进化 + 降级（机制 6）

核心原则：跨 session 学习，症状匹配→注入 hint，三态生命周期。

SPEC §1.4 机制 6:
  - 存储：同 SQLite，新表 patterns（CLAUDE.md Q6 方案 A）
  - 匹配：symptom_signature 相似度（含 embedding，Phase 1 用规则匹配）
  - 进化：active → stale → archived
  - 注入：匹配到的 pattern 的 diagnosis_template 作为 hint 注入 prompt

当前简化实现：
  - 内存存储（Step 5 迁到 SQLite）
  - 规则匹配（error_type + stack_top_class）
  - 三态生命周期自动管理
"""
from __future__ import annotations

import time
import json
from pathlib import Path
from microtrace.context.models import (
    Context,
    DiagnosisPattern,
    PatternStatus,
    Problem,
    JudgmentCategory,
)


# ── 时间常量 ────────────────────────────────────────────────

STALE_THRESHOLD_SECONDS: float = 7 * 24 * 3600  # 7 天无人匹配 → stale
ARCHIVE_THRESHOLD_SECONDS: float = 30 * 24 * 3600  # stale 后再 30 天 → archived
MIN_ACCURACY_TO_KEEP: float = 0.5  # accuracy < 0.5 → 不注入 hint（但仍保留）


# ── PatternStore ────────────────────────────────────────────

class PatternStore:
    """
    诊断模式存储

    Phase 1 简化实现：内存 dict + JSON 文件持久化
    Step 5 迁到 SQLite 的 patterns 表
    """

    def __init__(self, file_path: str | None = None):
        self._patterns: dict[str, DiagnosisPattern] = {}
        self._file_path = file_path
        if file_path and Path(file_path).exists():
            self._load()

    # ── CRUD ─────────────────────────────────────────

    def add(self, pattern: DiagnosisPattern) -> None:
        self._patterns[pattern.id] = pattern

    def get(self, pattern_id: str) -> DiagnosisPattern | None:
        return self._patterns.get(pattern_id)

    def list_active(self) -> list[DiagnosisPattern]:
        """返回所有 active 模式"""
        return [
            p for p in self._patterns.values()
            if p.status == PatternStatus.ACTIVE
        ]

    def __len__(self) -> int:
        return len(self._patterns)

    # ── Pattern 提取 ────────────────────────────────

    def extract_from_session(self, ctx: Context) -> DiagnosisPattern | None:
        """
        从成功完成的 session 中提取诊断模式

        条件：
          1. 有 confirmed 假设
          2. 有足够的 evidence 支持
          3. 有明确的归属判断（非 UNKNOWN）
        """
        best = ctx.hypotheses.best
        if not best:
            return None

        if best.category == JudgmentCategory.UNKNOWN:
            return None

        if len(best.evidence_for) < 2:
            return None

        # 生成 symptom_signature（简化：LLM 摘要 + 结构化字段）
        symptom = _build_symptom_signature(ctx)

        # 生成 diagnosis_template
        template = _build_diagnosis_template(ctx, best)

        pattern = DiagnosisPattern(
            symptom_signature=symptom,
            error_type=ctx.problem.error_type if ctx.problem else None,
            stack_top_class=_extract_stack_top(ctx),
            diagnosis_template=template,
            category=best.category,
            status=PatternStatus.ACTIVE,
            success_count=1,
            failure_count=0,
            created_at=time.time(),
            last_matched_at=time.time(),
            source_session_id=ctx.session_id,
        )
        self.add(pattern)
        return pattern

    # ── Pattern 匹配 ────────────────────────────────

    def match(self, problem: Problem) -> list[DiagnosisPattern]:
        """
        按症状匹配诊断模式

        Phase 1 简化：结构化字段规则匹配（error_type + stack_top_class）
        Phase 2+ ：embedding 相似度匹配

        只返回 active 且 accuracy >= MIN_ACCURACY_TO_KEEP 的模式
        """
        matches: list[DiagnosisPattern] = []

        for pattern in self.list_active():
            if pattern.accuracy < MIN_ACCURACY_TO_KEEP:
                continue

            score = _compute_match_score(problem, pattern)
            if score >= 0.5:  # 匹配阈值
                matches.append(pattern)

        # 按 match score 降序排列
        matches.sort(
            key=lambda p: _compute_match_score(problem, p),
            reverse=True,
        )
        return matches[:3]  # 最多返回 3 个

    def match_and_inject(self, ctx: Context) -> list[str]:
        """
        匹配 pattern 并注入到 ctx.matched_patterns

        Returns:
            匹配到的 pattern ID 列表
        """
        if not ctx.problem:
            return []

        matched = self.match(ctx.problem)
        pattern_ids = [p.id for p in matched]
        ctx.matched_patterns = pattern_ids

        if matched:
            ctx.append_reasoning(
                f"[Pattern 匹配] {len(matched)} 个模式匹配，"
                f"accuracy 范围 {min(p.accuracy for p in matched):.2f}-{max(p.accuracy for p in matched):.2f}"
            )

        return pattern_ids

    def get_hints(self, ctx: Context) -> str:
        """
        生成 hint 文本（注入到 prompt 中）

        将匹配到的 patterns 的 diagnosis_template 格式化为提示
        """
        if not ctx.matched_patterns:
            return ""

        hints = ["## 历史诊断模式提示（仅供参考，需独立验证）"]
        for pid in ctx.matched_patterns:
            pattern = self.get(pid)
            if not pattern:
                continue
            hints.append(
                f"\n### 模式 #{pid[:8]}\n"
                f"- 症状: {pattern.symptom_signature[:200]}\n"
                f"- 诊断模板: {pattern.diagnosis_template[:500]}\n"
                f"- 历史准确率: {pattern.accuracy:.0%} ({pattern.success_count}成功/{pattern.failure_count}失败)\n"
                f"- 最终归属: {pattern.category.value}"
            )

        return "\n".join(hints)

    # ── Pattern 进化 ────────────────────────────────

    def record_success(self, pattern_id: str) -> None:
        """匹配成功 → 增加成功计数 + 保持 active"""
        pattern = self.get(pattern_id)
        if pattern:
            pattern.record_success()
            pattern.last_matched_at = time.time()

    def record_failure(self, pattern_id: str) -> None:
        """匹配失败（pattern 预测错误）→ 增加失败计数"""
        pattern = self.get(pattern_id)
        if pattern:
            pattern.record_failure()
            pattern.last_matched_at = time.time()

    def decay_patterns(self) -> int:
        """
        定期执行：过期降级

        - active + 超过 STALE_THRESHOLD 未匹配 → stale
        - stale + 超过 ARCHIVE_THRESHOLD → archived
        - accuracy < MIN_ACCURACY_TO_KEEP + active → 不注入但保留

        Returns:
            降级数量
        """
        now = time.time()
        decayed = 0

        for pattern in self._patterns.values():
            if pattern.last_matched_at is None:
                continue

            elapsed = now - pattern.last_matched_at

            if pattern.status == PatternStatus.ACTIVE and elapsed > STALE_THRESHOLD_SECONDS:
                pattern.mark_stale()
                decayed += 1

            elif pattern.status == PatternStatus.STALE and elapsed > ARCHIVE_THRESHOLD_SECONDS:
                pattern.archive()
                decayed += 1

        return decayed

    # ── 持久化 ──────────────────────────────────────

    def save(self) -> None:
        """保存到 JSON 文件（Step 5 迁到 SQLite）"""
        if not self._file_path:
            return
        data = {
            pid: p.model_dump(mode="json")
            for pid, p in self._patterns.items()
        }
        Path(self._file_path).parent.mkdir(parents=True, exist_ok=True)
        with open(self._file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _load(self) -> None:
        """从 JSON 文件加载"""
        if not self._file_path:
            return
        try:
            with open(self._file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for pid, raw in data.items():
                self._patterns[pid] = DiagnosisPattern.model_validate(raw)
        except (json.JSONDecodeError, OSError):
            pass

    def get_stats(self) -> dict:
        """返回统计信息"""
        active = sum(1 for p in self._patterns.values() if p.status == PatternStatus.ACTIVE)
        stale = sum(1 for p in self._patterns.values() if p.status == PatternStatus.STALE)
        archived = sum(1 for p in self._patterns.values() if p.status == PatternStatus.ARCHIVED)
        return {
            "total": len(self._patterns),
            "active": active,
            "stale": stale,
            "archived": archived,
        }


# ── 辅助函数 ────────────────────────────────────────────────

def _build_symptom_signature(ctx: Context) -> str:
    """从 session 生成症状签名（结构化 + LLM 摘要混合）"""
    problem = ctx.problem
    parts = []

    if problem and problem.error_type:
        parts.append(f"ErrorType: {problem.error_type}")

    stack_top = _extract_stack_top(ctx)
    if stack_top:
        parts.append(f"StackTop: {stack_top}")

    # 使用最终判断作为症状描述
    best = ctx.hypotheses.best
    if best:
        parts.append(f"Conclusion: {best.category.value} {best.statement[:200]}")

    # evidence 数量特征
    parts.append(f"EvidenceCount: {len(ctx.evidence)}")

    return " | ".join(parts)


def _build_diagnosis_template(ctx: Context, hypothesis) -> str:
    """
    生成诊断模板（注入 hint 用）

    包含：
      - 诊断路径（用了哪些工具/按什么顺序）
      - 关键发现
      - 最终结论
    """
    lines = [
        f"归属: {hypothesis.category.value}",
        f"结论: {hypothesis.statement}",
    ]

    # 记录调查路径（工具调用序列）
    tool_names = list(set(tc.name for tc in ctx.tool_history))
    if tool_names:
        lines.append(f"调查工具: {', '.join(tool_names)}")

    # 关键 evidence
    for ev_id in hypothesis.evidence_for[:3]:
        ev = ctx.get_evidence_by_id(ev_id)
        if ev:
            lines.append(f"关键证据: [{ev.source}] {ev.location}")

    return "\n".join(lines)


def _extract_stack_top(ctx: Context) -> str | None:
    """提取栈顶类名（用于结构化匹配）"""
    if ctx.problem and ctx.problem.stack_frames:
        return ctx.problem.stack_frames[0].class_name
    return None


def _compute_match_score(
    problem: Problem,
    pattern: DiagnosisPattern,
) -> float:
    """
    计算匹配分数（Phase 1 简化：规则匹配）

    算法：
      - error_type 精确匹配: +0.4
      - stack_top_class 精确匹配: +0.3
      - error_type 部分匹配（子串）: +0.2
      - symptom_signature 关键词匹配: +0.1
    """
    score = 0.0

    # error_type 匹配
    if problem.error_type and pattern.error_type:
        if problem.error_type == pattern.error_type:
            score += 0.4
        elif problem.error_type in pattern.error_type or pattern.error_type in problem.error_type:
            score += 0.2

    # stack_top_class 匹配
    problem_top = None
    if problem.stack_frames:
        problem_top = problem.stack_frames[0].class_name

    if problem_top and pattern.stack_top_class:
        if problem_top == pattern.stack_top_class:
            score += 0.3

    # symptom_signature 关键词匹配
    if problem.error_type and pattern.symptom_signature:
        # 检查 symptom_signature 中是否包含 error_type
        if problem.error_type.lower() in pattern.symptom_signature.lower():
            score += 0.1

    return min(score, 1.0)
