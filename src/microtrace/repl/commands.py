"""REPL 命令集（SPEC §4.7.2）"""
from __future__ import annotations
from typing import Callable
from microtrace.context.models import Context


def cmd_status(ctx: Context | None) -> bool:
    """显示当前状态"""
    from rich.console import Console
    console = Console()
    if not ctx:
        console.print("No active session")
        return True

    state = ctx.state if isinstance(ctx.state, str) else ctx.state.value
    console.print(f"State: {state}")
    console.print(f"Iteration: {ctx.iteration}/{ctx.max_iterations}")
    console.print(f"Evidence: {len(ctx.evidence)} 条")
    console.print(f"Hypotheses: {len(ctx.hypotheses.hypotheses)} 个\n{ctx.hypotheses.to_brief()}")
    return True


def cmd_evidence(ctx: Context | None) -> bool:
    """展开查看完整证据链"""
    from rich.console import Console
    console = Console()
    if not ctx:
        console.print("No active session")
        return True

    for i, ev in enumerate(ctx.evidence, 1):
        console.print(f"\n--- Evidence #{i} ---")
        console.print(f"Source: {ev.source}")
        console.print(f"Location: {ev.location}")
        console.print(f"Importance: {ev.importance} (relevance={ev.relevance:.2f})")
        console.print(f"Content[:300]: {ev.content[:300]}")
        if ev.preserved_lines:
            console.print(f"Key lines: {ev.preserved_lines[:200]}")
    return True


def cmd_hypotheses(ctx: Context | None) -> bool:
    """显示假设集"""
    from rich.console import Console
    console = Console()
    if not ctx:
        console.print("No active session")
        return True

    console.print(f"\n假设集（{len(ctx.hypotheses.hypotheses)} 个）：\n")
    for i, h in enumerate(ctx.hypotheses.hypotheses, 1):
        marker = "→" if h.id == ctx.hypotheses.current_focus else " "
        status_color = {
            "candidate": "yellow",
            "investigating": "blue",
            "confirmed": "green",
            "ruled_out": "red",
        }.get(h.status.value if hasattr(h.status, 'value') else str(h.status), "white")
        console.print(
            f"  {marker} #{i} [{status_color}]{h.status.value}[/{status_color}] "
            f"{h.category.value}({h.confidence:.2f})"
        )
        console.print(f"      {h.statement[:120]}")
        if h.ruled_out_reason:
            console.print(f"      [red]排除: {h.ruled_out_reason[:100]}[/red]")
        if h.evidence_for:
            console.print(f"      [green]evidence_for: {len(h.evidence_for)}[/green]")
        if h.evidence_against:
            console.print(f"      [red]evidence_against: {len(h.evidence_against)}[/red]")
        console.print()
    return True


def cmd_save(ctx: Context | None) -> bool:
    """保存当前 session"""
    from rich.console import Console
    from microtrace.persistence.sqlite import save_context_to_sqlite
    from microtrace.config import get_db_path
    console = Console()

    if not ctx:
        console.print("No active session")
        return True

    try:
        save_context_to_sqlite(ctx, str(get_db_path()))
        console.print(f"已保存到 {ctx.session_id}")
    except Exception as e:
        console.print(f"[red]保存失败: {e}[/red]")
    return True


def cmd_clear(ctx: Context | None) -> bool:
    """重置会话（暂未实现：清空 ctx，调用方负责重建）"""
    from rich.console import Console
    console = Console()
    console.print("重置会话：使用 /exit 退出后重新启动 microtrace")
    return True


def cmd_config(*args, **kwargs) -> bool:
    """查看配置"""
    from rich.console import Console
    from microtrace.config import Config
    console = Console()
    try:
        config = Config.load()
        console.print(config.model_dump_json(indent=2, exclude_none=True))
    except Exception as e:
        console.print(f"[red]读取配置失败: {e}[/red]")
    return True


def cmd_exit(ctx: Context | None) -> bool:
    """退出 REPL"""
    if ctx:
        from microtrace.persistence.sqlite import save_context_to_sqlite
        from microtrace.config import get_db_path
        ctx.user_interrupt = True
        try:
            save_context_to_sqlite(ctx, str(get_db_path()))
        except Exception:
            pass
    return False  # 返回 False = 退出 REPL


# 命令表
COMMANDS: dict[str, Callable] = {
    "/status": cmd_status,
    "/evidence": cmd_evidence,
    "/save": cmd_save,
    "/clear": cmd_clear,
    "/config": cmd_config,
    "/judgment": cmd_hypotheses,
    "/hypotheses": cmd_hypotheses,
    "/exit": cmd_exit,
    "/quit": cmd_exit,
}


def dispatch(cmd: str, ctx: Context | None) -> bool:
    """
    分发 REPL 命令
    返回 True=继续，False=退出
    """
    from rich.console import Console
    console = Console()
    fn = COMMANDS.get(cmd.split()[0].lower())
    if fn:
        return fn(ctx)
    console.print(f"[yellow]未知命令: {cmd}[/yellow]")
    console.print("可用: /status /evidence /hypotheses  /judgment /save /clear /config /exit")
    return True
