"""read_file 工具 — 读取代码或日志文件"""
from __future__ import annotations
from pathlib import Path
from microtrace.tools.base import Tool, ToolInput, ToolResult
from pydantic import Field


class ReadFileInput(ToolInput):
    """read_file 工具参数"""
    file_path: str = Field(description="文件路径（绝对路径或相对当前目录）")
    offset: int = Field(default=0, ge=0, description="起始行号（0-based）")
    limit: int = Field(default=200, ge=1, description="最多读取行数")
    max_bytes: int = Field(default=10_000_000, ge=1, description="最大文件大小（字节）")


@Tool.define(
    name="read_file",
    description="读取代码或日志文件内容。支持指定行号范围。用于查看源代码或日志内容。",
    input_model=ReadFileInput,
)
class ReadFileTool(Tool):
    async def execute(self, args: dict) -> ToolResult:
        try:
            params = ReadFileInput.model_validate(args)
        except Exception as e:
            return ToolResult(success=False, error=f"参数错误: {e}")

        try:
            p = Path(params.file_path)
            # 安全检查：不允许 .. 穿越
            if ".." in p.parts:
                return ToolResult(success=False, error="Path traversal not allowed")
            if not p.exists():
                return ToolResult(success=False, error=f"文件不存在: {params.file_path}")
            if not p.is_file():
                return ToolResult(success=False, error=f"不是文件: {params.file_path}")

            size = p.stat().st_size
            if size > params.max_bytes:
                return ToolResult(
                    success=False,
                    error=f"文件过大: {size} 字节（上限 {params.max_bytes}）",
                )

            with open(p, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()

            start = min(params.offset, len(lines))
            end = min(start + params.limit, len(lines))
            content = "".join(lines[start:end])

            header = f"--- {params.file_path} (lines {start+1}-{end} of {len(lines)}) ---\n"
            return ToolResult(success=True, output=header + content)
        except Exception as e:
            return ToolResult(success=False, error=str(e))
