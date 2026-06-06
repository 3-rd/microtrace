"""Tool registry + 默认工具注册"""
from microtrace.tools.base import Tool, ToolInput, ToolResult
from microtrace.tools.read_file import ReadFileTool, ReadFileInput
from microtrace.tools.search_logs import SearchLogsTool, SearchLogsInput
from microtrace.tools.find_class import FindClassTool, FindClassInput
from microtrace.tools.parse_stack_trace import ParseStackTraceTool, ParseStackTraceInput

__all__ = [
    "Tool", "ToolInput", "ToolResult", "ToolRegistry",
    "ReadFileTool", "ReadFileInput",
    "SearchLogsTool", "SearchLogsInput",
    "FindClassTool", "FindClassInput",
    "ParseStackTraceTool", "ParseStackTraceInput",
    "registry", "default_registry", "get_default_registry",
]


class ToolRegistry:
    """工具注册表（按 name 索引 Tool 实例）"""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """注册 Tool 实例"""
        if not tool.name:
            raise ValueError("Tool must have a non-empty name")
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        """移除工具"""
        self._tools.pop(name, None)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def has(self, name: str) -> bool:
        return name in self._tools

    def list_tools(self) -> list[str]:
        return list(self._tools.keys())

    def schemas(self) -> list[dict]:
        """返回所有工具的 schema（用于 LLM prompt）"""
        return [tool.schema for tool in self._tools.values()]

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools


# 默认注册表（4 个最小工具）
default_registry = ToolRegistry()
default_registry.register(ReadFileTool())
default_registry.register(SearchLogsTool())
default_registry.register(FindClassTool())
default_registry.register(ParseStackTraceTool())

# 全局单例（兼容旧 import）
registry = default_registry


def get_default_registry() -> ToolRegistry:
    """返回默认 registry"""
    return default_registry
