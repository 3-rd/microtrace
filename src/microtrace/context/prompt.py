"""Prompt 装配（SPEC §4.3, §4.9.3）"""
from __future__ import annotations
import json
from microtrace.context.models import (
    Context,
    Evidence,
    EvidenceImportance,
    UserReply,
    ContentType,
)
from microtrace.tools import ToolRegistry


# 永不压缩区（SPEC §4.3.2）
NEVER_COMPACT_KEYS = {"problem", "current_judgment", "pending_question"}

# importance 排序：critical > supporting > background
IMPORTANCE_ORDER = [
    EvidenceImportance.CRITICAL,
    EvidenceImportance.SUPPORTING,
    EvidenceImportance.BACKGROUND,
]


def sort_evidence(evidence: list[Evidence]) -> list[Evidence]:
    """按 importance + relevance 排序"""
    return sorted(
        evidence,
        key=lambda e: (
            -IMPORTANCE_ORDER.index(EvidenceImportance(e.importance))
            if isinstance(e.importance, str)
            else -IMPORTANCE_ORDER.index(e.importance),
            -float(e.relevance),
        ),
    )


def _format_problem(ctx: Context) -> str:
    problem = ctx.problem
    if not problem:
        return "## 问题\n（尚未解析）"
    parts = [f"## 问题\n\n{problem.raw_input[:2000]}"]
    if problem.error_type:
        parts.append(f"**错误类型**: {problem.error_type}")
    if problem.stack_frames:
        parts.append(
            "**堆栈帧**:\n"
            + "\n".join(f"- {sf.to_short_string()}" for sf in problem.stack_frames[:5])
        )
    if problem.log_snippets:
        parts.append(
            "**日志片段**:\n"
            + "\n".join(f"- {s[:200]}" for s in problem.log_snippets[:3])
        )
    return "\n".join(parts)


def _format_judgment(judgment) -> str:
    return (
        f"## 当前判断\n\n"
        f"**类别**: {judgment.category}\n"
        f"**置信度**: {judgment.confidence:.2f}\n"
        f"**理由**: {judgment.one_line_reason}\n"
        f"**推理**: {judgment.reasoning}"
    )


def _format_evidence_pool(
    evidence: list[Evidence],
    max_items: int = 5,
    max_content_len: int = 500,
) -> str:
    """按 importance+relevance 排序，截取前 max_items 条"""
    sorted_ev = sort_evidence(evidence)
    selected = sorted_ev[:max_items]

    lines = ["## 证据池"]
    if not selected:
        lines.append("（暂无证据）")
    for ev in selected:
        content = ev.content[:max_content_len]
        lines.append(f"\n### [{ev.source}] {ev.location}")
        lines.append(
            f"relevance={ev.relevance:.2f}, importance={ev.importance}"
        )
        lines.append(content)
        if ev.preserved_lines:
            lines.append(f"**关键行**: {ev.preserved_lines[:200]}")

    skipped = len(sorted_ev) - len(selected)
    if skipped > 0:
        lines.append(f"\n_（还有 {skipped} 条 evidence 已截取）_")
    return "\n".join(lines)


def _format_compactions(compactions) -> str:
    lines = []
    for c in compactions:
        lines.append(f"### Compaction @ iter {c.triggered_at_iteration}")
        lines.append(c.summary[:500])
    return "\n".join(lines) if lines else ""


def _format_reasoning_trace(trace: list[str], max_steps: int = 3) -> str:
    recent = trace[-max_steps:] if trace else []
    if not recent:
        return "## 推理轨迹\n（暂无）"
    return "## 推理轨迹（最近）\n" + "\n".join(f"- {s}" for s in recent)


def _format_user_replies(replies: list[UserReply]) -> str:
    lines = ["## 用户回复"]
    for r in replies[-2:]:  # 最近 2 轮（与 OpenCode DEFAULT_TAIL_TURNS 对齐）
        lines.append(f"**Q**: {r.question}")
        lines.append(f"**A**: {r.answer}")
    return "\n".join(lines)


