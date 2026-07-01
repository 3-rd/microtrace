"""ASK_USER 流转测试（修复 ASK_USER resume 后语义）"""
import pytest
from microtrace.context.models import (
    Context, State, Problem, Hypothesis, HypothesisSet, HypothesisStatus,
    JudgmentCategory,
    QuestionPrompt, QuestionOption, UserReply,
)


def test_ask_user_state_persists_pending_question():
    """ASK_USER 时 ctx.pending_question 应保留"""
    ctx = Context(
        state=State.ASK_USER,
        pending_question=QuestionPrompt(
            header="test",
            question="What time?",
            options=[QuestionOption(label="10:00", description="morning")],
            multiple=False,
            custom=True,
        ),
    )
    assert ctx.pending_question is not None
    assert ctx.pending_question.question == "What time?"


def test_ask_user_resume_requires_user_replies():
    """ASK_USER resume 条件：state=ASK_USER + user_replies 非空"""
    ctx = Context(
        state=State.ASK_USER,
        pending_question=QuestionPrompt(
            header="test", question="Q", options=[], multiple=False, custom=True
        ),
    )
    # 没 user_replies → 不能 resume
    assert not ctx.user_replies
    # 加 user_replies → 可以 resume
    ctx.user_replies.append(UserReply(
        question="Q", answer="A", timestamp=1234.0
    ))
    assert ctx.user_replies
    assert ctx.user_replies[0].answer == "A"


def test_user_reply_timestamp():
    """UserReply 需要 timestamp"""
    reply = UserReply(question="Q", answer="A", timestamp=1700000000.0)
    assert reply.timestamp == 1700000000.0


def test_session_id_required_for_ask_user_resume():
    """ASK_USER resume 需要 session_id（用来 load/save）"""
    ctx = Context(session_id="test-123", state=State.ASK_USER)
    assert ctx.session_id == "test-123"


def test_ask_user_question_options_validation():
    """QuestionPrompt 字段验证"""
    q = QuestionPrompt(
        header="Doom Loop",
        question="Continue?",
        options=[
            QuestionOption(label="yes", description="proceed"),
            QuestionOption(label="no", description="stop"),
        ],
        multiple=False,
        custom=True,
    )
    assert len(q.options) == 2
    assert q.multiple is False
    assert q.custom is True


def test_question_option_max_length():
    """QuestionOption label max 20 字"""
    import pytest
    with pytest.raises(Exception):
        QuestionOption(
            label="x" * 21,  # 超过 20
            description="too long",
        )


def test_question_prompt_max_length():
    """QuestionPrompt header max 30 字"""
    import pytest
    with pytest.raises(Exception):
        QuestionPrompt(
            header="x" * 31,  # 超过 30
            question="Q",
            options=[],
            multiple=False,
            custom=True,
        )
