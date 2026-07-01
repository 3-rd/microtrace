"""Agent 主循环 (SPEC §4.2) — 双层结构：run_session 外层 + agent_iteration 内层

Phase 1 集成：
  - 证据锚定：_conclude() → validate_claim()
  - 逐跳 Gate：run_session() 外层 check_hop_gate()
  - 矛盾检测：agent_iteration() post-tool check_evidence_contradiction()
  - 鉴别诊断：HypothesisSet 替代 Judgment
"""
from __future__ import annotations
import asyncio
import json
import re
import time
import uuid
from datetime import datetime
from typing import Any

from microtrace.context.models import (
    Context,
    State,
    Problem,
    StackFrame,
    Hypothesis,
    HypothesisSet,
    HypothesisStatus,
    DiagnosisClaim,
    ConfidenceTier,
    JudgmentCategory,
    Evidence,
    EvidenceSource,
    EvidenceImportance,
    ContentType,
    ToolCall,
    ToolState,
    StreamEvent,
    StreamEventType,
    QuestionPrompt,
    CompactionRecord,
    GateResult,
)
from microtrace.context.prompt import _assemble_prompt, determine_content_type
from microtrace.context.compaction import compact, is_overflow, extract_microtrace_critical_lines
from microtrace.agent.state import StateHandler, transition
from microtrace.agent.doom_loop import (
    check_doom_loop,
    build_doom_loop_question,
    apply_doom_loop_answer,
)
from microtrace.agent.confidence import compute_confidence_tier, tier_to_action, is_ready_to_conclude
from microtrace.agent.hop_gate import check_hop_gate, get_gate_action
from microtrace.agent.contradiction import check_evidence_contradiction, apply_contradiction_result
from microtrace.tools import ToolRegistry
from microtrace.llm import LLMError


# ── Helpers ──────────────────────────────────────────────────────

def _summarize_args(args: dict) -> str:
    """工具参数摘要（Doom Loop / 显示用）"""
    items = [f"{k}={repr(v)[:30]}" for k, v in list(args.items())[:3]]
    return ", ".join(items)


def _summarize_output(output: str, max_len: int = 200) -> str:
    """工具输出摘要"""
    if not output:
        return ""
    if len(output) <= max_len:
        return output
    return output[:max_len] + "..."


def _parse_text_action(text: str) -> dict:
    """
    从 LLM 文本中解析 action 声明

    Phase 1 扩展支持：
      {@action: conclude, text: ...}
      {@action: ask_user, question: ...}
      {@hypothesis: {...}}
      {@focus_hypothesis: "id"}
      {@confirm: "id"}
      {@rule_out: {"id": "...", "reason": "..."}}
      {@diagnosis_claim: {...}}
    """
    if not text:
        return {}

    # 标准 action
    m = re.search(r"\{@action:\s*(\w+)(?:,\s*(\w+):\s*([^}]*))?\}", text)
    if m:
        action = m.group(1)
        if action == "conclude":
            return {"action": "conclude", "text": m.group(3) or text}
        elif action == "ask_user":
            return {"action": "ask_user", "question": m.group(3) or text}

    # Phase 1: hypothesis 相关 action
    for tag in ["@hypothesis", "@focus_hypothesis", "@confirm", "@rule_out", "@diagnosis_claim"]:
        pattern = rf"\{{{tag}:\s*([^}}]*)\}}"
        m = re.search(pattern, text)
        if m:
            return _parse_hypothesis_action(tag, m.group(1))

    return {}


def _parse_hypothesis_action(tag: str, content: str) -> dict:
    """解析 Phase 1 假设相关 action"""
    try:
        if tag == "@hypothesis":
            data = json.loads(content)
            return {"action": "hypothesis", "data": data}
        elif tag == "@focus_hypothesis":
            hyp_id = content.strip().strip('"').strip("'")
            return {"action": "focus_hypothesis", "id": hyp_id}
        elif tag == "@confirm":
            hyp_id = content.strip().strip('"').strip("'")
            return {"action": "confirm_hypothesis", "id": hyp_id}
        elif tag == "@rule_out":
            data = json.loads(content)
            return {"action": "rule_out_hypothesis", "id": data.get("id"), "reason": data.get("reason", "")}
        elif tag == "@diagnosis_claim":
            data = json.loads(content)
            return {"action": "diagnosis_claim", "data": data}
    except json.JSONDecodeError:
        pass
    return {}


