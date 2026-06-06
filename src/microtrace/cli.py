"""Typer CLI entry point"""
from __future__ import annotations
import time
import typer

app = typer.Typer(invoke_without_command=True)


@app.callback()
def main(
    ctx: typer.Context,
):
    """microtrace - Java microservice problem diagnosis Agent"""
    if ctx.invoked_subcommand is None:
        # 无子命令 → 启动 REPL
        from microtrace.repl.main import run_repl
        run_repl()


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
        f"evidence={len(ctx.evidence)}, judgment={ctx.current_judgment.category}"
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
