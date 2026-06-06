"""Agent 主循环 (SPEC §4.2) — 双层结构：run_session 外层 + agent_iteration 内层"""
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
    Judgment,
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
)
from microtrace.context.prompt import _assemble_prompt, determine_content_type
from microtrace.context.compaction import compact, is_overflow, extract_microtrace_critical_lines
from microtrace.agent.state import StateHandler, transition
from microtrace.agent.doom_loop import (
    check_doom_loop,
    build_doom_loop_question,
    apply_doom_loop_answer,
)
from microtrace.tools import ToolRegistry
from microtrace.llm import LLMError


# ── helpers ────────────────────────────────────────────────────

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
    格式：{@action: conclude, text: ...} 或 {@action: ask_user, question: ...}
    """
    if not text:
        return {}
    pattern = r"\{@action:\s*(\w+)(?:,\s*(\w+):\s*([^}]*))?\}"
    m = re.search(pattern, text)
    if not m:
        return {}
    action = m.group(1)
    if action == "conclude":
        return {"action": "conclude", "text": m.group(3) or text}
    elif action == "ask_user":
        return {"action": "ask_user", "question": m.group(3) or text}
    return {}


def _generate_session_id() -> str:
    """生成 session ID（时间戳 + 短 hash）"""
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    short = uuid.uuid4().hex[:6]
    return f"{ts}-{short}"


# ── INTAKE ─────────────────────────────────────────────────────

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


# ── 工具执行 ───────────────────────────────────────────────────

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
        tc.output_raw = result.output
        tc.output_summary = _summarize_output(result.output)
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


# ── MAX_ITERATIONS 强制总结 ──────────────────────────────────

def _format_judgment_fallback(ctx: Context) -> str:
    """LLM 不可用时的兜底输出"""
    judgment = ctx.current_judgment
    if judgment.category == JudgmentCategory.UNKNOWN:
        return "Agent 未能形成结论（异常退出）"

    lines = [
        "## 尽力而为的判断",
        "",
        f"**类别**: {judgment.category}",
        f"**置信度**: {judgment.confidence:.2f}",
        f"**理由**: {judgment.one_line_reason}",
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
    return (
        f"CRITICAL - MAXIMUM ITERATIONS REACHED\n\n"
        f"你已用 {ctx.iteration}/{ctx.max_iterations} 轮。工具调用已被禁用。"
        f"你必须用纯文本回答。\n\n"
        f"## 问题\n{problem_raw}\n\n"
        f"## 当前判断\n{ctx.current_judgment.to_brief()}\n\n"
        f"## 证据数：{len(ctx.evidence)} 条\n\n"
        f"## 最近推理\n" + "\n".join(f"- {s}" for s in ctx.reasoning_trace[-5:]) +
        "\n\n请按以下结构回答：\n"
        "1. 已调查的内容（引用证据）\n"
        "2. 当前最佳判断（A/B/C）和置信度\n"
        "3. 未能验证的剩余 gap\n"
        "4. 建议下一步调查方向"
    )


async def _force_max_iter_summary(ctx: Context, llm) -> None:
    """MAX_ITERATIONS 强制总结（LLM 失败用 judgment 兜底）"""
    ctx.append_reasoning("[MAX_ITERATIONS] 强制总结")
    forced_prompt = _build_forced_summary_prompt(ctx)
    try:
        text_parts: list[str] = []
        async for ev in llm.stream(forced_prompt, tools=[]):
            if ev.type == StreamEventType.TEXT_DELTA and ev.text:
                text_parts.append(ev.text)
        ctx.final_output = "".join(text_parts) or _format_judgment_fallback(ctx)
    except Exception as e:
        ctx.append_reasoning(f"[MAX_ITERATIONS] 强制总结 LLM 失败: {e}，用 judgment 兜底")
        ctx.final_output = _format_judgment_fallback(ctx)


# ── CONCLUDE ──────────────────────────────────────────────────

async def _conclude(ctx: Context) -> str:
    """CONCLUDE 态：格式化输出"""
    if ctx.final_output:
        return ctx.final_output

    lines = [
        "# 问题诊断结论",
        "",
        f"**类别**: {ctx.current_judgment.category}",
        f"**置信度**: {ctx.current_judgment.confidence:.2f}",
        f"**理由**: {ctx.current_judgment.one_line_reason}",
        "",
        "## 证据链",
    ]
    for ev in ctx.evidence:
        if ev.importance == EvidenceImportance.CRITICAL:
            lines.append(f"- [{ev.source}] {ev.location}: {ev.content[:100]}")
    return "\n".join(lines)


# ── 持久化辅助 ───────────────────────────────────────────────

async def _save_session(ctx: Context) -> None:
    from microtrace.persistence.sqlite import save_context_to_sqlite
    from microtrace.config import get_db_path
    try:
        save_context_to_sqlite(ctx, str(get_db_path()))
    except Exception as e:
        ctx.append_reasoning(f"[SAVE] 失败: {e}")


# ── agent_iteration（内层：单次 stream）─────────────────────

async def agent_iteration(
    ctx: Context,
    llm,
    tools: ToolRegistry,
) -> None:
    """
    内层 processor：单次 LLM stream 迭代
    - Doom Loop 检测
    - Prompt 组装
    - LLM 流式调用
    - 事件处理（tool_call / ask_user / conclude）
    - 工具执行
    - overflow 检查
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

    # 4. 后处理
    if tool_calls_to_run:
        if len(tool_calls_to_run) > 1:
            await _execute_tools_parallel(ctx, tool_calls_to_run, tools)
        else:
            await _execute_one_tool(ctx, tool_calls_to_run[0], tools)

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

    # 5. Overflow 检查
    try:
        if is_overflow(ctx):
            await compact(ctx, llm)
    except Exception as e:
        ctx.append_reasoning(f"[OVERFLOW/COMPACTION ERROR] {e}")