def _generate_session_id() -> str:
    """生成 session ID（时间戳 + 短 hash）"""
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    short = uuid.uuid4().hex[:6]
    return f"{ts}-{short}"


# ── INTAKE ───────────────────────────────────────────────────────

async def _intake(ctx: Context, initial_input: str, llm) -> None:
    """
    INTAKE 态：解析原始输入
    - 空输入直接 EXIT（修复 3）
    - LLM 解析失败降级处理
    """
    ctx.append_reasoning("[INTAKE] 开始解析原始输入")
    ctx.append_event("state.intake.started", {})

    if not initial_input or not initial_input.strip():
        ctx.error = "Empty input"
        ctx.state = State.EXIT
        ctx.append_reasoning("[INTAKE] 空输入，直接 EXIT")
        ctx.append_event("state.exited", {"reason": "empty_input"})
        return

    try:
        parse_prompt = (
            "解析以下用户输入，提取问题信息：\n\n"
            f"{initial_input[:2000]}\n\n"
            "输出 JSON 格式（其他文字都不要）：\n"
            "{\n"
            '  "error_type": "错误类型描述（如 NullPointerException）",\n'
            '  "stack_frames": [\n'
            '    {"class_name": "com.foo.Bar", "method_name": "method", '
            '"file_name": "Bar.java", "line_number": 42}\n'
            "  ],\n"
            '  "log_snippets": ["日志片段..."]\n'
            "}\n\n"
            "如果没有堆栈或日志，相应字段返回空数组。"
        )

        response = await llm.complete(parse_prompt)
        m = re.search(r"\{.*\}", response, re.DOTALL)
        if m:
            data = json.loads(m.group())
            stack_frames = [
                StackFrame(
                    class_name=f.get("class_name", ""),
                    method_name=f.get("method_name", ""),
                    file_name=f.get("file_name", ""),
                    line_number=f.get("line_number", 0),
                )
                for f in data.get("stack_frames", [])
            ]
            ctx.problem = Problem(
                raw_input=initial_input,
                error_type=data.get("error_type"),
                stack_frames=stack_frames,
                log_snippets=data.get("log_snippets", []),
            )
            ctx.append_reasoning(
                f"[INTAKE] 解析成功: error_type={data.get('error_type')}, "
                f"frames={len(stack_frames)}"
            )
        else:
            ctx.problem = Problem(
                raw_input=initial_input,
                parse_error="JSON 解析失败",
            )
            ctx.append_reasoning("[INTAKE] JSON 解析失败，降级处理")
    except Exception as e:
        ctx.problem = Problem(
            raw_input=initial_input,
            parse_error=str(e),
        )
        ctx.error = f"INTAKE parse failed: {e}"
        ctx.state = State.EXIT
        ctx.append_reasoning(f"[INTAKE] 解析彻底失败: {e}，直接 EXIT")
        ctx.append_event("state.exited", {"reason": "parse_failed"})
        return

    ctx.append_event("state.intake.completed", {
        "error_type": ctx.problem.error_type,
    })


# ── 工具执行 ─────────────────────────────────────────────────────

