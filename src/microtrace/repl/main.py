"""REPL 入口 (SPEC §4.7)"""
from __future__ import annotations
import asyncio
import sys
from pathlib import Path
from microtrace.context.models import Context, State, QuestionPrompt
from microtrace.config import Config
from microtrace.tools import get_default_registry
from microtrace.llm import create_default_client
from microtrace.repl.commands import dispatch
from microtrace.windows.console import _setup_windows_console


def _setup_history():
    """设置 REPL history 文件路径"""
    from platformdirs import user_data_dir
    from microtrace.config import APP_NAME
    history_dir = Path(user_data_dir(APP_NAME))
    history_dir.mkdir(parents=True, exist_ok=True)
    return history_dir / "repl_history"


def _get_input_sync(prompt_session, prompt_str: str = "microtrace> ") -> str:
    """同步获取用户输入（在 to_thread 中运行）"""
    try:
        return prompt_session.prompt(prompt_str)
    except (KeyboardInterrupt, EOFError):
        raise


async def _get_input(prompt_session) -> str:
    """异步获取用户输入"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _get_input_sync, prompt_session)


async def _ask_user_input(prompt_session, question: QuestionPrompt) -> str:
    """ASK_USER 场景：显示问题，等用户回复"""
    from rich.console import Console
    from rich.panel import Panel
    console = Console()

    console.print(Panel(
        question.question,
        title=f"[bold yellow]{question.header}[/bold yellow]",
        expand=False,
    ))
    if question.options:
        for i, opt in enumerate(question.options, 1):
            console.print(f"  [dim]{i}[/dim] {opt.label} - {opt.description}")
    console.print()

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, _get_input_sync, prompt_session, "[用户回复] "
    )


def _display_state(ctx: Context) -> None:
    """显示当前状态 banner"""
    from rich.console import Console
    from rich.table import Table
    console = Console()

    state = ctx.state if isinstance(ctx.state, str) else ctx.state.value
    state_colors = {
        "INTAKE": "blue",
        "INVESTIGATE": "green",
        "ASK_USER": "yellow",
        "CONCLUDE": "cyan",
        "EXIT": "red",
    }
    color = state_colors.get(state, "white")

    table = Table(show_header=False, box=None)
    table.add_column(style="bold")
    table.add_column()
    table.add_row("状态", f"[{color}]{state}[/{color}]")
    table.add_row("轮次", f"{ctx.iteration}/{ctx.max_iterations}")
    table.add_row("证据", f"{len(ctx.evidence)} 条")
    table.add_row("判断", ctx.current_judgment.to_brief())
    console.print(table)
    console.print()


def _is_command(text: str) -> bool:
    """判断是否 REPL 命令"""
    return text.strip().startswith("/")


def run_repl(ctx: Context | None = None) -> None:
    """
    REPL 主入口
    - 加载配置
    - 初始化 prompt_toolkit session
    - 处理用户输入
    - 调用 run_session
    - 显示状态 banner
    """
    _setup_windows_console()

    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.history import FileHistory
    except ImportError:
        print("ERROR: prompt_toolkit 未安装。pip install prompt-toolkit", file=sys.stderr)
        sys.exit(1)

    history_path = _setup_history()
    session = PromptSession(history=FileHistory(str(history_path)))

    ctx = ctx or None
    print("microtrace REPL — 输入问题开始诊断，输入 /exit 退出，输入 /help 查看命令")
    print()

    while True:
        try:
            user_input = asyncio.run(_get_input(session))
        except (KeyboardInterrupt, EOFError):
            break

        if not user_input or not user_input.strip():
            continue

        # REPL 命令
        if _is_command(user_input):
            if not dispatch(user_input, ctx):
                break
            continue

        # 运行 agent session
        try:
            ctx = asyncio.run(_run_agent(user_input.strip(), ctx))
            _display_state(ctx)
        except Exception as e:
            print(f"ERROR: {e}", file=sys.stderr)
            if ctx:
                ctx.append_reasoning(f"[REPL ERROR] {e}")

    print("Goodbye!")


async def _run_agent(initial_input: str, ctx: Context | None) -> Context:
    """运行一次 agent session"""
    from microtrace.agent.loop import run_session

    config = Config.load()
    llm = create_default_client()
    tools = get_default_registry()

    ctx = await run_session(
        initial_input=initial_input,
        llm=llm,
        tools=tools,
        ctx=ctx,
    )
    return ctx
