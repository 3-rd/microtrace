"""全部 Pydantic data models（SPEC.md §3.2 + §1.4 Phase 1 六机制）"""
from __future__ import annotations

import uuid
from enum import Enum
import time as _time
from datetime import datetime
from typing import Literal
from pydantic import BaseModel, Field


# ═══════════════════════════════════════════════════════════════
# Phase 0 Enums（保留不变）
# ═══════════════════════════════════════════════════════════════

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
    A = "A"       # 本产品 Bug
    B = "B"       # 下游产品报错
    C = "C"       # 使用方法问题
    UNKNOWN = "UNKNOWN"


# ═══════════════════════════════════════════════════════════════
# Phase 1 新增 Enums（六机制专用）
# ═══════════════════════════════════════════════════════════════

class HypothesisStatus(str, Enum):
    """
    假设四态生命周期（机制 3：鉴别诊断）
    candidate → investigating → confirmed
    candidate → investigating → ruled_out
    """
    CANDIDATE = "candidate"           # 候选（LLM 提出，尚未验证）
    INVESTIGATING = "investigating"   # 调查中（当前聚焦）
    CONFIRMED = "confirmed"           # 已确认（有充分证据）
    RULED_OUT = "ruled_out"           # 已排除（证据否定）


class ConfidenceTier(str, Enum):
    """
    置信度分层（机制 4：规则引擎计算，非 LLM 自评）
    """
    CERTAIN = "certain"         # >0.9, 3+ independent evidence sources
    LIKELY = "likely"           # 0.7-0.9, 2+ evidence sources
    SUSPECTED = "suspected"     # 0.4-0.7, 1+ evidence source
    RULED_OUT = "ruled_out"     # disproven, evidence contradicts


class PatternStatus(str, Enum):
    """
    诊断模式生命周期（机制 6：跨 session 模式进化）
    active → active (success_count++)
    active → stale (N sessions unused)
    stale → archived (M sessions unused after stale)
    """
    ACTIVE = "active"       # 活跃使用中
    STALE = "stale"         # 长时间未匹配
    ARCHIVED = "archived"   # 已归档（不再注入 hint）


class GateResult(str, Enum):
    """逐跳验证 Gate 判定结果（机制 2）"""
    PASS = "pass"           # 证据充分，继续推进
    HOLD = "hold"           # 证据不足，留在当前 hop
    BACKTRACK = "backtrack" # 证据矛盾，回滚上一 hop
    FAIL = "fail"           # 致命矛盾，标记失败


# ═══════════════════════════════════════════════════════════════
# Phase 0 Core Models（保留不变）
# ═══════════════════════════════════════════════════════════════

class StackFrame(BaseModel):
    """堆栈帧"""
    class_name: str
    method_name: str
    file_name: str
    line_number: int

    def to_short_string(self) -> str:
        return f"at {self.class_name}.{self.method_name}({self.file_name}:{self.line_number})"


class Problem(BaseModel):
    """问题陈述（INTAKE 输出）"""
    raw_input: str = Field(description="原始用户输入")
    error_type: str | None = Field(default=None, description="错误类型描述")
    stack_frames: list[StackFrame] = Field(default_factory=list, description="堆栈帧列表")
    log_snippets: list[str] = Field(default_factory=list, description="日志片段")
    timestamp: datetime | None = Field(default=None, description="问题发生时间")
    parse_error: str | None = Field(default=None, description="INTAKE 解析失败原因")


class Evidence(BaseModel):
    """证据（只增不减）"""
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
    """工具调用记录（用于 Doom Loop 检测）"""
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
    """LLM 流式事件（10 种事件类型）"""
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
    hypothesis_update: dict | None = Field(default=None, description="[Phase 1] LLM 假设更新")
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


# ═══════════════════════════════════════════════════════════════
# Phase 1 新增 Models（六机制核心）
# ═══════════════════════════════════════════════════════════════