async def _execute_one_tool(
    ctx: Context,
    tc: ToolCall,
    tools: ToolRegistry,
) -> ToolCall:
    """执行单个工具调用（生成 evidence）"""
    tc.state = ToolState.RUNNING
    ctx.add_tool_call(tc)
    ctx.append_reasoning(f"[工具执行] {tc.name} 开始")

    try:
        tool = tools.get(tc.name)
        if tool is None:
            raise ValueError(f"工具 {tc.name} 未注册")

        if tc.name in ctx.disabled_tools:
            raise PermissionError(f"工具 {tc.name} 已被禁用")

        result = await tool.execute(tc.args)
        tc.output_raw = result.content
        tc.output_summary = _summarize_output(result.content)
        tc.state = ToolState.COMPLETED
        ctx.append_reasoning(f"[工具完成] {tc.name} → {tc.output_summary[:80]}")

        ev = Evidence(
            source=EvidenceSource.TOOL_OUTPUT,
            location=f"tool:{tc.name}",
            content=tc.output_summary,
            raw_content=tc.output_raw or "",
            importance=EvidenceImportance.SUPPORTING,
            relevance=0.5,
            content_type=ContentType.COMPRESSIBLE,
            discovered_at_iteration=ctx.iteration,
            tool_name=tc.name,
        )
        ev.preserved_lines = extract_microtrace_critical_lines(tc.output_raw or "")
        ev.content_type = determine_content_type(ev, ctx)
        ctx.add_evidence(ev)

    except Exception as e:
        tc.state = ToolState.ERROR
        tc.error = str(e)
        tc.output_summary = f"ERROR: {e}"
        ctx.append_reasoning(f"[工具错误] {tc.name}: {e}")
        ev = Evidence(
            source=EvidenceSource.ERROR,
            location=f"tool:{tc.name}",
            content=f"Tool {tc.name} failed: {e}",
            raw_content="",
            importance=EvidenceImportance.BACKGROUND,
            content_type=ContentType.COMPRESSIBLE,
            discovered_at_iteration=ctx.iteration,
            tool_name=tc.name,
        )
        ctx.add_evidence(ev)

    return tc


async def _execute_tools_parallel(
    ctx: Context,
    tool_calls: list[ToolCall],
    tools: ToolRegistry,
) -> None:
    """并行执行多个工具调用（错误隔离）"""
    await asyncio.gather(
        *[_execute_one_tool(ctx, tc, tools) for tc in tool_calls],
        return_exceptions=True,
    )


# ── MAX_ITERATIONS 强制总结 ────────────────────────────────────

def _format_hypothesis_fallback(ctx: Context) -> str:
    """LLM 不可用时的 Hypothesis 兜底输出"""
    best = ctx.hypotheses.best
    if not best:
        return "Agent 未能形成结论（异常退出）"

    lines = [
        "## 尽力而为的判断",
        "",
        f"**类别**: {best.category.value if hasattr(best.category, 'value') else best.category}",
        f"**置信度**: {best.confidence:.2f}",
        f"**假设**: {best.statement}",
        "",
        f"## 已知证据（{len(ctx.evidence)} 条）",
    ]
    for i, ev in enumerate(ctx.evidence[:5], 1):
        lines.append(f"{i}. [{ev.source}] {ev.location}")
    lines.extend([
        "",
        "⚠️ *（MAX_ITERATIONS 强制总结时 LLM 不可用，本输出为兜底）*",
    ])
    return "\n".join(lines)


def _build_forced_summary_prompt(ctx: Context) -> str:
    """构建 MAX_ITERATIONS 强制总结 prompt"""
    problem_raw = ctx.problem.raw_input[:500] if ctx.problem else ""
    hypothesis_brief = ctx.hypotheses.to_brief() if ctx.hypotheses.hypotheses else "无假设"

    return (
        f"CRITICAL - MAXIMUM ITERATIONS REACHED\n\n"
        f"你已用 {ctx.iteration}/{ctx.max_iterations} 轮。工具调用已被禁用。"
        f"你必须用纯文本回答。\n\n"
        f"## 问题\n{problem_raw}\n\n"
        f"## 当前假设集\n{hypothesis_brief}\n\n"
        f"## 证据数：{len(ctx.evidence)} 条\n\n"
        f"## 最近推理\n" + "\n".join(f"- {s}" for s in ctx.reasoning_trace[-5:]) +
        "\n\n请按以下结构回答：\n"
        "1. 已调查的内容（引用证据）\n"
        "2. 当前最佳假设（A/B/C）和置信度\n"
        "3. 未能验证的剩余 gap\n"
        "4. 建议下一步调查方向"
    )


