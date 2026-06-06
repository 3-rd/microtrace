"""find_class 工具 — 按类名定位 Java 文件"""
from __future__ import annotations
from pathlib import Path
from microtrace.tools.base import Tool, ToolInput, ToolResult
from pydantic import Field


class FindClassInput(ToolInput):
    """find_class 工具参数"""
    class_name: str = Field(description="Java 类名（不含 .java 后缀，必须以大写字母开头）")
    search_root: str | None = Field(
        default=None,
        description="搜索根目录（默认当前目录）",
    )


@Tool.define(
    name="find_class",
    description="在项目目录中搜索 Java 类文件。返回文件路径和类声明行。",
    input_model=FindClassInput,
)
class FindClassTool(Tool):
    async def execute(self, args: dict) -> ToolResult:
        try:
            params = FindClassInput.model_validate(args)
        except Exception as e:
            return ToolResult(success=False, error=f"参数错误: {e}")

        if not params.class_name or not params.class_name[0].isupper():
            return ToolResult(success=False, error="Java 类名必须以大写字母开头")

        try:
            root = Path(params.search_root) if params.search_root else Path.cwd()
            if not root.exists():
                return ToolResult(success=False, error=f"搜索根目录不存在: {root}")
            if ".." in root.parts:
                return ToolResult(success=False, error="Path traversal not allowed")

            pattern = f"{params.class_name}.java"
            matches = list(root.rglob(pattern))[:5]  # 最多 5 个

            if not matches:
                return ToolResult(
                    success=True,
                    output=f"未找到类: {params.class_name}（在 {root} 下）",
                )

            results: list[str] = []
            for p in matches:
                try:
                    rel = p.relative_to(root)
                except ValueError:
                    rel = p
                # 读类声明行
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        for line in f:
                            if (
                                f"class {params.class_name}" in line
                                or f"interface {params.class_name}" in line
                            ):
                                results.append(f"{rel}: {line.strip()}")
                                break
                except Exception:
                    results.append(str(rel))

            return ToolResult(
                success=True,
                output=f"找到 {len(results)} 个匹配：\n" + "\n".join(results),
            )
        except Exception as e:
            return ToolResult(success=False, error=str(e))