class Hypothesis(BaseModel):
    """
    单个诊断假设（机制 3：鉴别诊断）

    四态生命周期：
      candidate → investigating → confirmed  （证据充分）
      candidate → investigating → ruled_out   （证据否定）
    """
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), description="唯一 ID")
    statement: str = Field(description="假设陈述，如 'NPE 由 Feign 调用超时导致'")
    category: JudgmentCategory = Field(
        default=JudgmentCategory.UNKNOWN,
        description="归属分类 A/B/C"
    )
    status: HypothesisStatus = Field(
        default=HypothesisStatus.CANDIDATE,
        description="假设状态"
    )
    confidence: float = Field(
        default=0.5, ge=0.0, le=1.0,
        description="置信度 0.0~1.0"
    )
    evidence_for: list[str] = Field(
        default_factory=list,
        description="支持的 evidence ID 列表"
    )
    evidence_against: list[str] = Field(
        default_factory=list,
        description="否定的 evidence ID 列表"
    )
    ruled_out_reason: str | None = Field(
        default=None,
        description="排除原因（LLM 生成，自由文本）"
    )
    created_at_iteration: int = Field(default=0, description="首次提出轮次")
    updated_at_iteration: int = Field(default=0, description="最后更新轮次")

    def to_brief(self) -> str:
        return f"[{self.status.value}] {self.category.value}({self.confidence:.2f}): {self.statement[:80]}"

    def add_supporting_evidence(self, evidence_id: str) -> None:
        """添加支持证据"""
        if evidence_id not in self.evidence_for:
            self.evidence_for.append(evidence_id)

    def add_contradicting_evidence(self, evidence_id: str) -> None:
        """添加否定证据"""
        if evidence_id not in self.evidence_against:
            self.evidence_against.append(evidence_id)


class HypothesisSet(BaseModel):
    """
    假设集合（机制 3：鉴别诊断，替代 Phase 0 的单一 Judgment）

    两阶段流程：
      1. 展开（expand）：LLM 提出 2-4 个候选假设
      2. 排除（narrow）：逐个验证，排除不成立的
    """
    hypotheses: list[Hypothesis] = Field(
        default_factory=list,
        description="全部假设"
    )
    current_focus: str | None = Field(
        default=None,
        description="当前正在调查的 hypothesis ID"
    )

    @property
    def confirmed(self) -> list[Hypothesis]:
        return [h for h in self.hypotheses if h.status == HypothesisStatus.CONFIRMED]

    @property
    def investigating(self) -> list[Hypothesis]:
        return [h for h in self.hypotheses if h.status == HypothesisStatus.INVESTIGATING]

    @property
    def candidates(self) -> list[Hypothesis]:
        return [h for h in self.hypotheses if h.status == HypothesisStatus.CANDIDATE]

    @property
    def ruled_out(self) -> list[Hypothesis]:
        return [h for h in self.hypotheses if h.status == HypothesisStatus.RULED_OUT]

    @property
    def best(self) -> Hypothesis | None:
        """得分最高的已确认假设"""
        confirmed = self.confirmed
        if not confirmed:
            return None
        return max(confirmed, key=lambda h: h.confidence)

    def get(self, hypothesis_id: str) -> Hypothesis | None:
        for h in self.hypotheses:
            if h.id == hypothesis_id:
                return h
        return None

    def add(self, hypothesis: Hypothesis) -> None:
        self.hypotheses.append(hypothesis)

    def set_focus(self, hypothesis_id: str) -> None:
        h = self.get(hypothesis_id)
        if h and h.status == HypothesisStatus.CANDIDATE:
            h.status = HypothesisStatus.INVESTIGATING
        self.current_focus = hypothesis_id

    def confirm(self, hypothesis_id: str) -> None:
        h = self.get(hypothesis_id)
        if h:
            h.status = HypothesisStatus.CONFIRMED

    def rule_out(self, hypothesis_id: str, reason: str) -> None:
        h = self.get(hypothesis_id)
        if h:
            h.status = HypothesisStatus.RULED_OUT
            h.ruled_out_reason = reason

    def to_brief(self) -> str:
        parts = []
        for h in self.hypotheses:
            marker = "→" if h.id == self.current_focus else " "
            parts.append(f"  {marker} {h.to_brief()}")
        return "\n".join(parts) if parts else "（无假设）"


