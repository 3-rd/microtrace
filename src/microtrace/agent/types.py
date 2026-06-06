"""Agent-level exceptions and types"""


class AgentError(Exception):
    """Agent 操作异常基类"""
    pass


class ToolExecutionError(AgentError):
    """工具执行失败"""
    pass


class LLMError(AgentError):
    """LLM 调用失败"""
    pass


class StateTransitionError(AgentError):
    """状态转换失败"""
    pass


class CompactionError(AgentError):
    """Compaction 操作失败"""
    pass