async def _force_max_iter_summary(ctx: Context, llm) -> None:
    """MAX_ITERATIONS 强制总结（LLM 失败用 hypothesis 兜底）"""
    ctx.append_reasoning("[MAX_ITERATIONS] 强制总结")
    forced_prompt = _build_forced_summary_prompt(ctx)
    try:
        text_parts: list[str] = []
        async for ev in llm.stream(forced_prompt, tools=[]):
            if ev.type == StreamEventType.TEXT_DELTA and ev.text:
                text_parts.append(ev.text)
        ctx.final_output = "".join(text_parts) or _format_hypothesis_fallback(ctx)
    except Exception as e:
        ctx.append_reasoning(f"[MAX_ITERATIONS] 强制总结 LLM 失败: {e}，用 hypothesis 兜底")
        ctx.final_output = _format_hypothesis_fallback(ctx)


# ── 证据锚定验证（机制 1）─────────────────────────────────────

def validate_claim(claim: DiagnosisClaim, ctx: Context) -> tuple[bool, str]:
    """
    硬验证 DiagnosisClaim（代码层，非 LLM 判断）

    验证规则：
      1. evidence_refs 不能为空
      2. 每条 evidence_ref 必须在 ctx.evidence 中存在
      3. 至少 1 条 critical evidence
      4. category 不能是 UNKNOWN

    Returns:
        (is_valid, error_message)
    """
    if not claim.evidence_refs:
        return False, "evidence_refs 为空（违反机制 1：证据锚定）"

    if len(claim.evidence_refs) < 2:
        return False, f"evidence_refs 不足: {len(claim.evidence_refs)}（最少需要 2 条）"

    # 验证每条 evidence_ref 都存在
    existing_ids = {ev.id for ev in ctx.evidence}
    for ref in claim.evidence_refs:
        if ref not in existing_ids:
            return False, f"evidence_ref '{ref}' 不存在于 ctx.evidence 中"

    # 至少 1 条 critical
    critical_evidence = {
        ev.id for ev in ctx.evidence
        if ev.content_type == ContentType.CRITICAL or ev.importance == EvidenceImportance.CRITICAL
    }
    has_critical = bool(set(claim.evidence_refs) & critical_evidence)
    if not has_critical:
        return False, "引用的 evidence 中没有 critical 级别"

    # category 不能是 UNKNOWN
    if claim.category == JudgmentCategory.UNKNOWN:
        return False, "category 为 UNKNOWN，拒绝输出"

    return True, ""


# ── CONCLUDE ────────────────────────────────────────────────────

async def _conclude(ctx: Context) -> str:
    """
    CONCLUDE 态：格式化输出 + 证据锚定验证（机制 1）

    流程：
      1. 如果有 LLM 产出的 final_output → 直接返回
      2. 如果有 DiagnosisClaim → validate_claim() → 通过后格式化
      3. 否则用 HypothesisSet 的 best hypothesis 兜底
    """
    if ctx.final_output:
        return ctx.final_output

    # 尝试从 diagnosis_claim 格式化
    claim = ctx.diagnosis_claim
    if claim:
        is_valid, error = validate_claim(claim, ctx)
        if not is_valid:
            ctx.append_reasoning(f"[validate_claim FAILED] {error}")
            ctx.append_event("claim.validation_failed", {"error": error})
            # 降级：用 best hypothesis
            return _format_hypothesis_conclusion(ctx)

        ctx.append_reasoning(f"[validate_claim PASSED] evidence_refs={len(claim.evidence_refs)}")
        return _format_claim_conclusion(claim, ctx)

    # 兜底：用 best hypothesis
    return _format_hypothesis_conclusion(ctx)


def _format_claim_conclusion(claim: DiagnosisClaim, ctx: Context) -> str:
    """格式化 DiagnosisClaim 为人类可读输出"""
    lines = [
        "# 问题诊断结论",
        "",
        f"**类别**: {claim.category.value if hasattr(claim.category, 'value') else claim.category}",
        f"**置信度分层**: {claim.confidence_tier.value if hasattr(claim.confidence_tier, 'value') else claim.confidence_tier}",
        f"**结论**: {claim.statement}",
        "",
        "## 证据链",
    ]
    for ev_id in claim.evidence_refs:
        ev = ctx.get_evidence_by_id(ev_id)
        if ev:
            lines.append(f"- [{ev.source}] {ev.location}: {ev.content[:100]}")
    return "\n".join(lines)


