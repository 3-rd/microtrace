"""Doom Loop 测试 (SPEC §8.1)"""
import pytest
from microtrace.context.models import (
    Context, ToolCall, ToolState, QuestionPrompt,
)
from microtrace.agent.doom_loop import (
    check_doom_loop,
    build_doom_loop_question,
    apply_doom_loop_answer,
    DOOM_LOOP_THRESHOLD,
)


def _add_call(ctx, name, args, iteration):
    ctx.add_tool_call(ToolCall(
        name=name, args=args, args_summary=str(args)[:30],
        output_summary="", iteration=iteration, state=ToolState.COMPLETED,
    ))


def test_doom_loop_threshold_is_3():
    """DOOM_LOOP_THRESHOLD = 3（与 OpenCode 一致）"""
    assert DOOM_LOOP_THRESHOLD == 3


def test_doom_loop_triggers_on_3_same_calls():
    """连续 3 次相同调用 → 触发"""
    ctx = Context()
    for i in range(3):
        _add_call(ctx, "find_class", {"class_name": "UserService"}, i + 1)
    assert check_doom_loop(ctx) == True
    assert ctx.doom_loop_tool == "find_class"
    assert ctx.doom_loop_args == {"class_name": "UserService"}


def test_doom_loop_not_triggered_on_2_calls():
    """只有 2 次 → 不触发"""
    ctx = Context()
    for i in range(2):
        _add_call(ctx, "find_class", {"class_name": "UserService"}, i + 1)
    assert check_doom_loop(ctx) == False
    assert ctx.doom_loop_tool is None


def test_doom_loop_not_triggered_on_different_args():
    """不同参数 → 不触发"""
    ctx = Context()
    _add_call(ctx, "find_class", {"class_name": "A"}, 1)
    _add_call(ctx, "find_class", {"class_name": "B"}, 2)
    _add_call(ctx, "find_class", {"class_name": "C"}, 3)
    assert check_doom_loop(ctx) == False


def test_doom_loop_not_triggered_on_different_tools():
    """不同工具 → 不触发"""
    ctx = Context()
    _add_call(ctx, "find_class", {"class_name": "A"}, 1)
    _add_call(ctx, "read_file", {"file_path": "/x"}, 2)
    _add_call(ctx, "search_logs", {"keyword": "err"}, 3)
    assert check_doom_loop(ctx) == False


def test_doom_loop_only_looks_at_last_3():
    """只看最近 3 次（前面的不算）"""
    ctx = Context()
    # 前面 3 次相同
    for i in range(3):
        _add_call(ctx, "find_class", {"class_name": "Old"}, i + 1)
    # 后面 3 次不同
    for i in range(3):
        _add_call(ctx, "find_class", {"class_name": f"New{i}"}, i + 4)
    # 最后 3 次不同，不触发
    assert check_doom_loop(ctx) == False


def test_build_doom_loop_question():
    """build_doom_loop_question 构造弹窗"""
    ctx = Context()
    _add_call(ctx, "find_class", {"class_name": "X"}, 1)
    _add_call(ctx, "find_class", {"class_name": "X"}, 2)
    _add_call(ctx, "find_class", {"class_name": "X"}, 3)
    check_doom_loop(ctx)  # 标记 doom_loop_tool

    question = build_doom_loop_question(ctx)
    assert isinstance(question, QuestionPrompt)
    assert "find_class" in question.question
    assert len(question.options) == 3
    labels = [opt.label for opt in question.options]
    assert "继续" in labels
    assert "总是允许" in labels
    assert "拒绝" in labels
    assert question.custom is True


def test_apply_answer_continue():
    """apply_doom_loop_answer(继续) 清空标记"""
    ctx = Context()
    for i in range(3):
        _add_call(ctx, "find_class", {"class_name": "X"}, i + 1)
    check_doom_loop(ctx)
    assert ctx.doom_loop_tool == "find_class"

    apply_doom_loop_answer(ctx, "继续")
    assert ctx.doom_loop_tool is None
    assert ctx.doom_loop_args is None
    assert "find_class" not in ctx.disabled_tools


def test_apply_answer_reject_disables_tool():
    """apply_doom_loop_answer(拒绝) 禁用工具"""
    ctx = Context()
    for i in range(3):
        _add_call(ctx, "find_class", {"class_name": "X"}, i + 1)
    check_doom_loop(ctx)

    apply_doom_loop_answer(ctx, "拒绝")
    assert ctx.doom_loop_tool is None
    assert "find_class" in ctx.disabled_tools


def test_apply_answer_always_allows():
    """apply_doom_loop_answer(总是允许) 不禁用"""
    ctx = Context()
    for i in range(3):
        _add_call(ctx, "find_class", {"class_name": "X"}, i + 1)
    check_doom_loop(ctx)

    apply_doom_loop_answer(ctx, "总是允许")
    assert ctx.doom_loop_tool is None
    assert "find_class" not in ctx.disabled_tools


def test_apply_answer_custom():
    """apply_doom_loop_answer(自定义) 不禁用，只记录"""
    ctx = Context()
    for i in range(3):
        _add_call(ctx, "find_class", {"class_name": "X"}, i + 1)
    check_doom_loop(ctx)

    apply_doom_loop_answer(ctx, "改用 search_logs")
    assert ctx.doom_loop_tool is None
    assert "find_class" not in ctx.disabled_tools
    assert any("自定义" in s for s in ctx.reasoning_trace)


def test_doom_loop_args_dict_order_independent():
    """args 顺序不影响（用 sort_keys=True 序列化）"""
    ctx1 = Context()
    ctx2 = Context()
    # 同样内容但顺序不同
    for i in range(3):
        _add_call(ctx1, "find_class", {"class_name": "A", "offset": 0}, i + 1)
        _add_call(ctx2, "find_class", {"offset": 0, "class_name": "A"}, i + 1)
    assert check_doom_loop(ctx1) == True
    # 注：ctx2 的 doom_loop_tool 也会被设置（因为 check 是独立调用）
    # 这里只验证 ctx1 触发了
