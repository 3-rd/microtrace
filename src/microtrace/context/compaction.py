"""Compaction 完整流程（SPEC §4.9, Q8 拆分：OpenCode 通用 + microtrace 独有）"""
from __future__ import annotations
import json
import re
from datetime import datetime
from microtrace.context.models import (
    Context,
    Evidence,
    CompactionRecord,
    ContentType,
    ToolState,
)
from microtrace.context.prompt import determine_content_type
from microtrace.config import get_compaction_buffer


# ── OpenCode 通用参数 ─────────────────────────────────────────
COMPACTION_BUFFER: int = 20_000  # 与 OpenCode 一致
DEFAULT_TAIL_TURNS: int = 2
TOOL_OUTPUT_MAX_CHARS: int = 2000
PRUNE_PROTECTED_TOOLS: list[str] = ["skill"]


# ── microtrace 关键行提取（业务定制）────────────────────────
MICROTRACE_PRESERVE_PATTERNS: list[str] = [
    r"Exception in thread",
    r"\b\w+Exception\b",                      # NullPointerException 等
    r"java\.lang\.\w+",                        # 完整 java.lang.XXX
    r"at\s+[\w\.]+\([\w\.]+\.java:\d+\)",
    r"Caused by:",
    r"error\s*code[:=]?\s*\d{3,4}",
    r"returned\s+status\s+\d{3}",
    r"HTTP/\d\.\d\s+\d{3}",
    r"@Transactional",
    r"@Async",
    r"@Scheduled",
    r"@FeignClient",
    r"@DubboReference",
    r"\b(ERROR|FATAL)\b",
]


SUMMARY_TEMPLATE = """## Goal
- [single-sentence task summary]

## Constraints & Preferences
- [user constraints, preferences, specs, or "(none)"]

## Progress
### Done
- [completed work or "(none)"]
### In Progress
- [current work or "(none)"]
### Blocked
- [blockers or "(none)"]

## Key Decisions
- [decision and why, or "(none)"]

## Next Steps
- [ordered next actions or "(none)"]

## Critical Context
- [important technical facts, errors, or "(none)"]

## Relevant Files
- [file or directory path: why it matters, or "(none)"]

Rules:
- Keep every section, even when empty.
- Use terse bullets, not prose paragraphs.
- Preserve exact file paths, commands, error strings, and identifiers when known.
- Do not mention the summary process or that context was compacted.
"""


# ── microtrace 关键行提取 ──────────────────────────────────────

def extract_microtrace_critical_lines(tool_output: str, max_lines: int = 20) -> str:
    """从 tool output 提取 microtrace 关键行（不 summarization）"""
    if not tool_output:
        return ""
    lines = tool_output.split("\n")
    critical: list[str] = []
    for line in lines:
        for pattern in MICROTRACE_PRESERVE_PATTERNS:
            if re.search(pattern, line):
                critical.append(line.strip())
                break
    return "\n".join(critical[:max_lines])


# ── OpenCode 通用 PRUNE ──────────────────────────────────────

def _prune_old_tool_outputs(ctx: Context) -> int:
    """
    OpenCode 通用 PRUNE：跳过最近 DEFAULT_TAIL_TURNS 轮 + 跳过 PRUNE_PROTECTED_TOOLS
    Phase 0 简化：按 tool call 数量算 tail，复杂 turn 场景 Phase 1+ 优化
    """
    pruned = 0
    tool_calls = [tc for tc in ctx.tool_history if tc.state == ToolState.COMPLETED]

    if len(tool_calls) <= DEFAULT_TAIL_TURNS:
        return 0

    for tc in tool_calls[:-DEFAULT_TAIL_TURNS]:
        if tc.name in PRUNE_PROTECTED_TOOLS:
            continue
        # 找对应 evidence 并 PRUNE（一个 tool call 配对一个 evidence）
        for ev in ctx.evidence:
            if ev.tool_name == tc.name and not ev.compacted:
                ev.compacted = True
                ev.content = ev.content[:TOOL_OUTPUT_MAX_CHARS]
                pruned += 1
                break
    return pruned


# ── Overflow 检测 ────────────────────────────────────────────

def is_overflow(ctx: Context, context_window: int = 128_000) -> bool:
    """
    检测 context 是否溢出
    触发阈值：estimated_tokens >= context_window - COMPACTION_BUFFER
    """
    buffer = get_compaction_buffer()
    estimated = ctx.cumulative_tokens + _estimate_prompt_size(ctx)
    usable = context_window - buffer
    return estimated >= usable


