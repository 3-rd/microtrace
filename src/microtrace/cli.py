"""Typer CLI entry point (Phase 1: +dry-run, +patterns)"""
from __future__ import annotations
import time
import typer

app = typer.Typer(invoke_without_command=True)


@app.callback()
def main(
    ctx: typer.Context,
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help="Dry-run 模式：工具只读不写，记录 trace（SPEC §1.6）",
    ),
    trace_dir: str = typer.Option(
        None, "--trace-dir",
        help="Trace 文件输出目录（dry-run 模式使用）",
    ),
    input_text: str = typer.Option(
        None, "--input", "-i",
        help="直接传入问题文本（非交互模式）",
    ),
):
    """microtrace - Java microservice problem diagnosis Agent"""
    if ctx.invoked_subcommand is None:
        from microtrace.repl.main import run_repl

        # Dry-run 模式
        if dry_run:
            typer.echo(f"[dry-run] 工具只读，trace 输出到 {trace_dir or './traces/'}")
            run_repl(dry_run=True, trace_dir=trace_dir)

        # 非交互模式（--input）
        elif input_text:
            _run_headless(input_text, dry_run, trace_dir)

        # 默认：交互式 REPL
        else:
            run_repl()


def _run_headless(input_text: str, dry_run: bool = False, trace_dir: str | None = None) -> None:
    """非交互模式：直接传入问题文本，运行 agent 并输出结论"""
    import asyncio
    from microtrace.agent.loop import run_session
    from microtrace.config import Config
    from microtrace.tools import get_default_registry
    from microtrace.llm import create_default_client

    async def _run():
        config = Config.load()
        llm = create_default_client()
        tools = get_default_registry()
        ctx = await run_session(
            initial_input=input_text,
            llm=llm,
            tools=tools,
        )
        if dry_run and ctx.dry_run:
            ctx.trace_dir = trace_dir
        return ctx

    ctx = asyncio.run(_run())
    if ctx.final_output:
        typer.echo(ctx.final_output)
    else:
        typer.echo(ctx.hypotheses.to_brief())


@app.command()
def patterns(
    limit: int = typer.Option(20, "--limit", "-n", help="显示最近 N 个模式"),
):
    """List diagnosis patterns (Phase 1 机制 6)"""
    from microtrace.persistence.sqlite import load_patterns
    from microtrace.config import get_db_path

    rows = load_patterns(str(get_db_path()))
    if not rows:
        typer.echo("没有保存的诊断模式")
        return

    typer.echo(f"{'ID':<12} {'Status':<10} {'Accuracy':<8} {'Category':<8} Symptom")
    typer.echo("-" * 80)
    for r in rows[:limit]:
        typer.echo(
            f"{r.get('id', '')[:10]:<12} "
            f"{r.get('status', ''):<10} "
            f"{r.get('accuracy', 0):.0%:<8} "
            f"{r.get('category', ''):<8} "
            f"{r.get('symptom_signature', '')[:50]}"
        )


@app.command()
def sessions(
    limit: int = typer.Option(20, "--limit", "-n", help="显示最近 N 个 session"),
):
    """List historical sessions"""
    from microtrace.persistence.sqlite import list_sessions
    from microtrace.config import get_db_path

    db_path = str(get_db_path())
    rows = list_sessions(db_path, limit)

    if not rows:
        typer.echo("没有保存的 session")
        return

    typer.echo(f"{'ID':<40} {'Status':<12} {'Updated':<12} Title")
    typer.echo("-" * 80)
    for r in rows:
        updated = time.strftime("%m-%d %H:%M", time.localtime(r["updated_at"]))
        typer.echo(
            f"{r['id']:<40} {r['status']:<12} {updated:<12} {r['title'] or ''}"
        )


@app.command()
def resume(
    session_id: str = typer.Argument(..., help="session ID"),
):
    """Resume a session by ID"""
    from microtrace.persistence.sqlite import load_context_from_sqlite
    from microtrace.config import get_db_path
    from microtrace.repl.main import run_repl

    db_path = str(get_db_path())
    ctx = load_context_from_sqlite(session_id, db_path)
    if not ctx:
        typer.echo(f"Session '{session_id}' 不存在", err=True)
        raise typer.Exit(1)

    typer.echo(f"恢复 session {session_id}")
    state = ctx.state if isinstance(ctx.state, str) else ctx.state.value
    typer.echo(
        f"状态: state={state}, iter={ctx.iteration}/{ctx.max_iterations}, "
        f"evidence={len(ctx.evidence)}, hypotheses={len(ctx.hypotheses.hypotheses)}"
    )
    run_repl(ctx=ctx)


@app.command()
def delete(
    session_id: str = typer.Argument(..., help="session ID"),
    force: bool = typer.Option(False, "--force", "-f"),
):
    """Delete a session"""
    if not force:
        typer.confirm(f"删除 session '{session_id}'？", abort=True)
    from microtrace.persistence.sqlite import delete_session
    from microtrace.config import get_db_path

    if delete_session(session_id, str(get_db_path())):
        typer.echo(f"已删除 {session_id}")
    else:
        typer.echo(f"Session '{session_id}' 不存在", err=True)
        raise typer.Exit(1)


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="bind host"),
    port: int = typer.Option(8000, help="bind port"),
    reload: bool = typer.Option(False, "--reload", help="auto-reload (dev only)"),
):
    """Run FastAPI HTTP server (Phase 0 preview)"""
    import uvicorn
    typer.echo(f"Starting microtrace API at http://{host}:{port}")
    uvicorn.run(
        "microtrace.http.api:app",
        host=host,
        port=port,
        reload=reload,
    )
