"""parse_stack_trace 工具 — 解析 Java 堆栈"""
from __future__ import annotations
import re
from microtrace.tools.base import Tool, ToolInput, ToolResult
from pydantic import Field


class ParseStackTraceInput(ToolInput):
    """parse_stack_trace 工具参数"""
    stack_text: str | None = Field(
        default=None,
        description="堆栈文本（如果为空则返回提示）",
    )
    top_n: int = Field(default=10, ge=1, le=50, description="返回前 N 个堆栈帧")


# microtrace 关键行提取模式（与 compaction.py 共享）
MICROTRACE_CRITICAL_PATTERNS = [
    r"Exception in thread",
    r"at\s+[\w\.]+\([\w\.]+\.java:\d+\)",
    r"Caused by:",
    r"error\s*code[:=]?\s*\d{3,4}",
    r"returned\s+status\s+\d{3}",
    r"HTTP/\d\.\d\s+\d{3}",
    r"@Transactional",
    r"@Async",
    r"@Scheduled",
    r"@FeignClient",
    r"@DubboReference",
    r"\b(ERROR|FATAL)\b",
]


@Tool.define(
    name="parse_stack_trace",
    description="解析 Java 堆栈跟踪，提取 class/method/file/line 信息。用于定位异常发生位置。",
    input_model=ParseStackTraceInput,
)
class ParseStackTraceTool(Tool):
    async def execute(self, args: dict) -> ToolResult:
        try:
            params = ParseStackTraceInput.model_validate(args)
        except Exception as e:
            return ToolResult(success=False, error=f"参数错误: {e}")

        try:
            text = params.stack_text or ""
            if not text.strip():
                return ToolResult(success=True, content="堆栈文本为空")

            lines = text.split("\n")
            frames: list[dict] = []
            critical_lines: list[str] = []

            for line in lines:
                # 堆栈帧: at com.foo.Bar.method(File.java:123)
                m = re.search(
                    r"at\s+([\w\.]+)\.([\w<>]+)\(([\w\.]+):(\d+)\)", line
                )
                if m:
                    frames.append({
                        "class": m.group(1),
                        "method": m.group(2),
                        "file": m.group(3),
                        "line": int(m.group(4)),
                    })

                # 关键行提取
                for pattern in MICROTRACE_CRITICAL_PATTERNS:
                    if re.search(pattern, line):
                        critical_lines.append(line.strip())
                        break

            if not frames:
                return ToolResult(
                    success=True,
                    content=f"未解析到堆栈帧：\n{text[:500]}",
                )

            top = frames[: params.top_n]
            result_lines = ["解析到的堆栈帧："]
            for i, f in enumerate(top, 1):
                result_lines.append(
                    f"  {i}. {f['class']}.{f['method']}() at {f['file']}:{f['line']}"
                )

            if critical_lines:
                result_lines.append("\n关键行：")
                for cl in critical_lines[:10]:
                    result_lines.append(f"  - {cl}")

            return ToolResult(success=True, content="\n".join(result_lines))
        except Exception as e:
            return ToolResult(success=False, error=str(e))
