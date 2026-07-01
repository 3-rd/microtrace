"""SQLite 持久化 (SPEC §4.6)"""
from __future__ import annotations
import json
import sqlite3
import time
from pathlib import Path
from microtrace.context.models import Context, SessionStatus


CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('in_progress', 'completed', 'abandoned')),
    title TEXT,
    context_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);
CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions(updated_at DESC);

-- Phase 1: 诊断模式表（机制 6）
CREATE TABLE IF NOT EXISTS patterns (
    id TEXT PRIMARY KEY,
    symptom_signature TEXT NOT NULL,
    error_type TEXT,
    stack_top_class TEXT,
    diagnosis_template TEXT NOT NULL,
    category TEXT NOT NULL CHECK (category IN ('A', 'B', 'C', 'UNKNOWN')),
    status TEXT NOT NULL CHECK (status IN ('active', 'stale', 'archived')),
    success_count INTEGER NOT NULL DEFAULT 0,
    failure_count INTEGER NOT NULL DEFAULT 0,
    accuracy REAL NOT NULL DEFAULT 0.0,
    created_at REAL NOT NULL,
    last_matched_at REAL,
    source_session_id TEXT,
    pattern_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_patterns_status ON patterns(status);
CREATE INDEX IF NOT EXISTS idx_patterns_error_type ON patterns(error_type);
CREATE INDEX IF NOT EXISTS idx_patterns_stack_top ON patterns(stack_top_class);

-- Phase 1: 诊断 trace 表（§1.6 Dry-run Mode）
CREATE TABLE IF NOT EXISTS traces (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    iteration INTEGER NOT NULL,
    hop INTEGER NOT NULL DEFAULT 0,
    event_type TEXT NOT NULL,
    event_data TEXT NOT NULL,
    timestamp REAL NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

CREATE INDEX IF NOT EXISTS idx_traces_session ON traces(session_id);
CREATE INDEX IF NOT EXISTS idx_traces_iteration ON traces(session_id, iteration);
"""


def init_db(db_path: str) -> None:
    """初始化 SQLite 数据库（创建表）"""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(CREATE_TABLE_SQL)
        conn.commit()
    finally:
        conn.close()


def save_context_to_sqlite(ctx: Context, db_path: str) -> None:
    """
    将 Context 序列化为 JSON 保存到 SQLite
    审计修复 1：ASK_USER 进入/退出时也调用
    """
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    try:
        context_json = ctx.model_dump_json(exclude_none=True)
        now = time.time()

        # 状态判定
        state = ctx.state if isinstance(ctx.state, str) else ctx.state.value
        if state == "EXIT":
            status = SessionStatus.COMPLETED.value
        elif ctx.user_interrupt:
            status = SessionStatus.ABANDONED.value
        else:
            status = SessionStatus.IN_PROGRESS.value

        conn.execute(
            """
            INSERT OR REPLACE INTO sessions (id, created_at, updated_at, status, title, context_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                ctx.session_id,
                ctx.created_at or now,
                now,
                status,
                _generate_title(ctx),
                context_json,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def load_context_from_sqlite(session_id: str, db_path: str) -> Context | None:
    """从 SQLite 加载 Context（用于 resume）"""
    if not Path(db_path).exists():
        return None
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT context_json FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if not row:
            return None
        data = json.loads(row[0])
        return Context.model_validate(data)
    finally:
        conn.close()


def list_sessions(db_path: str, limit: int = 20) -> list[dict]:
    """列出最近 N 个 session"""
    if not Path(db_path).exists():
        return []
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT id, created_at, updated_at, status, title
            FROM sessions
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [
            {
                "id": r[0],
                "created_at": r[1],
                "updated_at": r[2],
                "status": r[3],
                "title": r[4],
            }
            for r in rows
        ]
    finally:
        conn.close()


def delete_session(session_id: str, db_path: str) -> bool:
    """删除一个 session"""
    if not Path(db_path).exists():
        return False
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def _generate_title(ctx: Context) -> str:
    """生成 session title（用于 sessions 列表显示）"""
    if ctx.problem and ctx.problem.error_type:
        return ctx.problem.error_type[:50]
    if ctx.final_output:
        return ctx.final_output[:50]
    state = ctx.state if isinstance(ctx.state, str) else ctx.state.value
    return state


# ── Phase 1: Pattern 持久化（机制 6）─────────────────────────

def save_pattern(pattern_dict: dict, db_path: str) -> None:
    """保存或更新一个诊断模式"""
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT OR REPLACE INTO patterns
            (id, symptom_signature, error_type, stack_top_class, diagnosis_template,
             category, status, success_count, failure_count, accuracy,
             created_at, last_matched_at, source_session_id, pattern_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                pattern_dict["id"],
                pattern_dict.get("symptom_signature", ""),
                pattern_dict.get("error_type"),
                pattern_dict.get("stack_top_class"),
                pattern_dict.get("diagnosis_template", ""),
                pattern_dict.get("category", "UNKNOWN"),
                pattern_dict.get("status", "active"),
                pattern_dict.get("success_count", 0),
                pattern_dict.get("failure_count", 0),
                pattern_dict.get("accuracy", 0.0),
                pattern_dict.get("created_at", time.time()),
                pattern_dict.get("last_matched_at"),
                pattern_dict.get("source_session_id"),
                json.dumps(pattern_dict, ensure_ascii=False),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def load_patterns(db_path: str) -> list[dict]:
    """加载所有 pattern（按 success_count 降序）"""
    if not Path(db_path).exists():
        return []
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT pattern_json FROM patterns
            WHERE status != 'archived'
            ORDER BY success_count DESC
            """
        ).fetchall()
        return [json.loads(row[0]) for row in rows]
    finally:
        conn.close()


def load_active_patterns(db_path: str) -> list[dict]:
    """只加载 active 状态的 pattern"""
    if not Path(db_path).exists():
        return []
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT pattern_json FROM patterns
            WHERE status = 'active'
            ORDER BY accuracy DESC, success_count DESC
            """
        ).fetchall()
        return [json.loads(row[0]) for row in rows]
    finally:
        conn.close()


def update_pattern_status(pattern_id: str, status: str, db_path: str) -> None:
    """更新 pattern 状态"""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE patterns SET status = ? WHERE id = ?",
            (status, pattern_id),
        )
        conn.commit()
    finally:
        conn.close()


# ── Phase 1: Trace 记录（§1.6 Dry-run Mode）──────────────────

def save_trace(
    session_id: str,
    iteration: int,
    hop: int,
    event_type: str,
    event_data: dict,
    db_path: str,
) -> None:
    """记录一条 trace（dry-run 模式）"""
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO traces (id, session_id, iteration, hop, event_type, event_data, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"{session_id}-{iteration}-{int(time.time() * 1000)}",
                session_id,
                iteration,
                hop,
                event_type,
                json.dumps(event_data, ensure_ascii=False),
                time.time(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def load_traces(session_id: str, db_path: str) -> list[dict]:
    """加载指定 session 的全部 trace"""
    if not Path(db_path).exists():
        return []
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT iteration, hop, event_type, event_data, timestamp
            FROM traces
            WHERE session_id = ?
            ORDER BY timestamp ASC
            """,
            (session_id,),
        ).fetchall()
        return [
            {
                "iteration": r[0],
                "hop": r[1],
                "event_type": r[2],
                "event_data": json.loads(r[3]),
                "timestamp": r[4],
            }
            for r in rows
        ]
    finally:
        conn.close()
