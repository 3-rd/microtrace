import pytest
from microtrace.context.models import (
    Context, Problem, Hypothesis, HypothesisSet, HypothesisStatus,
    Evidence, ToolCall, UserReply,
    CompactionRecord, AgentEvent, QuestionOption, QuestionPrompt,
    State, SessionStatus, EvidenceSource, EvidenceImportance,
    ContentType, StreamEventType, ToolState, JudgmentCategory,
    DiagnosisClaim, ConfidenceTier, DiagnosisPattern, PatternStatus,
)


def test_context_roundtrip():
    """Context 完整序列化/反序列化"""
    original = Context(
        session_id="test-001",
        problem=Problem(
            raw_input="NPE at UserService.java:42",
            error_type="NullPointerException",
        ),
    )

    # 加一个 hypothesis
    h = Hypothesis(
        statement="NPE in our code",
        category=JudgmentCategory.A,
        confidence=0.85,
        status=HypothesisStatus.CONFIRMED,
    )
    original.hypotheses.add(h)
    original.hypotheses.confirm(h.id)

    # 序列化
    json_str = original.model_dump_json()

    # 反序列化
    restored = Context.model_validate_json(json_str)

    assert restored.session_id == original.session_id
    assert len(restored.hypotheses.hypotheses) == 1
    assert restored.hypotheses.best.category == JudgmentCategory.A
    assert restored.problem.error_type == "NullPointerException"


def test_evidence_with_all_fields():
    """Evidence 完整字段测试"""
    ev = Evidence(
        source=EvidenceSource.CODE,
        location="UserService.java:42",
        content="throw new NPE",
        raw_content="throw new NullPointerException(...)",
        content_type=ContentType.CRITICAL,
        importance=EvidenceImportance.CRITICAL,
        relevance=0.95,
        preserved_lines="at UserService.getUser(UserService.java:42)",
        discovered_at_iteration=2,
        tool_name="read_file",
    )
    assert ev.content_type == ContentType.CRITICAL
    assert ev.compacted is False


def test_state_enum_values():
    """State 枚举的字符串值"""
    assert State.INTAKE.value == "INTAKE"
    assert State.INVESTIGATE.value == "INVESTIGATE"
    assert State.ASK_USER.value == "ASK_USER"
    assert State.CONCLUDE.value == "CONCLUDE"
    assert State.EXIT.value == "EXIT"


def test_stream_event_type_10_types():
    """StreamEventType 修订后只有 10 种"""
    types = [t.value for t in StreamEventType]
    assert len(types) == 10
    # 确认包含的关键事件
    assert "tool-call" in types
    assert "ask-user" in types
    assert "conclude" in types
    assert "judgment-update" in types
