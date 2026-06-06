"""全部 Pydantic data models（SPEC.md §3.2）"""
from __future__ import annotations

import uuid
from enum import Enum
import time as _time
from datetime import datetime
from typing import Literal
from pydantic import BaseModel, Field


# ── Enums ──────────────────────────────────────────────────────

class State(str, Enum):
    """5 显式状态"""
    INTAKE = "INTAKE"
    INVESTIGATE = "INVESTIGATE"
    ASK_USER = "ASK_USER"
    CONCLUDE = "CONCLUDE"
    EXIT = "EXIT"


class SessionStatus(str, Enum):
    """Session 持久化状态"""
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    ABANDONED = "abandoned"


class EvidenceSource(str, Enum):
    """证据来源"""
    CODE = "code"
    LOG = "log"
    STACK = "stack"
    TOOL_OUTPUT = "tool_output"
    ERROR = "error"
    USER = "user"


class EvidenceImportance(str, Enum):
    """证据重要性"""
    CRITICAL = "critical"
    SUPPORTING = "supporting"
    BACKGROUND = "background"


class ContentType(str, Enum):
    """证据内容类型"""
    CRITICAL = "critical"
    COMPRESSIBLE = "compressible"


class StreamEventType(str, Enum):
    """LLM 流式事件类型（10 种）"""
    START = "start"
    REASONING_DELTA = "reasoning-delta"
    TEXT_DELTA = "text-delta"
    TOOL_INPUT_START = "tool-input-start"
    TOOL_CALL = "tool-call"
    TOOL_RESULT = "tool-result"
    JUDGMENT_UPDATE = "judgment-update"
    ASK_USER = "ask-user"
    CONCLUDE = "conclude"
    ERROR = "error"


class ToolState(str, Enum):
    """工具 4 态子状态机"""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    ERROR = "error"


class JudgmentCategory(str, Enum):
    """业务报错三分类"""
    A = "A"
    B = "B"
    C = "C"
    UNKNOWN = "UNKNOWN"


# ── Core Models ────────────────────────────────────────────────

class StackFrame(BaseModel):
    """堆栈帧"""
    class_name: str
    method_name: str
    file_name: str
    line_number: int

    def to_short_string(self) -> str:
        return f"{self.class_name}.{self.method_name}() at {self.file_name}:{self.line_number}"


class Problem(BaseModel):
    """
    问题陈述（INTAKE 输出）
    """
    raw_input: str = Field(description="原始用户输入")
    error_type: str | None = Field(default=None, description="错误类型描述")
    stack_frames: list[StackFrame] = Field(default_factory=list, description="堆栈帧列表")
    log_snippets: list[str] = Field(default_factory=list, description="日志片段")
    timestamp: datetime | None = Field(default=None, description="问题发生时间")
    parse_error: str | None = Field(default=None, description="INTAKE 解析失败原因")


class Judgment(BaseModel):
    """
    当前判断（单例，随推理更新）
    """
    category: JudgmentCategory = Field(description="A=本产品Bug, B=下游报错, C=用法问题, UNKNOWN")
    confidence: float = Field(ge=0.0, le=1.0, description="置信度 0.0~1.0")
    one_line_reason: str = Field(description="一句话理由")
    reasoning: str = Field(description="当前轮详细推理")

    def to_brief(self) -> str:
        return f"{self.category}({self.confidence:.2f}): {self.one_line_reason}"


class Evidence(BaseModel):
    """
    证据（只增不减）
    """
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), description="唯一 ID")
    source: EvidenceSource = Field(description="来源")
    location: str = Field(description="位置描述")
    content: str = Field(description="截取后的内容")
    raw_content: str = Field(description="原始完整内容")
    content_type: ContentType = Field(
        default=ContentType.COMPRESSIBLE,
        description="critical=永不压缩, compressible=可压缩"
    )
    importance: EvidenceImportance = Field(
        default=EvidenceImportance.SUPPORTING,
        description="critical/supporting/background"
    )
    relevance: float = Field(
        default=0.5, ge=0.0, le=1.0,
        description="对当前判断 A/B/C 的帮助度"
    )
    compacted: bool = Field(default=False, description="是否已被 compaction PRUNE")
    preserved_lines: str = Field(default="", description="从 tool output 提取的关键行")
    discovered_at_iteration: int = Field(description="发现轮次")
    tool_name: str | None = Field(default=None, description="来源工具名")

    model_config = {"use_enum_values": True}


class ToolCall(BaseModel):
    """
    工具调用记录（用于 Doom Loop 检测）
    """
    name: str = Field(description="工具名")
    args: dict = Field(description="完整 args")
    args_summary: str = Field(description="参数摘要")
    output_summary: str = Field(description="输出摘要")
    output_raw: str | None = Field(default=None, description="原始输出")
    iteration: int = Field(description="调用轮次")
    state: ToolState = Field(default=ToolState.PENDING, description="工具状态")
    error: str | None = Field(default=None, description="错误信息")

    model_config = {"use_enum_values": True}


class UserReply(BaseModel):
    """用户对 ASK_USER 的回复"""
    question: str = Field(description="原始问题")
    answer: str = Field(description="用户回答")
    timestamp: float = Field(description="Unix timestamp")