def _estimate_prompt_size(ctx: Context) -> int:
    """估算当前 prompt 大小（字符数 * 0.25 近似 token）"""
    try:
        size = len(ctx.model_dump_json(exclude_none=True))
    except Exception:
        size = 0
    return int(size * 0.25)


# ── SUMMARY 生成（调 LLM）───────────────────────────────────

async def _summarize(
    ctx: Context,
    llm,  # LLMClient
    previous_summary: str | None = None,
) -> str:
    """调 LLM 生成摘要（8-section anchored）"""
    candidate_evidence = [
        ev for ev in ctx.evidence
        if ev.content_type != ContentType.CRITICAL and not ev.compacted
    ]
    evidence_text = "\n".join(
        f"- [{ev.source}] {ev.location}: {ev.content[:200]}"
        for ev in candidate_evidence[-10:]
    )

    if previous_summary:
        prompt = (
            "Update the anchored summary below using the new evidence.\n"
            "Preserve still-true details, remove stale details, and merge in the new facts.\n\n"
            f"<previous-summary>\n{previous_summary}\n</previous-summary>\n\n"
            f"New evidence:\n{evidence_text}\n\n"
            + SUMMARY_TEMPLATE
        )
    else:
        prompt = (
            "Create a new anchored summary from the evidence.\n\n"
            f"Evidence:\n{evidence_text}\n\n"
            + SUMMARY_TEMPLATE
        )

    response = await llm.complete(prompt)
    return response.strip()


def _truncated_fallback_summary(ctx: Context) -> str:
    """LLM 失败时简单截取 evidence 标题"""
    lines = ["## 压缩摘要 (truncated fallback)"]
    for ev in ctx.evidence[-10:]:
        if not ev.compacted:
            lines.append(f"- [{ev.source}] {ev.location}: {ev.content[:50]}...")
    return "\n".join(lines)


# ── Compaction 完整流程（microtrace 流程图）────────────────────

async def compact(ctx: Context, llm) -> None:
    """
    Compaction 完整流程
    1. microtrace 独有：关键行提取
    2. OpenCode 通用：PRUNE
    3. OpenCode 通用：SUMMARY（8-section anchored，失败有 fallback）
    4. microtrace 独有：标记 critical evidence
    5. 记录 CompactionRecord
    """
    import time
    ctx.append_reasoning("[COMPACTION] 触发")
    ctx.append_event("compaction.started", {
        "reason": "auto_overflow",
        "iteration": ctx.iteration,
    })

    # 1. microtrace 独有：关键行提取
    for ev in ctx.evidence:
        if ev.source in ("log", "code", "tool_output", "stack"):
            ev.preserved_lines = extract_microtrace_critical_lines(ev.raw_content or "")

    # 2. OpenCode 通用：PRUNE
    pruned_count = _prune_old_tool_outputs(ctx)

    # 3. OpenCode 通用：SUMMARY
    previous_summary = ctx.compactions[-1].summary if ctx.compactions else None
    try:
        new_summary = await _summarize(ctx, llm, previous_summary)
    except Exception as e:
        ctx.append_reasoning(f"[COMPACTION] SUMMARY 失败: {e}，用 truncated fallback")
        new_summary = _truncated_fallback_summary(ctx)

    # 4. microtrace 独有：标记 critical evidence
    for ev in ctx.evidence:
        ev.content_type = determine_content_type(ev, ctx)

    # 5. 记录 CompactionRecord
    critical_ids = [ev.id for ev in ctx.evidence if ev.content_type == ContentType.CRITICAL]
    record = CompactionRecord(
        triggered_at_iteration=ctx.iteration,
        reason="auto_overflow",
        tokens_before=ctx.cumulative_tokens,
        tokens_after=int(len(new_summary) * 1.3),
        summary=new_summary,
        preserved_evidence_ids=critical_ids,
        pruned_count=pruned_count,
        timestamp=time.time(),
    )
    ctx.compactions.append(record)

    # 6. 精简 reasoning_trace
    ctx.reasoning_trace = [
        f"[COMPACTION] 已压缩 {pruned_count} 条 tool call",
        f"[COMPACTION] Summary: {new_summary[:200]}",
    ] + ctx.reasoning_trace[-3:]

    ctx.append_reasoning(
        f"[COMPACTION] 完成，pruned={pruned_count}, critical={len(critical_ids)}"
    )
    ctx.append_event("compaction.ended", {
        "pruned": pruned_count,
        "critical": len(critical_ids),
    })
