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
    console.print(f"Judgment: {ctx.current_judgment.to_brief()}")
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


def cmd_judgment(ctx: Context | None) -> bool:
    """显示判断历史"""
    from rich.console import Console
    console = Console()
    if not ctx:
        console.print("No active session")
        return True

    console.print(f"\n判断历史（{len(ctx.judgment_history)} 次更新）：\n")
    for i, j in enumerate(ctx.judgment_history, 1):
        marker = "★" if i > 1 and j.category != ctx.judgment_history[i - 2].category else ""
        console.print(f"  #{i}  {j.category}({j.confidence:.2f}) {marker}")
        console.print(f"      {j.one_line_reason}\n")

    console.print(
        f"当前: {ctx.current_judgment.category} "
        f"({ctx.current_judgment.confidence:.2f})"
    )
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
    "/judgment": cmd_judgment,
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
    console.print("可用: /status /evidence /judgment /save /clear /config /exit")
    return True