def _format_hypothesis_conclusion(ctx: Context) -> str:
    """用 HypothesisSet 的 best hypothesis 兜底格式化输出"""
    best = ctx.hypotheses.best
    if not best:
        return "Agent 未能形成结论"

    lines = [
        "# 问题诊断结论",
        "",
        f"**类别**: {best.category.value if hasattr(best.category, 'value') else best.category}",
        f"**置信度**: {best.confidence:.2f}",
        f"**状态**: {best.status.value if hasattr(best.status, 'value') else best.status}",
        f"**假设**: {best.statement}",
        "",
        "## 全部假设",
        ctx.hypotheses.to_brief(),
        "",
        "## 证据链",
    ]
    for ev_id in best.evidence_for:
        ev = ctx.get_evidence_by_id(ev_id)
        if ev:
            lines.append(f"- [{ev.source}] {ev.location}: {ev.content[:100]}")
    return "\n".join(lines)


# ── 持久化辅助 ─────────────────────────────────────────────────

async def _save_session(ctx: Context) -> None:
    from microtrace.persistence.sqlite import save_context_to_sqlite
    from microtrace.config import get_db_path
    try:
        save_context_to_sqlite(ctx, str(get_db_path()))
    except Exception as e:
        ctx.append_reasoning(f"[SAVE] 失败: {e}")


# ── agent_iteration（内层：单次 stream）───────────────────────

