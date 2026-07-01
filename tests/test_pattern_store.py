"""诊断模式存储 + 匹配测试（机制 6）"""
import pytest
import time
from microtrace.context.models import (
    Context,
    DiagnosisPattern,
    PatternStatus,
    Problem,
    StackFrame,
    Hypothesis,
    HypothesisStatus,
    JudgmentCategory,
    Evidence,
    EvidenceSource,
    ToolCall,
    ToolState,
)
from microtrace.agent.pattern_store import (
    PatternStore,
    _compute_match_score,
    _build_symptom_signature,
    _extract_stack_top,
)


class TestPatternStoreCRUD:
    """Pattern CRUD 测试"""

    def test_add_and_get(self):
        store = PatternStore()
        p = DiagnosisPattern(
            symptom_signature="test",
            diagnosis_template="do X then Y",
            created_at=time.time(),
        )
        store.add(p)
        assert store.get(p.id) == p
        assert len(store) == 1

    def test_list_active_only(self):
        store = PatternStore()
        active = DiagnosisPattern(
            symptom_signature="active",
            diagnosis_template="A",
            status=PatternStatus.ACTIVE,
            created_at=time.time(),
        )
        stale = DiagnosisPattern(
            symptom_signature="stale",
            diagnosis_template="B",
            status=PatternStatus.STALE,
            created_at=time.time(),
        )
        store.add(active)
        store.add(stale)
        assert len(store.list_active()) == 1

    def test_record_success_increments(self):
        store = PatternStore()
        p = DiagnosisPattern(
            symptom_signature="test",
            diagnosis_template="X",
            created_at=time.time(),
        )
        store.add(p)
        store.record_success(p.id)
        updated = store.get(p.id)
        assert updated.success_count == 1
        assert updated.status == PatternStatus.ACTIVE

    def test_record_failure_tracks(self):
        store = PatternStore()
        p = DiagnosisPattern(
            symptom_signature="test",
            diagnosis_template="X",
            success_count=5,
            failure_count=1,
            created_at=time.time(),
        )
        store.add(p)
        store.record_failure(p.id)
        updated = store.get(p.id)
        assert updated.failure_count == 2

    def test_accuracy_computation(self):
        p = DiagnosisPattern(
            symptom_signature="test",
            diagnosis_template="X",
            success_count=7,
            failure_count=3,
            created_at=time.time(),
        )
        assert p.accuracy == 0.7

    def test_zero_accuracy(self):
        p = DiagnosisPattern(
            symptom_signature="test",
            diagnosis_template="X",
            success_count=0,
            failure_count=0,
            created_at=time.time(),
        )
        assert p.accuracy == 0.0

    def test_decay_patterns(self):
        store = PatternStore()
        old = DiagnosisPattern(
            symptom_signature="old",
            diagnosis_template="X",
            status=PatternStatus.ACTIVE,
            created_at=time.time() - 8 * 24 * 3600,  # 8 天前
            last_matched_at=time.time() - 8 * 24 * 3600,
        )
        store.add(old)
        decayed = store.decay_patterns()
        assert decayed >= 1
        updated = store.get(old.id)
        assert updated.status == PatternStatus.STALE


class TestMatchScore:
    """匹配分数计算测试"""

    def test_exact_error_type_match(self):
        problem = Problem(
            raw_input="test",
            error_type="NullPointerException",
        )
        pattern = DiagnosisPattern(
            symptom_signature="NPE in controller",
            error_type="NullPointerException",
            diagnosis_template="check null",
            created_at=time.time(),
        )
        score = _compute_match_score(problem, pattern)
        assert score >= 0.4

    def test_stack_top_match(self):
        problem = Problem(
            raw_input="test",
            error_type="RuntimeException",
            stack_frames=[StackFrame(
                class_name="com.foo.UserService",
                method_name="getUser",
                file_name="UserService.java",
                line_number=42,
            )],
        )
        pattern = DiagnosisPattern(
            symptom_signature="error in service",
            stack_top_class="com.foo.UserService",
            diagnosis_template="check service",
            created_at=time.time(),
        )
        score = _compute_match_score(problem, pattern)
        assert score >= 0.3

    def test_no_match(self):
        problem = Problem(raw_input="test", error_type="TimeoutException")
        pattern = DiagnosisPattern(
            symptom_signature="NPE error",
            error_type="NullPointerException",
            stack_top_class="com.other.Class",
            diagnosis_template="X",
            created_at=time.time(),
        )
        score = _compute_match_score(problem, pattern)
        assert score == 0.0


class TestExtractFromSession:
    """Pattern 提取测试"""

    def test_extract_from_successful_session(self):
        store = PatternStore()
        ctx = Context(
            session_id="test-session",
            iteration=4,
        )
        ctx.problem = Problem(
            raw_input="test NPE",
            error_type="NullPointerException",
            stack_frames=[StackFrame(
                class_name="com.foo.Bar",
                method_name="method",
                file_name="Bar.java",
                line_number=10,
            )],
        )
        h = Hypothesis(
            statement="NPE caused by null check missing",
            category=JudgmentCategory.A,
            status=HypothesisStatus.CONFIRMED,
            confidence=0.9,
            evidence_for=["ev1", "ev2"],
        )
        ctx.hypotheses.add(h)
        ctx.hypotheses.confirm(h.id)

        # Add evidence
        ev1 = Evidence(id="ev1", source=EvidenceSource.CODE, location="Bar.java:10",
                       content="content", raw_content="raw", discovered_at_iteration=1)
        ev2 = Evidence(id="ev2", source=EvidenceSource.LOG, location="app.log",
                       content="content", raw_content="raw", discovered_at_iteration=2)
        ctx.evidence = [ev1, ev2]

        # Add tool history
        ctx.tool_history = [
            ToolCall(name="read_file", args={}, args_summary="", output_summary="",
                     iteration=1, state=ToolState.COMPLETED),
        ]

        pattern = store.extract_from_session(ctx)
        assert pattern is not None
        assert pattern.category == JudgmentCategory.A
        assert pattern.error_type == "NullPointerException"
        assert pattern.success_count == 1
