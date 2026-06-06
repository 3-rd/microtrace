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