async def agent_iteration(
    ctx: Context,
    llm,
    tools: ToolRegistry,
) -> None:
    """
    内层 processor：单次 LLM stream 迭代

    Phase 1 新增：
      - 解析 hypothesis 相关 action（{@hypothesis, @focus, @confirm, @rule_out}）
      - Post-tool 矛盾检测（机制 5）
    """
    if check_doom_loop(ctx):
        ctx.pending_question = build_doom_loop_question(ctx)
        await transition(ctx, State.ASK_USER, reason="Doom Loop 触发")
        return

    try:
        prompt_text = _assemble_prompt(ctx, tools)
    except Exception as e:
        ctx.append_reasoning(f"[PROMPT ASSEMBLE ERROR] {e}")
        ctx.error = f"prompt assemble: {e}"
        return

    ctx.append_reasoning(
        f"[LLM 调用] iter={ctx.iteration}, prompt长度={len(prompt_text)}"
    )
    ctx.append_event("step.started", {"iteration": ctx.iteration})

    tool_calls_to_run: list[ToolCall] = []
    question_text: str | None = None
    conclusion_text: str | None = None
    text_buffer: list[str] = []
    new_evidence_ids: list[str] = []  # Phase 1: 本轮新增的 evidence ID

    try:
        async for event in llm.stream(prompt_text, tools=tools.schemas()):
            ctx.append_event("llm.event", {
                "type": str(event.type),
                "iteration": ctx.iteration,
            })

            if event.type == StreamEventType.TEXT_DELTA and event.text:
                text_buffer.append(event.text)
                parsed = _parse_text_action(event.text)

                if parsed.get("action") == "conclude" and not conclusion_text:
                    conclusion_text = parsed.get("text", "".join(text_buffer))
                elif parsed.get("action") == "ask_user" and not question_text:
                    question_text = parsed.get("question", "".join(text_buffer))

                # Phase 1: 处理 hypothesis 相关 action
                elif parsed.get("action") == "hypothesis":
                    _handle_hypothesis_action(ctx, parsed)
                elif parsed.get("action") == "focus_hypothesis":
                    _handle_focus_action(ctx, parsed)
                elif parsed.get("action") == "confirm_hypothesis":
                    _handle_confirm_action(ctx, parsed)
                elif parsed.get("action") == "rule_out_hypothesis":
                    _handle_rule_out_action(ctx, parsed)
                elif parsed.get("action") == "diagnosis_claim":
                    _handle_diagnosis_claim_action(ctx, parsed)

            elif event.type == StreamEventType.TOOL_CALL:
                tool_name = event.tool_name or "unknown"
                tool_args = event.tool_args or {}
                tool_calls_to_run.append(ToolCall(
                    name=tool_name,
                    args=tool_args,
                    args_summary=_summarize_args(tool_args),
                    output_summary="",
                    output_raw=None,
                    iteration=ctx.iteration,
                    state=ToolState.PENDING,
                ))
                ctx.append_reasoning(
                    f"[tool-call] {tool_name} args={_summarize_args(tool_args)}"
                )

            elif event.type == StreamEventType.ERROR:
                ctx.append_reasoning(f"[LLM ERROR] {event.error}")
                ctx.error = event.error
                ctx.append_event("step.failed", {"error": event.error})

    except Exception as e:
        ctx.append_reasoning(f"[LLM STREAM ERROR] {e}")
        ctx.error = str(e)
        ctx.append_event("step.failed", {"error": str(e)})
        return

    ctx.append_event("step.finished", {"iteration": ctx.iteration})

    # 记录本轮开始前的 evidence 数量
    ev_count_before = len(ctx.evidence)

    # 4. 后处理
    if tool_calls_to_run:
        if len(tool_calls_to_run) > 1:
            await _execute_tools_parallel(ctx, tool_calls_to_run, tools)
        else:
            await _execute_one_tool(ctx, tool_calls_to_run[0], tools)

        # Phase 1: 记录本轮新增的 evidence
        for ev in ctx.evidence[ev_count_before:]:
            new_evidence_ids.append(ev.id)

    elif question_text:
        ctx.pending_question = QuestionPrompt(
            header="Agent 提问",
            question=question_text,
            options=[],
            multiple=False,
            custom=True,
        )
        await transition(ctx, State.ASK_USER, reason="LLM ask_user")

    elif conclusion_text:
        ctx.final_output = conclusion_text
        ctx.append_reasoning(f"[LLM 自决结束] {conclusion_text[:100]}")

    # 5. Phase 1: Post-tool 矛盾检测（机制 5）
    for ev_id in new_evidence_ids:
        ev = ctx.get_evidence_by_id(ev_id)
        if ev:
            contradiction = check_evidence_contradiction(ctx, ev)
            if contradiction.found:
                apply_contradiction_result(ctx, contradiction)
                break  # 检测到矛盾后停止，避免级联

    # 6. Overflow 检查
    try:
        if is_overflow(ctx):
            await compact(ctx, llm)
    except Exception as e:
        ctx.append_reasoning(f"[OVERFLOW/COMPACTION ERROR] {e}")


# ── Phase 1: Hypothesis Action Handlers ─────────────────────────

def _handle_hypothesis_action(ctx: Context, parsed: dict) -> None:
    """处理 LLM 提出的新假设"""
    data = parsed.get("data", {})
    hyp = Hypothesis(
        statement=data.get("statement", ""),
        category=JudgmentCategory(data.get("category", "UNKNOWN")),
        confidence=float(data.get("confidence", 0.5)),
        status=HypothesisStatus.CANDIDATE,
        created_at_iteration=ctx.iteration,
        updated_at_iteration=ctx.iteration,
    )
    ctx.add_hypothesis(hyp)


def _handle_focus_action(ctx: Context, parsed: dict) -> None:
    """处理 LLM 聚焦某个假设"""
    hyp_id = parsed.get("id", "")
    if hyp_id:
        ctx.hypotheses.set_focus(hyp_id)
        ctx.append_reasoning(f"[聚焦假设] {hyp_id[:8]}")


