"""MiniMax LLM client (Anthropic SDK) — SPEC §4.5 + OpenClaw 实际配置

实际配置（~/.openclaw/openclaw.json）：
- baseUrl: https://api.minimaxi.com/anthropic
- provider: minimax-portal
- model: MiniMax-M3-highspeed
- API 协议：Anthropic Messages
"""
from __future__ import annotations
import json
import asyncio
from typing import AsyncIterator
from anthropic import (
    AsyncAnthropic,
    APIError,
    APIStatusError,
    APIConnectionError,
    APITimeoutError,
    RateLimitError as AnthropicRateLimitError,
)
from microtrace.context.models import (
    StreamEvent,
    StreamEventType,
)


# ── 错误层级 ──────────────────────────────────────────────────

class LLMError(Exception):
    """LLM 错误基类"""
    pass


class NetworkError(LLMError):
    """网络问题（可重试）"""
    pass


class AuthError(LLMError):
    """认证失败（不重试）"""
    pass


class RateLimitError(LLMError):
    """限流（可重试）"""
    def __init__(self, message: str, retry_after_ms: int | None = None):
        super().__init__(message)
        self.retry_after_ms = retry_after_ms


class BadRequestError(LLMError):
    """请求错误（不重试）"""
    pass


class ServerError(LLMError):
    """服务端错误（可重试）"""
    pass


# ── 常量 (Q11) ────────────────────────────────────────────────

RETRY_DELAYS: list[int] = [2, 4, 8, 16, 32]  # 指数退避（秒）
MAX_RETRIES: int = 5

TRANSIENT_EXCEPTIONS: tuple[type[Exception], ...] = (
    APIConnectionError,
    APITimeoutError,
    ConnectionError,
    TimeoutError,
)


# ── MiniMaxClient ─────────────────────────────────────────────