# ── run_session（外层：显式循环）─────────────────────────────

async def run_session(
    initial_input: str,
    llm,
    tools: ToolRegistry,
    ctx: Context | None = None,
    session_id: str | None = None,
) -> Context:
    """
    外层 driver：显式循环，驱动整个 session
    - 管理 Context 生命周期
    - 调用 agent_iteration() 单次迭代
    - 处理状态转换
    - 管理 session 持久化
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
    # 恢复 ASK_USER：如果 ctx 已经在 ASK_USER 且有 user_replies，回到 INVESTIGATE
    if ctx.state == State.ASK_USER and ctx.user_replies:
        ctx.append_reasoning("[ASK_USER RESUME] 检测到 user_replies，回到 INVESTIGATE")
        await transition(ctx, State.INVESTIGATE, reason="用户已回复")
    elif ctx.state == State.ASK_USER:
        # 继续等待（ctx.pending_question 还在）
        await _save_session(ctx)
        ctx.append_reasoning("[ASK_USER] 继续等待用户回复")
        return ctx

    await StateHandler.enter(ctx, from_state=None)
    await _intake(ctx, initial_input, llm)
    if ctx.state == State.EXIT:
        await _save_session(ctx)
        ctx.append_reasoning("[SESSION END] (after INTAKE EXIT)")
        return ctx

    # ── INVESTIGATE 态：主循环 ──
    await transition(ctx, State.INVESTIGATE, reason="INTAKE 完成")

    while True:
        ctx.iteration += 1
        ctx.append_reasoning(f"[开始第 {ctx.iteration} 轮]")

        # 退出条件 1：max_iterations 到达
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

        # 退出条件 3：已有 final_output（LLM 自决结束）
        if ctx.final_output:
            ctx.append_reasoning(
                f"[LLM 自决结束] {ctx.final_output[:80]}..."
            )
            await transition(ctx, State.CONCLUDE, reason="LLM 自决")
            break

        # ── 单次迭代 ──
        await agent_iteration(ctx, llm, tools)

        # ── 状态检查 ──
        if ctx.state == State.ASK_USER:
            # ASK_USER 是硬阻塞：return ctx 给调用方（REPL/HTTP），
            # 等用户回复后再 resume
            await _save_session(ctx)
            ctx.append_reasoning("[ASK_USER] 等用户回复")
            return ctx

        if ctx.state == State.EXIT:
            break

        if ctx.state == State.CONCLUDE:
            break

        # ── 每轮保存 ──
        await _save_session(ctx)

    # ── CONCLUDE 态 ──
    if not ctx.final_output:
        ctx.final_output = await _conclude(ctx)
    await _save_session(ctx)
    ctx.append_reasoning("[SESSION END]")
    return ctx