def _handle_confirm_action(ctx: Context, parsed: dict) -> None:
    """处理 LLM 确认假设"""
    hyp_id = parsed.get("id", "")
    if hyp_id:
        # 将本轮 evidence 关联到该假设
        hyp = ctx.hypotheses.get(hyp_id)
        if hyp:
            recent_ev = [ev for ev in ctx.evidence if ev.discovered_at_iteration == ctx.iteration]
            for ev in recent_ev:
                hyp.add_supporting_evidence(ev.id)
            ctx.hypotheses.confirm(hyp_id)
            # 计算置信度分层
            tier = compute_confidence_tier(hyp, ctx.evidence)
            ctx.confidence_tier = tier
            ctx.append_reasoning(
                f"[假设确认] {hyp_id[:8]} tier={tier.value} "
                f"evidence_for={len(hyp.evidence_for)}"
            )


def _handle_rule_out_action(ctx: Context, parsed: dict) -> None:
    """处理 LLM 排除假设"""
    hyp_id = parsed.get("id", "")
    reason = parsed.get("reason", "")
    if hyp_id:
        # 将本轮 evidence 关联到否定列表
        hyp = ctx.hypotheses.get(hyp_id)
        if hyp:
            recent_ev = [ev for ev in ctx.evidence if ev.discovered_at_iteration == ctx.iteration]
            for ev in recent_ev:
                hyp.add_contradicting_evidence(ev.id)
        ctx.hypotheses.rule_out(hyp_id, reason)
        ctx.append_reasoning(f"[假设排除] {hyp_id[:8]}: {reason[:80]}")


def _handle_diagnosis_claim_action(ctx: Context, parsed: dict) -> None:
    """处理 LLM 输出 DiagnosisClaim"""
    data = parsed.get("data", {})
    claim = DiagnosisClaim(
        category=JudgmentCategory(data.get("category", "UNKNOWN")),
        statement=data.get("statement", ""),
        evidence_refs=data.get("evidence_refs", []),
        hypothesis_ref=data.get("hypothesis_ref"),
        confidence_tier=ConfidenceTier(data.get("confidence_tier", "suspected")),
        created_at_iteration=ctx.iteration,
    )
    ctx.set_diagnosis_claim(claim)


# ── run_session（外层：显式循环 + Gate）────────────────────────

