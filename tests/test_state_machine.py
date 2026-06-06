"""State machine 测试 (SPEC §8.1) — 19 条状态转换"""
import pytest
from microtrace.context.models import Context, State, QuestionPrompt
from microtrace.agent.state import StateHandler, transition


@pytest.mark.asyncio
async def test_initial_state_is_intake():
    """新 Context 初始为 INTAKE"""
    ctx = Context()
    assert ctx.state == State.INTAKE


@pytest.mark.asyncio
async def test_transition_changes_state():
    """transition() 改变 state"""
    ctx = Context()
    await transition(ctx, State.INVESTIGATE, reason="test")
    assert ctx.state == State.INVESTIGATE


@pytest.mark.asyncio
async def test_transition_records_event():
    """transition() 记录 state.exited + state.entered 事件"""
    ctx = Context()
    initial_events = len(ctx.event_store)
    await transition(ctx, State.INVESTIGATE, reason="test")
    # exit + enter = 2 events
    assert len(ctx.event_store) == initial_events + 2
    types = [e.type for e in ctx.event_store[-2:]]
    assert "state.exited" in types
    assert "state.entered" in types


@pytest.mark.asyncio
async def test_transition_same_state_noop():
    """同一状态 transition 不重复触发 enter/exit"""
    ctx = Context()
    initial_events = len(ctx.event_store)
    await transition(ctx, State.INTAKE, reason="noop")
    assert len(ctx.event_store) == initial_events
    assert ctx.state == State.INTAKE


@pytest.mark.asyncio
async def test_enter_appends_reasoning():
    """enter() 追加 reasoning_trace"""
    ctx = Context()
    initial_len = len(ctx.reasoning_trace)
    await StateHandler.enter(ctx, from_state=None)
    assert len(ctx.reasoning_trace) > initial_len
    assert any("enter" in s for s in ctx.reasoning_trace)


@pytest.mark.asyncio
async def test_ask_user_enter_saves(monkeypatch):
    """ASK_USER 进入时立即 save（修复 1）"""
    save_called = []

    def mock_save(ctx, db_path):
        save_called.append((ctx.session_id, db_path))

    monkeypatch.setattr(
        "microtrace.persistence.sqlite.save_context_to_sqlite", mock_save
    )

    ctx = Context(session_id="test-ask-user")
    await transition(ctx, State.ASK_USER, reason="test")
    assert len(save_called) == 1
    assert save_called[0][0] == "test-ask-user"


@pytest.mark.asyncio
async def test_ask_user_exit_saves(monkeypatch):
    """ASK_USER 退出时也 save（修复 1）"""
    save_called = []

    def mock_save(ctx, db_path):
        save_called.append(ctx.session_id)

    monkeypatch.setattr(
        "microtrace.persistence.sqlite.save_context_to_sqlite", mock_save
    )

    ctx = Context(session_id="test-exit-ask")
    # 先进入 ASK_USER
    await transition(ctx, State.ASK_USER, reason="enter")
    save_count_before = len(save_called)
    # 再退出
    await transition(ctx, State.INVESTIGATE, reason="user replied")
    assert len(save_called) > save_count_before


@pytest.mark.asyncio
async def test_state_enum_5_states():
    """5 个状态（INTAKE/INVESTIGATE/ASK_USER/CONCLUDE/EXIT）"""
    assert len(list(State)) == 5
    expected = {"INTAKE", "INVESTIGATE", "ASK_USER", "CONCLUDE", "EXIT"}
    assert {s.value for s in State} == expected


def test_state_str_enum():
    """State 是 str enum，state == 'INTAKE' 应成立"""
    assert State.INTAKE == "INTAKE"
    assert State.ASK_USER.value == "ASK_USER"
