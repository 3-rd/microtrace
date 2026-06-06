"""SQLite 持久化测试 (SPEC §8.1)"""
import os
import time
import tempfile
import pytest
from microtrace.context.models import (
    Context, Problem, Judgment, Evidence, JudgmentCategory, EvidenceSource,
)
from microtrace.persistence.sqlite import (
    init_db, save_context_to_sqlite, load_context_from_sqlite,
    list_sessions, delete_session,
)


@pytest.fixture
def tmp_db():
    """临时数据库 fixture"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    if os.path.exists(path):
        os.unlink(path)


def test_init_db_creates_tables(tmp_db):
    """init_db 创建 sessions 表"""
    init_db(tmp_db)
    import sqlite3
    conn = sqlite3.connect(tmp_db)
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='sessions'"
    ).fetchall()
    assert len(rows) == 1
    conn.close()


def test_save_and_load_roundtrip(tmp_db):
    """save + load 完整 roundtrip"""
    init_db(tmp_db)
    original = Context(
        session_id="test-roundtrip",
        problem=Problem(raw_input="NPE at UserService.java:42", error_type="NPE"),
        current_judgment=Judgment(
            category=JudgmentCategory.A, confidence=0.85,
            one_line_reason="looks like our bug", reasoning="evidence says so"
        ),
    )
    save_context_to_sqlite(original, tmp_db)
    loaded = load_context_from_sqlite("test-roundtrip", tmp_db)
    assert loaded is not None
    assert loaded.session_id == "test-roundtrip"
    assert loaded.problem.error_type == "NPE"
    assert loaded.current_judgment.category == "A"
    assert loaded.current_judgment.confidence == 0.85


def test_load_nonexistent_returns_none(tmp_db):
    """加载不存在的 session 返回 None"""
    init_db(tmp_db)
    loaded = load_context_from_sqlite("nonexistent-id", tmp_db)
    assert loaded is None


def test_list_sessions_orders_by_updated(tmp_db):
    """list_sessions 按 updated_at DESC 排序"""
    init_db(tmp_db)
    # 创建 3 个 session，间隔写入
    for i in range(3):
        ctx = Context(
            session_id=f"session-{i}",
            problem=Problem(raw_input=f"input {i}"),
        )
        save_context_to_sqlite(ctx, tmp_db)
        time.sleep(0.01)  # 确保 updated_at 不同

    sessions = list_sessions(tmp_db)
    assert len(sessions) == 3
    # 最新的在前
    assert sessions[0]["id"] == "session-2"
    assert sessions[2]["id"] == "session-0"


def test_list_sessions_limit(tmp_db):
    """list_sessions limit 参数"""
    init_db(tmp_db)
    for i in range(5):
        ctx = Context(session_id=f"s-{i}")
        save_context_to_sqlite(ctx, tmp_db)
    assert len(list_sessions(tmp_db, limit=2)) == 2
    assert len(list_sessions(tmp_db, limit=10)) == 5


def test_delete_session(tmp_db):
    """delete_session 真的删了"""
    init_db(tmp_db)
    ctx = Context(session_id="to-delete")
    save_context_to_sqlite(ctx, tmp_db)
    assert load_context_from_sqlite("to-delete", tmp_db) is not None

    result = delete_session("to-delete", tmp_db)
    assert result is True
    assert load_context_from_sqlite("to-delete", tmp_db) is None


def test_delete_nonexistent_returns_false(tmp_db):
    """删除不存在的 session 返回 False"""
    init_db(tmp_db)
    result = delete_session("nonexistent", tmp_db)
    assert result is False


def test_save_updates_existing(tmp_db):
    """INSERT OR REPLACE 更新已有 session"""
    init_db(tmp_db)
    ctx1 = Context(
        session_id="same-id",
        problem=Problem(raw_input="first version"),
    )
    save_context_to_sqlite(ctx1, tmp_db)

    ctx2 = Context(
        session_id="same-id",
        problem=Problem(raw_input="second version"),
    )
    save_context_to_sqlite(ctx2, tmp_db)

    loaded = load_context_from_sqlite("same-id", tmp_db)
    assert loaded.problem.raw_input == "second version"
    # list_sessions 应只有 1 条
    assert len(list_sessions(tmp_db)) == 1


def test_session_status_in_progress(tmp_db):
    """正常保存时 status = in_progress"""
    init_db(tmp_db)
    ctx = Context(session_id="in-progress", problem=Problem(raw_input="x"))
    save_context_to_sqlite(ctx, tmp_db)
    sessions = list_sessions(tmp_db)
    assert sessions[0]["status"] == "in_progress"


def test_session_status_abandoned_on_interrupt(tmp_db):
    """user_interrupt=True → status = abandoned"""
    init_db(tmp_db)
    ctx = Context(
        session_id="interrupted",
        problem=Problem(raw_input="x"),
        user_interrupt=True,
    )
    save_context_to_sqlite(ctx, tmp_db)
    sessions = list_sessions(tmp_db)
    assert sessions[0]["status"] == "abandoned"


def test_session_status_completed_on_exit(tmp_db):
    """state=EXIT → status = completed"""
    init_db(tmp_db)
    from microtrace.context.models import State
    ctx = Context(
        session_id="exited",
        problem=Problem(raw_input="x"),
        state=State.EXIT,
    )
    save_context_to_sqlite(ctx, tmp_db)
    sessions = list_sessions(tmp_db)
    assert sessions[0]["status"] == "completed"


def test_evidence_persists(tmp_db):
    """Evidence 字段完整持久化"""
    init_db(tmp_db)
    ctx = Context(
        session_id="with-evidence",
        evidence=[Evidence(
            source=EvidenceSource.CODE,
            location="UserService.java:42",
            content="throw NPE",
            raw_content="throw new NullPointerException()",
            relevance=0.9,
            discovered_at_iteration=2,
            tool_name="read_file",
        )],
    )
    save_context_to_sqlite(ctx, tmp_db)
    loaded = load_context_from_sqlite("with-evidence", tmp_db)
    assert len(loaded.evidence) == 1
    ev = loaded.evidence[0]
    assert ev.location == "UserService.java:42"
    assert ev.relevance == 0.9
    assert ev.tool_name == "read_file"