class DiagnosisClaim(BaseModel):
    """
    诊断声明（机制 1：证据锚定）

    硬约束：evidence_refs 不能为空。
    通过 validate_claim() 验证后才可转为 final_output。
    """
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    category: JudgmentCategory = Field(description="归属 A/B/C")
    statement: str = Field(description="诊断结论")
    evidence_refs: list[str] = Field(
        default_factory=list,
        description="引用的 evidence ID 列表（硬约束：不能为空）"
    )
    hypothesis_ref: str | None = Field(
        default=None,
        description="对应的 hypothesis ID"
    )
    confidence_tier: ConfidenceTier = Field(
        default=ConfidenceTier.SUSPECTED,
        description="规则引擎计算的置信度分层"
    )
    created_at_iteration: int = Field(default=0)

    @property
    def evidence_count(self) -> int:
        return len(self.evidence_refs)

    @property
    def is_valid(self) -> bool:
        """最基本约束：必须引用至少 1 条证据"""
        return len(self.evidence_refs) > 0


class DiagnosisPattern(BaseModel):
    """
    诊断模式（机制 6：跨 session 模式进化）

    三态生命周期：active → stale → archived
    """
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    symptom_signature: str = Field(
        description="症状签名（LLM 生成的结构化摘要）"
    )
    error_type: str | None = Field(
        default=None,
        description="错误类型（结构化字段，用于规则匹配）"
    )
    stack_top_class: str | None = Field(
        default=None,
        description="栈顶类名（结构化字段，用于规则匹配）"
    )
    diagnosis_template: str = Field(
        description="LLM 生成的诊断模板（注入 hint 用）"
    )
    category: JudgmentCategory = Field(
        default=JudgmentCategory.UNKNOWN,
        description="最终归属 A/B/C"
    )
    status: PatternStatus = Field(
        default=PatternStatus.ACTIVE,
        description="模式状态"
    )
    success_count: int = Field(
        default=0,
        description="成功匹配并正确诊断的次数"
    )
    failure_count: int = Field(
        default=0,
        description="匹配了但诊断错误的次数"
    )
    created_at: float = Field(description="创建时间（Unix timestamp）")
    last_matched_at: float | None = Field(
        default=None,
        description="最后匹配时间"
    )
    source_session_id: str | None = Field(
        default=None,
        description="来源 session ID"
    )

    @property
    def accuracy(self) -> float:
        total = self.success_count + self.failure_count
        if total == 0:
            return 0.0
        return self.success_count / total

    def record_success(self) -> None:
        self.success_count += 1
        self.status = PatternStatus.ACTIVE  # 成功匹配 → 保持 active

    def record_failure(self) -> None:
        self.failure_count += 1

    def mark_stale(self) -> None:
        """长时间未匹配 → stale"""
        self.status = PatternStatus.STALE

    def archive(self) -> None:
        """stale 后进一步降级 → archived"""
        self.status = PatternStatus.ARCHIVED


# ═══════════════════════════════════════════════════════════════
# Context（Phase 1 扩展）
# ═══════════════════════════════════════════════════════════════