def _format_disabled_tools(disabled: set[str]) -> str:
    if not disabled:
        return ""
    items = "\n".join(f"- `{t}`" for t in disabled)
    return f"## ⚠️ 已禁用工具（请不要调）\n{items}"


def _format_tools(tools: ToolRegistry) -> str:
    lines = ["## 可用工具"]
    for tool in tools.list_tools():
        t = tools.get(tool)
        if t is None:
            continue
        lines.append(f"\n### {t.name}")
        lines.append(t.description)
        lines.append(f"```json\n{json.dumps(t.schema.get('parameters', {}), indent=2, ensure_ascii=False)}\n```")
    return "\n".join(lines)


def _build_instruction(ctx: Context) -> str:
    return (
        f"## 指令\n\n"
        f"- 当前处于第 {ctx.iteration} 轮（共最多 {ctx.max_iterations} 轮）\n"
        f"- 每条结论必须引用证据（证据编号或文件:行号）\n"
        f"- 证据不足时，明确说\"我无法判断，需要 X 信息\"\n"
        f"- 使用工具获取事实，不要臆测\n"
        f"- 输出格式：用 `{{@action: conclude, text: ...}}` 表示输出结论，"
        f"`{{@action: ask_user, question: ...}}` 表示询问用户"
    )


def _load_system_prompt() -> str:
    """加载 master prompt（agent.md 全文）"""
    from microtrace.prompts import load_agent_prompt
    return load_agent_prompt() or "(no master prompt configured)"


def determine_content_type(ev: Evidence, ctx: Context) -> ContentType:
    """
    5 条结构规则：自动判定 evidence 的 content_type (SPEC §4.9.3)
    """
    # 规则 1：堆栈帧里的关键 class
    if ev.source == "stack":
        return ContentType.CRITICAL

    # 规则 2：根因代码位置（含 @ 标记）
    if ev.source == "code" and "@" in ev.content:
        return ContentType.CRITICAL

    # 规则 3：日志里 NPE 抛出点（"at X.java:line"）
    if ev.source == "log" and "at " in ev.content and ".java:" in ev.content:
        return ContentType.CRITICAL

    # 规则 4：早期 evidence（决定方向，iter <= max_iterations/2）
    if ev.discovered_at_iteration <= ctx.max_iterations // 2:
        return ContentType.CRITICAL

    # 规则 5：LLM 评 critical
    if ev.importance == EvidenceImportance.CRITICAL:
        return ContentType.CRITICAL

    return ContentType.COMPRESSIBLE


def _assemble_prompt(ctx: Context, tools: ToolRegistry) -> str:
    """从 Context 组装 LLM prompt（8-section 结构）"""
    sections: list[str] = []

    # 1. System Prompt（全量，不压缩）
    sections.append(_load_system_prompt())

    # 2. Problem（永不压缩）
    sections.append(_format_problem(ctx))

    # 3. Judgment（永不压缩）
    sections.append(_format_judgment(ctx.current_judgment))

    # 4. Evidence Pool + Compaction Summary
    evidence_text = _format_evidence_pool(ctx.evidence, max_items=5, max_content_len=500)
    if ctx.compactions:
        evidence_text += "\n\n## 历史压缩摘要\n"
        evidence_text += _format_compactions(ctx.compactions[-2:])
    sections.append(evidence_text)

    # 5. Reasoning Trace
    sections.append(_format_reasoning_trace(ctx.reasoning_trace, max_steps=3))

    # 6. User Replies
    if ctx.user_replies:
        sections.append(_format_user_replies(ctx.user_replies))

    # 7. Disabled Tools（审计修复 2）
    disabled_section = _format_disabled_tools(ctx.disabled_tools)
    if disabled_section:
        sections.append(disabled_section)

    # 8. Available Tools
    sections.append(_format_tools(tools))

    # 9. Instruction
    sections.append(_build_instruction(ctx))

    return "\n\n".join(sections)
