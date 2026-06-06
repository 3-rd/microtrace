"""5 次重试 + 指数退避 + transient-only（保留，未来用）"""
from __future__ import annotations

import asyncio
import random
from typing import TypeVar, Callable, Any

T = TypeVar("T")


async def with_retry(
    func: Callable[..., T],
    max_attempts: int = 5,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    transient_exceptions: tuple[type[Exception], ...] = (),
) -> T:
    """
    指数退避重试（只重试 transient 异常）
    """
    last_exception: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await func()
        except transient_exceptions as e:
            last_exception = e
            if attempt == max_attempts:
                raise
            delay = min(base_delay * (2 ** (attempt - 1)) + random.uniform(0, 0.5), max_delay)
            await asyncio.sleep(delay)
        except Exception as e:
            raise
    raise last_exception or RuntimeError("unreachable")
