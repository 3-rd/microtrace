"""Event types + EventStore（SPEC §4.1 事件溯源）"""
from __future__ import annotations

from microtrace.context.models import StreamEventType, AgentEvent


class EventStore:
    """
    事件溯源存储（append-only）

    用法：
      store = EventStore()
      store.append("tool.called", {"name": "read_file"}, iteration=3)
      recent = store.recent(limit=10)
    """

    def __init__(self, events: list[AgentEvent] | None = None):
        self._events: list[AgentEvent] = events or []

    def append(self, event_type: str, data: dict, iteration: int | None = None) -> None:
        import time
        self._events.append(AgentEvent(
            type=event_type,
            data=data,
            timestamp=time.time(),
            iteration=iteration,
        ))

    def recent(self, limit: int = 10) -> list[AgentEvent]:
        return self._events[-limit:]

    def filter(self, event_type: str) -> list[AgentEvent]:
        return [e for e in self._events if e.type == event_type]

    def __len__(self) -> int:
        return len(self._events)

    def __iter__(self):
        return iter(self._events)

    def clear(self) -> None:
        self._events.clear()


__all__ = ["StreamEventType", "AgentEvent", "EventStore"]
