"""Tool base class + Tool.define 模式"""
from __future__ import annotations

from abc import ABC, abstractmethod
from pydantic import BaseModel


class ToolInput(BaseModel):
    """Tool input 基类（工具用 Pydantic 定义参数）"""
    pass


class ToolResult(BaseModel):
    """Tool 执行结果"""
    success: bool = True
    content: str = ""
    error: str | None = None


class Tool(ABC):
    """
    工具基类
    子类用 @Tool.define(name=..., description=..., input_model=...) 注册
    并实现 async execute(args: dict) -> ToolResult
    """
    name: str = ""
    description: str = ""
    input_model: type[ToolInput] | None = None

    @abstractmethod
    async def execute(self, args: dict) -> ToolResult:
        """执行工具（子类实现）"""
        ...

    @property
    def schema(self) -> dict:
        """返回工具 schema（用于 LLM prompt）"""
        if self.input_model is None:
            return {
                "name": self.name,
                "description": self.description,
                "parameters": {"type": "object", "properties": {}},
            }
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.input_model.model_json_schema(),
        }

    @classmethod
    def define(
        cls,
        name: str,
        description: str,
        input_model: type[ToolInput] | None = None,
    ):
        """装饰器：注册工具元信息"""
        def decorator(subclass: type[Tool]) -> type[Tool]:
            subclass.name = name
            subclass.description = description
            subclass.input_model = input_model
            return subclass
        return decorator
