"""search_logs 工具 — 按关键词搜索日志"""
from __future__ import annotations
from pathlib import Path
from microtrace.tools.base import Tool, ToolInput, ToolResult
from pydantic import Field


class SearchLogsInput(ToolInput):
    """search_logs 工具参数"""
    keyword: str = Field(description="搜索关键词（支持多个，逗号分隔；OR 关系）")
    log_dir: str = Field(default="/var/log", description="日志目录（绝对路径）")
    time_range: str | None = Field(
        default=None,
        description="时间范围（暂未实现，仅作为信息保留）",
    )
    max_lines: int = Field(default=100, ge=1, le=1000, description="最多返回行数")


@Tool.define(
    name="search_logs",
    description="按关键词在指定日志目录中搜索。返回包含关键词的行（来自 .log 文件）。",
    input_model=SearchLogsInput,
)
class SearchLogsTool(Tool):
    async def execute(self, args: dict) -> ToolResult:
        try:
            params = SearchLogsInput.model_validate(args)
        except Exception as e:
            return ToolResult(success=False, error=f"参数错误: {e}")

        try:
            keywords = [k.strip() for k in params.keyword.split(",") if k.strip()]
            if not keywords:
                return ToolResult(success=False, error="关键词不能为空")

            log_path = Path(params.log_dir)
            if not log_path.exists():
                return ToolResult(success=False, error=f"日志目录不存在: {params.log_dir}")
            if not log_path.is_dir():
                return ToolResult(success=False, error=f"不是目录: {params.log_dir}")

            results: list[str] = []
            log_files = sorted(log_path.glob("*.log"))[:10]  # 最多 10 个文件

            for log_file in log_files:
                try:
                    with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                        for line in f:
                            if any(kw in line for kw in keywords):
                                results.append(f"[{log_file.name}] {line.rstrip()}")
                                if len(results) >= params.max_lines:
                                    break
                except Exception:
                    continue
                if len(results) >= params.max_lines:
                    break

            if not results:
                return ToolResult(
                    success=True,
                    output=f"未找到匹配 '{params.keyword}' 的日志（目录 {params.log_dir}）",
                )

            return ToolResult(
                success=True,
                output=f"找到 {len(results)} 条匹配：\n" + "\n".join(results),
            )
        except Exception as e:
            return ToolResult(success=False, error=str(e))