async def run_session(
    initial_input: str,
    llm,
    tools: ToolRegistry,
    ctx: Context | None = None,
    session_id: str | None = None,
) -> Context:
    """
    外层 driver：显式循环，驱动整个 session

    Phase 1 新增（CLAUDE.md Q4 方案 B）：
      - 每轮后检查 Gate（机制 2）
      - Hop 跟踪
      - Pattern 匹配（机制 6，INTAKE→INVESTIGATE 之间）
    """
    # 初始化 Context
    if ctx is None:
        ctx = Context(
            session_id=session_id or _generate_session_id(),
            state=State.INTAKE,
            created_at=time.time(),
        )
    elif session_id and not ctx.session_id:
        ctx.session_id = session_id

    ctx.append_reasoning(f"[SESSION START] session_id={ctx.session_id}")

    # ── INTAKE 态 ──
    if ctx.state == State.ASK_USER and ctx.user_replies:
        ctx.append_reasoning("[ASK_USER RESUME] 检测到 user_replies，回到 INVESTIGATE")
        await transition(ctx, State.INVESTIGATE, reason="用户已回复")
    elif ctx.state == State.ASK_USER:
        await _save_session(ctx)
        ctx.append_reasoning("[ASK_USER] 继续等待用户回复")
        return ctx

    await StateHandler.enter(ctx, from_state=None)
    await _intake(ctx, initial_input, llm)
    if ctx.state == State.EXIT:
        await _save_session(ctx)
        ctx.append_reasoning("[SESSION END] (after INTAKE EXIT)")
        return ctx

    # ── Phase 1: Pattern 匹配（机制 6，INTAKE→INVESTIGATE 之间）──
    _match_patterns(ctx)

    # ── INVESTIGATE 态：主循环 ──
    ctx.increment_hop()  # Hop 1
    await transition(ctx, State.INVESTIGATE, reason="INTAKE 完成")

    while True:
        ctx.iteration += 1
        ctx.append_reasoning(f"[开始第 {ctx.iteration} 轮, Hop {ctx.current_hop}]")

        # 退出条件 1：max_iterations
        if ctx.iteration > ctx.max_iterations:
            ctx.append_reasoning("[MAX_ITERATIONS] 到达，强制总结")
            await _force_max_iter_summary(ctx, llm)
            await transition(ctx, State.CONCLUDE, reason="max_iterations 到达")
            break

        # 退出条件 2：用户中断
        if ctx.user_interrupt:
            ctx.append_reasoning("[USER INTERRUPT]")
            await transition(ctx, State.CONCLUDE, reason="用户中断")
            break

        # 退出条件 3：LLM 自决结束
        if ctx.final_output:
            ctx.append_reasoning(f"[LLM 自决结束] {ctx.final_output[:80]}...")
            await transition(ctx, State.CONCLUDE, reason="LLM 自决")
            break

        # ── 单次迭代 ──
        await agent_iteration(ctx, llm, tools)

        # ── 状态检查 ──
        if ctx.state == State.ASK_USER:
            await _save_session(ctx)
            ctx.append_reasoning("[ASK_USER] 等用户回复")
            return ctx

        if ctx.state == State.EXIT:
            break

        if ctx.state == State.CONCLUDE:
            break

        # ── Phase 1: Gate 检查（机制 2，方案 B：外层）──
        gate_result = check_hop_gate(ctx)
        gate_action = get_gate_action(gate_result)

        if gate_result == GateResult.PASS:
            ctx.increment_hop()
            # 检查是否可以直接 conclude
            if is_ready_to_conclude(ctx.confidence_tier):
                ctx.append_reasoning(f"[GATE PASS + CERTAIN] 证据充分，提前结束")
                await transition(ctx, State.CONCLUDE, reason="Gate PASS + tier=CERTAIN")
                break

        elif gate_result == GateResult.HOLD:
            # 继续当前 hop，收集更多证据
            ctx.append_reasoning("[GATE HOLD] 证据不足，继续当前 hop")

        elif gate_result == GateResult.BACKTRACK:
            # 回滚 hop
            ctx.append_reasoning("[GATE BACKTRACK] 回滚到上一 hop")
            if ctx.current_hop > 1:
                ctx.current_hop -= 1

        elif gate_result == GateResult.FAIL:
            ctx.append_reasoning("[GATE FAIL] 致命矛盾，标记失败")
            ctx.error = "Gate FAIL: 致命矛盾"
            await transition(ctx, State.CONCLUDE, reason="Gate FAIL")
            break

        # ── 每轮保存 ──
        await _save_session(ctx)

    # ── CONCLUDE 态 ──
    if not ctx.final_output:
        ctx.final_output = await _conclude(ctx)
    await _save_session(ctx)

    # Phase 1: 成功完成后提取 pattern（机制 6）
    _extract_pattern_on_success(ctx)

    ctx.append_reasoning("[SESSION END]")
    return ctx


# ── Phase 1: Pattern 辅助函数（机制 6）─────────────────────────

def _match_patterns(ctx: Context) -> None:
    """INTAKE→INVESTIGATE 之间匹配诊断模式"""
    try:
        from microtrace.agent.pattern_store import PatternStore
        from microtrace.config import get_data_dir
        store = PatternStore(file_path=str(get_data_dir() / "patterns.json"))
        store.match_and_inject(ctx)
    except Exception as e:
        ctx.append_reasoning(f"[Pattern 匹配] 失败: {e}")


def _extract_pattern_on_success(ctx: Context) -> None:
    """成功完成后提取诊断模式"""
    try:
        if not ctx.hypotheses.best:
            return
        from microtrace.agent.pattern_store import PatternStore
        from microtrace.config import get_data_dir
        store = PatternStore(file_path=str(get_data_dir() / "patterns.json"))
        pattern = store.extract_from_session(ctx)
        if pattern:
            store.save()
            ctx.append_reasoning(f"[Pattern 提取] pattern_id={pattern.id[:8]}")
    except Exception as e:
        ctx.append_reasoning(f"[Pattern 提取] 失败: {e}")
