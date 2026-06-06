"""State handler + transition() — 复用 context.models 里的 State enum"""
from __future__ import annotations
from microtrace.context.models import State, Context


class StateHandler:
    """状态 handler（enter/tick/exit 模式）"""

    @staticmethod
    async def enter(ctx: Context, from_state: str | None = None) -> None:
        """进入状态时执行"""
        ctx.append_reasoning(f"[STATE→{ctx.state}] enter")
        ctx.append_event("state.entered", {
            "state": ctx.state,
            "from_state": from_state,
        })
        # ASK_USER 进入时立即 save（修复 1：避免 ctrl+c 丢 pending_question）
        if ctx.state == State.ASK_USER:
            from microtrace.persistence.sqlite import save_context_to_sqlite
            from microtrace.config import get_db_path
            try:
                save_context_to_sqlite(ctx, str(get_db_path()))
            except Exception as e:
                ctx.append_reasoning(f"[SAVE] ASK_USER 进入时存盘失败: {e}")

    @staticmethod
    async def tick(ctx: Context) -> State | None:
        """态内主逻辑，返回 None=继续本态，返回目标态=切换"""
        return None

    @staticmethod
    async def exit(ctx: Context, to_state: State, reason: str) -> None:
        """退出状态时执行"""
        ctx.append_reasoning(f"[STATE→{ctx.state}→{to_state}] exit, reason={reason}")
        ctx.append_event("state.exited", {
            "from": ctx.state,
            "to": to_state,
            "reason": reason,
        })
        # ASK_USER 退出时也 save
        if ctx.state == State.ASK_USER:
            from microtrace.persistence.sqlite import save_context_to_sqlite
            from microtrace.config import get_db_path
            try:
                save_context_to_sqlite(ctx, str(get_db_path()))
            except Exception as e:
                ctx.append_reasoning(f"[SAVE] ASK_USER 退出时存盘失败: {e}")


async def transition(ctx: Context, target: State, reason: str) -> None:
    """状态切换（exit 旧 → 改 state → enter 新）"""
    from_state = ctx.state
    if from_state == target:
        return
    await StateHandler.exit(ctx, target, reason)
    ctx.state = target
    await StateHandler.enter(ctx, from_state)
