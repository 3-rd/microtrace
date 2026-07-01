"""FastAPI HTTP API (SPEC §4.8) — Phase 1+ 推迟，本版本提供最小骨架"""
from __future__ import annotations
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时初始化 DB"""
    from microtrace.config import get_db_path
    from microtrace.persistence.sqlite import init_db
    init_db(str(get_db_path()))
    yield


app = FastAPI(
    title="microtrace API",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatRequest(BaseModel):
    """POST /chat 请求"""
    message: str
    session_id: str | None = None


class ChatResponse(BaseModel):
    """POST /chat 响应"""
    session_id: str
    state: str
    final_output: str | None = None


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest) -> ChatResponse:
    """
    主对话接口
    - 新建 session 或继续已有 session
    - 运行 run_session
    - 返回最终输出
    """
    from microtrace.agent.loop import run_session
    from microtrace.persistence.sqlite import load_context_from_sqlite
    from microtrace.config import get_db_path
    from microtrace.tools import get_default_registry
    from microtrace.llm import create_default_client

    db_path = str(get_db_path())

    # resume 或新建
    ctx = None
    if req.session_id:
        ctx = load_context_from_sqlite(req.session_id, db_path)
        if not ctx:
            raise HTTPException(404, f"Session {req.session_id} not found")

    llm = create_default_client()
    tools = get_default_registry()

    ctx = await run_session(
        initial_input=req.message,
        llm=llm,
        tools=tools,
        ctx=ctx,
        session_id=req.session_id,
    )

    state = ctx.state if isinstance(ctx.state, str) else ctx.state.value
    return ChatResponse(
        session_id=ctx.session_id or "",
        state=state,
        final_output=ctx.final_output,
    )


@app.get("/state/{session_id}")
async def get_state(session_id: str):
    """获取 session 状态"""
    from microtrace.persistence.sqlite import load_context_from_sqlite
    from microtrace.config import get_db_path

    ctx = load_context_from_sqlite(session_id, str(get_db_path()))
    if not ctx:
        raise HTTPException(404, f"Session {session_id} not found")

    state = ctx.state if isinstance(ctx.state, str) else ctx.state.value
    return {
        "session_id": ctx.session_id,
        "state": state,
        "iteration": ctx.iteration,
        "max_iterations": ctx.max_iterations,
        "hypotheses": ctx.hypotheses.to_brief(),
        "evidence_count": len(ctx.evidence),
    }


@app.get("/evidence/{session_id}")
async def get_evidence(session_id: str):
    """获取证据列表"""
    from microtrace.persistence.sqlite import load_context_from_sqlite
    from microtrace.config import get_db_path

    ctx = load_context_from_sqlite(session_id, str(get_db_path()))
    if not ctx:
        raise HTTPException(404, f"Session {session_id} not found")

    return [
        {
            "id": ev.id,
            "source": ev.source,
            "location": ev.location,
            "content": ev.content[:200],
            "importance": ev.importance,
            "relevance": ev.relevance,
        }
        for ev in ctx.evidence
    ]


@app.post("/save/{session_id}")
async def save_session(session_id: str):
    """手动保存 session"""
    from microtrace.persistence.sqlite import (
        load_context_from_sqlite,
        save_context_to_sqlite,
    )
    from microtrace.config import get_db_path

    ctx = load_context_from_sqlite(session_id, str(get_db_path()))
    if not ctx:
        raise HTTPException(404, f"Session {session_id} not found")

    save_context_to_sqlite(ctx, str(get_db_path()))
    return {"status": "saved"}


@app.get("/sessions")
async def list_sessions(limit: int = 20):
    """列出最近 sessions"""
    from microtrace.persistence.sqlite import list_sessions as _list
    from microtrace.config import get_db_path
    return _list(str(get_db_path()), limit=limit)
