"""Doom Loop 检测 (SPEC §4.4 / DESIGN §2.5)"""
from __future__ import annotations
import json
from microtrace.context.models import Context, QuestionPrompt, QuestionOption


DOOM_LOOP_THRESHOLD: int = 3  # 与 OpenCode 一致


def check_doom_loop(ctx: Context) -> bool:
    """
    Doom Loop 检测：最近 3 次 tool call 是否完全相同
    - 匹配条件：tool name + JSON 序列化 args 完全相同
    - 触发：标记 ctx.doom_loop_tool，由 caller 决定进入 ASK_USER

    Returns: True 表示 doom loop 已检测到
    """
    if len(ctx.tool_history) < DOOM_LOOP_THRESHOLD:
        return False

    last_calls = ctx.tool_history[-DOOM_LOOP_THRESHOLD:]
    first = last_calls[0]

    if not all(
        tc.name == first.name
        and json.dumps(tc.args, sort_keys=True, default=str)
        == json.dumps(first.args, sort_keys=True, default=str)
        for tc in last_calls
    ):
        return False

    # 标记 doom loop
    ctx.doom_loop_tool = first.name
    ctx.doom_loop_args = first.args
    ctx.append_reasoning(
        f"[DOOM LOOP] 工具 {first.name} 被连续 {DOOM_LOOP_THRESHOLD} 次以相同参数调用"
    )
    return True


def build_doom_loop_question(ctx: Context) -> QuestionPrompt:
    """构造 Doom Loop 弹窗（once/always/reject/custom）"""
    last_call = ctx.tool_history[-1]
    args_summary = last_call.args_summary or json.dumps(last_call.args, ensure_ascii=False)[:80]
    return QuestionPrompt(
        header="Doom Loop (3次)",
        question=(
            f"工具 `{last_call.name}` 连续 {DOOM_LOOP_THRESHOLD} 次以相同参数调用。\n"
            f"参数: {args_summary}\n"
            f"最近结果: {last_call.output_summary[:100]}\n\n"
            f"你想怎么办？"
        ),
        options=[
            QuestionOption(
                label="继续",
                description="这一次允许，LLM 再调一次",
            ),
            QuestionOption(
                label="总是允许",
                description="整个 session 不再问",
            ),
            QuestionOption(
                label="拒绝",
                description="禁用此工具，agent 必须换思路",
            ),
        ],
        multiple=False,
        custom=True,
    )


def apply_doom_loop_answer(ctx: Context, answer: str) -> None:
    """
    处理用户对 Doom Loop 弹窗的回复
    - 继续/once：清空标记，让 LLM 再调
    - 总是允许/always：加入 allowed_tools（如果 Context 启用该字段）
    - 拒绝/reject：加入 disabled_tools
    - 其他：自定义回复，记入 reasoning_trace
    """
    ans = answer.strip()
    tool = ctx.doom_loop_tool

    if ans in ("1", "继续", "once"):
        ctx.doom_loop_tool = None
        ctx.doom_loop_args = None
        ctx.append_reasoning("[DOOM LOOP] 用户选择：继续")

    elif ans in ("2", "总是允许", "always"):
        ctx.doom_loop_tool = None
        ctx.doom_loop_args = None
        # 允许清单（可选，Phase 0 简化：靠 disabled_tools 控制）
        ctx.append_reasoning("[DOOM LOOP] 用户选择：总是允许")

    elif ans in ("3", "拒绝", "reject") and tool:
        ctx.disabled_tools.add(tool)
        ctx.doom_loop_tool = None
        ctx.doom_loop_args = None
        ctx.append_reasoning(f"[DOOM LOOP] 用户选择：拒绝，禁用 {tool}")

    else:
        ctx.doom_loop_tool = None
        ctx.doom_loop_args = None
        ctx.append_reasoning(f"[DOOM LOOP] 用户自定义回复: {ans[:100]}")