class Context(BaseModel):
    """
    Agent 完整上下文（整个 session 的唯一数据容器）

    Phase 1 变更：
      - 新增 hypotheses: HypothesisSet（替代 current_judgment + judgment_history）
      - 新增 diagnosis_claim: DiagnosisClaim | None（证据锚定）
      - 新增 confidence_tier: ConfidenceTier（规则引擎计算）
      - 新增 current_hop: int（逐跳验证 Gate 跟踪）
      - 新增 matched_patterns: list[str]（匹配到的诊断模式 ID）
      - 新增 dry_run: bool + trace_dir（§1.6 Dry-run Mode）
    """
    # ── Problem ──
    problem: Problem | None = Field(default=None)

    # ── Phase 1: HypothesisSet（替代 Judgment 单例）──
    hypotheses: HypothesisSet = Field(default_factory=HypothesisSet)

    # ── Phase 1: DiagnosisClaim ──
    diagnosis_claim: DiagnosisClaim | None = Field(default=None)

    # ── Phase 1: ConfidenceTier ──
    confidence_tier: ConfidenceTier = Field(default=ConfidenceTier.SUSPECTED)

    # ── Evidence ──
    evidence: list[Evidence] = Field(default_factory=list)

    # ── Tool History ──
    tool_history: list[ToolCall] = Field(default_factory=list)

    # ── Reasoning Trace ──
    reasoning_trace: list[str] = Field(default_factory=list)
    MAX_REASONING_TRACE: int = Field(default=10)

    # ── User Replies ──
    user_replies: list[UserReply] = Field(default_factory=list)

    # ── Compaction ──
    compactions: list[CompactionRecord] = Field(default_factory=list)
    cumulative_tokens: int = Field(default=0)

    # ── Event Sourcing ──
    event_store: list[AgentEvent] = Field(default_factory=list)

    # ── State Machine ──
    state: State = Field(default=State.INTAKE)

    # ── Loop Control ──
    iteration: int = Field(default=0)
    max_iterations: int = Field(default=8)
    user_interrupt: bool = Field(default=False)
    pending_question: QuestionPrompt | None = Field(default=None)
    final_output: str | None = Field(default=None)

    # ── Doom Loop ──
    doom_loop_tool: str | None = Field(default=None)
    doom_loop_args: dict | None = Field(default=None)
    disabled_tools: set[str] = Field(default_factory=set)

    # ── Error ──
    error: str | None = Field(default=None)

    # ── Session Metadata ──
    session_id: str | None = Field(default=None)
    created_at: float | None = Field(default=None)

    # ── Phase 1: Hop Gate（机制 2）──
    current_hop: int = Field(default=0, description="当前推理跳数")

    # ── Phase 1: Pattern Matching（机制 6）──
    matched_patterns: list[str] = Field(
        default_factory=list,
        description="匹配到的 DiagnosisPattern ID 列表"
    )

    # ── Phase 1: Dry-run Mode（§1.6）──
    dry_run: bool = Field(default=False, description="是否 dry-run 模式")
    trace_dir: str | None = Field(default=None, description="trace 文件输出目录")

    # ── Helper Methods ─────────────────────────────────────

    def append_reasoning(self, msg: str) -> None:
        """追加推理记录"""
        self.reasoning_trace.append(msg)
        if len(self.reasoning_trace) > self.MAX_REASONING_TRACE:
            self.reasoning_trace = self.reasoning_trace[-self.MAX_REASONING_TRACE:]

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

    def get_evidence_by_id(self, evidence_id: str) -> Evidence | None:
        """按 ID 查找 evidence"""
        for ev in self.evidence:
            if ev.id == evidence_id:
                return ev
        return None

    # ── Phase 1: Hypothesis Helpers ───────────────────────

    def add_hypothesis(self, hypothesis: Hypothesis) -> None:
        """添加假设到集合"""
        self.hypotheses.add(hypothesis)
        self.append_reasoning(
            f"[假设新增] {hypothesis.status.value} {hypothesis.statement[:80]}"
        )

    def update_hypothesis(self, hypothesis_id: str, **kwargs) -> Hypothesis | None:
        """更新假设字段"""
        h = self.hypotheses.get(hypothesis_id)
        if h:
            for key, value in kwargs.items():
                if hasattr(h, key):
                    setattr(h, key, value)
            h.updated_at_iteration = self.iteration
            self.append_reasoning(f"[假设更新] {h.to_brief()}")
        return h

    def set_diagnosis_claim(self, claim: DiagnosisClaim) -> None:
        """设置诊断声明"""
        self.diagnosis_claim = claim
        self.append_reasoning(
            f"[声明设置] {claim.category.value} "
            f"tier={claim.confidence_tier.value} "
            f"evidence_refs={len(claim.evidence_refs)}"
        )

    def increment_hop(self) -> int:
        """递增推理跳数，返回新值"""
        self.current_hop += 1
        self.append_reasoning(f"[Hop {self.current_hop}] 进入新推理跳")
        return self.current_hop

    model_config = {"use_enum_values": True}