class MiniMaxClient:
    """
    MiniMax LLM 客户端（Anthropic SDK 兼容协议）
    - baseUrl 默认 https://api.minimaxi.com/anthropic
    - 走 Anthropic Messages API
    """

    def __init__(
        self,
        api_key: str,
        model: str = "MiniMax-M3-highspeed",
        base_url: str = "https://api.minimaxi.com/anthropic",
        timeout: float = 120.0,
        max_tokens: int = 8192,
    ):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url
        self.timeout = timeout
        self.max_tokens = max_tokens
        self._client: AsyncAnthropic | None = None

    @property
    def client(self) -> AsyncAnthropic:
        if self._client is None:
            self._client = AsyncAnthropic(
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=self.timeout,
                max_retries=0,  # 我们自己重试
            )
        return self._client

    # ── stream (主接口) ───────────────────────────────────

    async def stream(
        self,
        prompt: str,
        tools: list[dict] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """
        流式调用 LLM（Anthropic Messages API）
        内部 5 次重试 + 指数退避（Q11）

        实现策略：
        - text_stream → emit TEXT_DELTA（自动跳过 thinking block）
        - get_final_message() → 处理 tool_use（Anthropic 的 tool_use 是 atomic block）
        - 全部完成后 → emit finish_reason
        """
        messages = [{"role": "user", "content": prompt}]
        anthropic_tools = _convert_tools(tools) if tools else None

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                async with self.client.messages.stream(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    messages=messages,
                    tools=anthropic_tools,
                ) as stream_resp:
                    # 1. 流式 yield 文本
                    async for text in stream_resp.text_stream:
                        if text:
                            yield StreamEvent(
                                type=StreamEventType.TEXT_DELTA,
                                text=text,
                            )

                    # 2. 拿 final message，处理 tool_use
                    try:
                        final = await stream_resp.get_final_message()
                    except Exception as e:
                        # get_final_message 失败时不要 break stream
                        yield StreamEvent(
                            type=StreamEventType.TEXT_DELTA,
                            text=f"\n[ERROR: get_final_message failed: {e}]",
                        )
                        return

                    for block in final.content:
                        # block type: "text" / "thinking" / "tool_use"
                        btype = getattr(block, "type", None)
                        if btype == "tool_use":
                            yield StreamEvent(
                                type=StreamEventType.TOOL_CALL,
                                tool_name=block.name,
                                tool_call_id=block.id,
                                tool_args=block.input if isinstance(block.input, dict) else {},
                            )

                    # 3. 通知 stop_reason
                    stop_reason = getattr(final, "stop_reason", None)
                    if stop_reason:
                        yield StreamEvent(
                            type=StreamEventType.TEXT_DELTA,
                            finish_reason=str(stop_reason),
                        )

                return  # 成功

            except TRANSIENT_EXCEPTIONS as e:
                if attempt == MAX_RETRIES:
                    raise ServerError(f"重试 {MAX_RETRIES} 次后仍失败: {e}") from e
                await asyncio.sleep(RETRY_DELAYS[attempt - 1])

            except AnthropicRateLimitError as e:
                retry_after_ms = _extract_retry_after(e)
                if attempt == MAX_RETRIES:
                    raise RateLimitError(
                        f"限流 {MAX_RETRIES} 次后仍失败",
                        retry_after_ms=retry_after_ms,
                    ) from e
                wait = (retry_after_ms or RETRY_DELAYS[attempt - 1] * 1000) / 1000
                await asyncio.sleep(wait)

            except APIStatusError as e:
                code = e.status_code
                if code in (401, 403):
                    raise AuthError(f"认证失败 {code}: {e}") from e
                if 400 <= code < 500:
                    raise BadRequestError(f"请求错误 {code}: {e}") from e
                if attempt == MAX_RETRIES:
                    raise ServerError(f"服务端 {code}，重试耗尽: {e}") from e
                await asyncio.sleep(RETRY_DELAYS[attempt - 1])

            except APIError as e:
                raise NetworkError(f"API 错误: {e}") from e

            except Exception as e:
                raise LLMError(f"未知错误: {e}") from e

    # ── complete (用于 INTAKE 解析等) ──────────────────

    async def complete(self, prompt: str) -> str:
        """非流式调用（INTAKE 解析等简单场景）"""
        messages = [{"role": "user", "content": prompt}]

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = await self.client.messages.create(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    messages=messages,
                )
                # 提取 text content
                texts = [
                    block.text
                    for block in response.content
                    if hasattr(block, "text")
                ]
                return "".join(texts)

            except TRANSIENT_EXCEPTIONS as e:
                if attempt == MAX_RETRIES:
                    raise ServerError(f"重试耗尽: {e}") from e
                await asyncio.sleep(RETRY_DELAYS[attempt - 1])

            except APIStatusError as e:
                code = e.status_code
                if code in (401, 403):
                    raise AuthError(f"认证失败 {code}: {e}") from e
                if 400 <= code < 500:
                    raise BadRequestError(f"请求错误 {code}: {e}") from e
                if attempt == MAX_RETRIES:
                    raise ServerError(f"服务端 {code}，重试耗尽: {e}") from e
                await asyncio.sleep(RETRY_DELAYS[attempt - 1])

            except APIError as e:
                raise NetworkError(f"API 错误: {e}") from e

            except Exception as e:
                raise LLMError(f"未知错误: {e}") from e

        raise LLMError("unreachable")


# ── Anthropic 事件 → microtrace StreamEvent 映射 ──────

# 每个 content block 的累积状态
class _BlockState:
    def __init__(self, block_type: str, block_index: int):
        self.type = block_type
        self.index = block_index
        self.text = ""           # type=text
        self.thinking = ""       # type=thinking
        self.tool_name: str | None = None
        self.tool_id: str | None = None
        self.tool_input_str = ""  # type=tool_use 累积 JSON


def _map_anthropic_event(event) -> list[StreamEvent]:
    """
    把 Anthropic stream 事件转换为 microtrace StreamEvent 列表
    - ContentBlockStartEvent (text) → 不立即 emit（等 delta）
    - ContentBlockStartEvent (tool_use) → 记录 tool info
    - ContentBlockDeltaEvent (text_delta) → emit TEXT_DELTA
    - ContentBlockDeltaEvent (thinking_delta) → emit REASONING_DELTA
    - ContentBlockDeltaEvent (input_json_delta) → 累积
    - ContentBlockStopEvent (tool_use) → emit TOOL_CALL（parse JSON）
    - MessageDeltaEvent → emit finish_reason
    """
    # 这里我们不用全局状态（高层 stream 接口不暴露 block_index）
    # 简化：每个事件独立处理，工具调用累积交给 SDK
    out: list[StreamEvent] = []

    # SDK 高层 stream 接口返回的事件类型：
    # - MessageStartEvent
    # - ContentBlockStartEvent
    # - ContentBlockDeltaEvent
    # - ContentBlockStopEvent
    # - MessageDeltaEvent
    # - MessageStopEvent

    etype = type(event).__name__

    if etype == "ContentBlockDeltaEvent":
        delta = event.delta
        dtype = type(delta).__name__

        if dtype == "TextDelta":
            out.append(StreamEvent(
                type=StreamEventType.TEXT_DELTA,
                text=delta.text,
            ))
        elif dtype == "ThinkingDelta":
            out.append(StreamEvent(
                type=StreamEventType.REASONING_DELTA,
                text=delta.thinking,
            ))
        elif dtype == "InputJSONDelta":
            # 工具 input JSON 增量（不单独 emit，累积到 stop）
            # 这里我们把它当 reasoning 透传，供 caller 累积
            out.append(StreamEvent(
                type=StreamEventType.REASONING_DELTA,  # 占位
                text=delta.partial_json,
                tool_name="__tool_input_delta__",  # 标记
            ))

    elif etype == "ContentBlockStartEvent":
        block = event.content_block
        btype = type(block).__name__
        if btype == "ToolUseBlock":
            # 工具调用开始：先 emit 一个空 TOOL_CALL（含 name/id）
            out.append(StreamEvent(
                type=StreamEventType.TOOL_CALL,
                tool_name=block.name,
                tool_call_id=block.id,
                tool_args={},
            ))

    elif etype == "MessageDeltaEvent":
        # 消息级 delta，含 stop_reason
        delta = event.delta
        if hasattr(delta, "stop_reason") and delta.stop_reason:
            out.append(StreamEvent(
                type=StreamEventType.TEXT_DELTA,  # 复用为结束信号
                finish_reason=str(delta.stop_reason),
            ))

    # 其他事件（MessageStart/Stop/ContentBlockStop）暂不处理

    return out


def _convert_tools(tools: list[dict]) -> list[dict]:
    """
    把 microtrace tool schemas 转 Anthropic tools 格式
    microtrace 格式：{"name": ..., "description": ..., "parameters": {...JSON Schema...}}
    Anthropic 格式：{"name": ..., "description": ..., "input_schema": {...}}
    """
    out = []
    for t in tools:
        out.append({
            "name": t.get("name", ""),
            "description": t.get("description", ""),
            "input_schema": t.get("parameters", {"type": "object", "properties": {}}),
        })
    return out


def _extract_retry_after(error: AnthropicRateLimitError) -> int | None:
    """从 Response headers 提取 Retry-After（毫秒）"""
    resp = getattr(error, "response", None)
    if resp is not None:
        ra = resp.headers.get("retry-after")
        if ra:
            try:
                return int(ra) * 1000
            except ValueError:
                pass
    return None


# ── Factory ─────────────────────────────────────────────────

def create_default_client() -> "MiniMaxClient":
    """从 config + env 创建默认 client"""
    from microtrace.config import Config
    import os

    cfg = Config.load()
    # 优先 env 变量
    api_key = os.environ.get("MICROTRACE_API_KEY") or cfg.llm.api_key or "dummy"
    return MiniMaxClient(
        api_key=api_key,
        model=cfg.llm.model,
        base_url=cfg.llm.base_url,
        timeout=cfg.llm.timeout,
    )