class CompactionRecord(BaseModel):
    """Compaction 记录"""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    triggered_at_iteration: int = Field(description="在哪一轮触发")
    reason: Literal["auto_overflow", "manual"] = Field(description="触发原因")
    tokens_before: int = Field(description="压缩前估算 token")
    tokens_after: int = Field(description="压缩后估算 token")
    summary: str = Field(description="LLM 生成的压缩摘要")
    preserved_evidence_ids: list[str] = Field(
        default_factory=list,
        description="保留的 critical evidence ID"
    )
    pruned_count: int = Field(default=0, description="PRUNE 掉的 tool call 数")
    timestamp: float = Field(description="Unix timestamp")


class StreamEvent(BaseModel):
    """
    LLM 流式事件（10 种事件类型）
    """
    type: StreamEventType
    text: str | None = Field(default=None)
    reasoning_id: str | None = Field(default=None)
    tool_name: str | None = Field(default=None)
    tool_call_id: str | None = Field(default=None)
    tool_args: dict | None = Field(default=None)
    tool_output: str | None = Field(default=None)
    tool_error: str | None = Field(default=None)
    evidence_relevance: float | None = Field(default=None)
    evidence_importance: EvidenceImportance | None = Field(default=None)
    evidence_reason: str | None = Field(default=None)
    finish_reason: str | None = Field(default=None)
    tokens: int | None = Field(default=None)
    cost: float | None = Field(default=None)
    judgment_update: Judgment | None = Field(default=None)
    question: str | None = Field(default=None)
    conclusion: str | None = Field(default=None)
    error: str | None = Field(default=None)


class QuestionOption(BaseModel):
    """ASK_USER 多选选项"""
    label: str = Field(max_length=20, description="显示文本")
    description: str = Field(description="选项解释")


class QuestionPrompt(BaseModel):
    """ASK_USER 弹窗内容"""
    header: str = Field(max_length=30, description="短标签")
    question: str = Field(description="完整问题")
    options: list[QuestionOption] = Field(description="可用选项")
    multiple: bool = Field(default=False, description="是否多选")
    custom: bool = Field(default=True, description="允许自定义答案")


class AgentEvent(BaseModel):
    """事件溯源事件（append-only）"""
    type: str = Field(description="事件类型")
    data: dict = Field(default_factory=dict)
    timestamp: float = Field(description="Unix timestamp")
    iteration: int | None = Field(default=None)


class Context(BaseModel):
    """
    Agent 完整上下文（整个 session 的唯一数据容器）
    """
    problem: Problem | None = Field(default=None)
    current_judgment: Judgment = Field(
        default_factory=lambda: Judgment(
            category=JudgmentCategory.UNKNOWN,
            confidence=0.0,
            one_line_reason="尚未开始",
            reasoning=""
        )
    )
    judgment_history: list[Judgment] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    tool_history: list[ToolCall] = Field(default_factory=list)
    reasoning_trace: list[str] = Field(default_factory=list)
    MAX_REASONING_TRACE: int = Field(default=10)
    user_replies: list[UserReply] = Field(default_factory=list)
    compactions: list[CompactionRecord] = Field(default_factory=list)
    cumulative_tokens: int = Field(default=0)
    event_store: list[AgentEvent] = Field(default_factory=list)
    state: State = Field(default=State.INTAKE)
    iteration: int = Field(default=0)
    max_iterations: int = Field(default=8)
    user_interrupt: bool = Field(default=False)
    pending_question: QuestionPrompt | None = Field(default=None)
    final_output: str | None = Field(default=None)
    doom_loop_tool: str | None = Field(default=None)
    doom_loop_args: dict | None = Field(default=None)
    disabled_tools: set[str] = Field(default_factory=set)
    error: str | None = Field(default=None)
    session_id: str | None = Field(default=None)
    created_at: float | None = Field(default=None)

    def append_reasoning(self, msg: str) -> None:
        """追加推理记录"""
        self.reasoning_trace.append(msg)
        if len(self.reasoning_trace) > self.MAX_REASONING_TRACE:
            self.reasoning_trace = self.reasoning_trace[-self.MAX_REASONING_TRACE:]

    def update_judgment(self, new: Judgment) -> None:
        """更新 judgment（版本化）"""
        old = self.current_judgment
        self.current_judgment = new
        self.judgment_history.append(new)
        self.append_reasoning(
            f"[判断更新 #{len(self.judgment_history)}] "
            f"{old.category}→{new.category}, "
            f"confidence={old.confidence:.2f}→{new.confidence:.2f}"
        )

    def add_evidence(self, ev: Evidence) -> None:
        """追加证据"""
        self.evidence.append(ev)

    def add_tool_call(self, tc: ToolCall) -> None:
        """追加工具调用"""
        self.tool_history.append(tc)

    def append_event(self, event_type: str, data: dict, iteration: int | None = None) -> None:
        """追加事件（append-only）"""
        self.event_store.append(AgentEvent(
            type=event_type,
            data=data,
            timestamp=_time.time(),
            iteration=iteration or self.iteration,
        ))

    model_config = {"use_enum_values": True}
