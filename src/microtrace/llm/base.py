"""LLMClient Protocol + StreamEvent"""
from __future__ import annotations

from typing import Protocol, AsyncIterator
from microtrace.context.models import StreamEvent


class LLMClient(Protocol):
    """LLM 客户端 Protocol"""

    async def stream(self, prompt: str, tools: list[dict]) -> AsyncIterator[StreamEvent]:
        """流式调用 LLM，返回 StreamEvent 迭代器"""
        ...
