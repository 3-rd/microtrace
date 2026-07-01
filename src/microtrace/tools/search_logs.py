"""search_logs 工具 — 按关键词搜索日志（支持时间范围过滤）"""
from __future__ import annotations
import re
from datetime import datetime, time as dt_time
from pathlib import Path
from microtrace.tools.base import Tool, ToolInput, ToolResult
from pydantic import Field


# 常见日志时间戳格式
TIMESTAMP_PATTERNS = [
    (re.compile(r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})"), "%Y-%m-%d %H:%M:%S"),
    (re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})"), "%Y-%m-%dT%H:%M:%S"),
    (re.compile(r"(\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2}:\d{2})"), "%m/%d/%Y %H:%M:%S"),
    (re.compile(r"(\d{4}-\d{2}-\d{2})"), "%Y-%m-%d"),
]


class SearchLogsInput(ToolInput):
    """search_logs 工具参数"""
    keyword: str = Field(description="搜索关键词（支持多个，逗号分隔；OR 关系）")
    log_dir: str = Field(default="/var/log", description="日志目录（绝对路径）")
    time_range: str | None = Field(
        default=None,
        description="时间范围，格式如 '10:00-11:00' 或 '2026-07-01 10:00-11:00' 或 '2026-07-01'（整天）",
    )
    max_lines: int = Field(default=100, ge=1, le=1000, description="最多返回行数")


@Tool.define(
    name="search_logs",
    description="按关键词在指定日志目录中搜索。支持时间范围过滤。返回包含关键词的行（来自 .log 文件）。",
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

            # 解析时间范围
            time_start, time_end = _parse_time_range(params.time_range) if params.time_range else (None, None)

            results: list[str] = []
            log_files = sorted(log_path.glob("*.log"))[:10]  # 最多 10 个文件

            for log_file in log_files:
                try:
                    with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                        for line in f:
                            # 关键词匹配
                            if not any(kw in line for kw in keywords):
                                continue

                            # 时间范围过滤
                            if time_start is not None:
                                line_time = _extract_timestamp(line)
                                if line_time is not None and not (time_start <= line_time <= (time_end or time_start)):
                                    continue

                            results.append(f"[{log_file.name}] {line.rstrip()}")
                            if len(results) >= params.max_lines:
                                break
                except Exception:
                    continue
                if len(results) >= params.max_lines:
                    break

            if not results:
                time_info = f"(时间范围 {params.time_range}) " if params.time_range else ""
                return ToolResult(
                    success=True,
                    content=f"未找到匹配 '{params.keyword}' {time_info}的日志（目录 {params.log_dir}）",
                )

            return ToolResult(
                success=True,
                content=f"找到 {len(results)} 条匹配：\n" + "\n".join(results),
            )
        except Exception as e:
            return ToolResult(success=False, error=str(e))


def _parse_time_range(time_range: str) -> tuple[datetime | None, datetime | None]:
    """
    解析时间范围字符串

    支持格式：
      - '10:00-11:00' → 今天的时间范围
      - '2026-07-01 10:00-11:00' → 具体日期时间范围
      - '2026-07-01' → 整天
    """
    time_range = time_range.strip()

    # 格式: 'YYYY-MM-DD HH:MM-HH:MM' 或 'YYYY-MM-DD HH:MM:SS-HH:MM:SS'
    if " " in time_range and "-" in time_range:
        # 找到最后一个日期部分
        parts = time_range.split(" ")
        if len(parts) >= 2:
            date_part = parts[0]
            time_parts = parts[1]
            if "-" in time_parts:
                t1, t2 = time_parts.split("-", 1)
                try:
                    start = datetime.strptime(f"{date_part} {t1.strip()}", "%Y-%m-%d %H:%M")
                    # t2 可能只有时间（HH:MM），也可能包含日期
                    if " " in t2:
                        end = datetime.strptime(t2.strip(), "%Y-%m-%d %H:%M")
                    else:
                        end = datetime.strptime(f"{date_part} {t2.strip()}", "%Y-%m-%d %H:%M")
                    return start, end
                except ValueError:
                    pass

    # 格式: 'YYYY-MM-DD'（整天）
    try:
        start = datetime.strptime(time_range, "%Y-%m-%d")
        end = start.replace(hour=23, minute=59, second=59)
        return start, end
    except ValueError:
        pass

    # 格式: 'HH:MM-HH:MM'（今天的时间范围）
    if "-" in time_range:
        parts = time_range.split("-")
        if len(parts) == 2:
            try:
                t1 = dt_time.fromisoformat(parts[0].strip())
                t2 = dt_time.fromisoformat(parts[1].strip())
                today = datetime.now().date()
                return (
                    datetime.combine(today, t1),
                    datetime.combine(today, t2),
                )
            except ValueError:
                pass

    return None, None


def _extract_timestamp(line: str) -> datetime | None:
    """从日志行提取时间戳"""
    for pattern, fmt in TIMESTAMP_PATTERNS:
        m = pattern.search(line)
        if m:
            try:
                return datetime.strptime(m.group(1), fmt)
            except ValueError:
                continue
    return None
