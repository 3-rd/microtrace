# SPEC.md - microtrace Phase 0 Implementation Specification

> 📋 Source of truth for code. For "why", see DESIGN.md. For "what/who", see VISION.md.

---

## 1. Goals & Non-Goals

### 1.1 Goals

| Goal | Reference |
|------|-----------|
| 帮助 VNFM 维护工程师定位 Java 多微服务问题（业务报错） | VISION.md §1 |
| 严格基于事实的推理——每条结论必须能引用证据 | VISION.md §3 |
| REPL 多轮对话形态——不是 one-shot CLI | VISION.md §5 |
| Phase 0 最小集：REPL + HTTP API + 4 个工具 | VISION.md §6 |
| 跨平台支持（Windows / macOS / Linux） | DESIGN.md §12 |

### 1.2 Non-Goals

| Non-Goal | Reference |
|----------|-----------|
| 知识库 / RAG（Phase 0 纯代码+日志推理） | VISION.md §5 |
| 多语言支持（只 Java） | VISION.md §5 |
| TUI app / Web UI（Phase 1+） | VISION.md §5 |
| 自动修复 | VISION.md §5 |
| 多 LLM 路由（只 MiniMax） | VISION.md §5 |
| 性能问题 / OOM / 重启问题类型（Phase 1+） | VISION.md §4 |
| RAG / Case store | VISION.md §5 |

### 1.3 Phase 0 Deliverables

- `microtrace` CLI 命令（基于 Typer）
- REPL 界面（基于 prompt_toolkit + rich）
- FastAPI HTTP 服务（`/chat`, `/state`, `/evidence`, `/save`）
- 4 个工具：read_file / search_logs / find_class / parse_stack_trace
- SQLite session 持久化
- 完整状态机（5 态 + 事件溯源）
- Compaction 机制（PRUNE + SUMMARY）
- Doom Loop 检测（3 次精确匹配）

> 参考 VISION.md §6，DESIGN.md §6

---

## 2. Architecture Overview

### 2.1 Module Structure

```
microtrace/
├── src/
│   └── microtrace/
│       ├── __main__.py           # 入口：python -m microtrace
│       ├── cli.py                # Typer CLI 入口
│       ├── agent/
│       │   ├── __init__.py
│       │   ├── state.py          # State 枚举 + 5 态 handler
│       │   ├── events.py         # 事件类型 + EventStore
│       │   ├── loop.py           # run_session + agent_iteration（双层结构）
│       │   ├── doom_loop.py      # Doom Loop 检测
│       │   └── types.py          # AgentError 等
│       ├── tools/
│       │   ├── __init__.py
│       │   ├── base.py           # Tool.define 模式 + ToolResult
│       │   ├── read_file.py
│       │   ├── search_logs.py
│       │   ├── find_class.py
│       │   └── parse_stack_trace.py
│       ├── llm/
│       │   ├── __init__.py
│       │   ├── client.py         # LLMClient Protocol
│       │   └── minimax.py       # MiniMaxClient 实现
│       ├── context/
│       │   ├── __init__.py
│       │   ├── models.py         # Problem / Judgment / Evidence / ToolCall / Context
│       │   ├── compaction.py     # CompactionRecord + compact()
│       │   └── prompt.py         # _assemble_prompt + 5 条结构规则
│       ├── repl/
│       │   ├── __init__.py
│       │   ├── main.py           # REPL 入口 + _setup_windows_console
│       │   ├── renderer.py       # Rich 渲染 + 状态 banner
│       │   └── commands.py      # /status /evidence /save /clear /config /exit
│       ├── http/
│       │   ├── __init__.py
│       │   └── api.py            # FastAPI 路由
│       ├── persistence/
│       │   ├── __init__.py
│       │   └── sqlite.py         # SQLite schema + save/load
│       └── config.py             # platformdirs 路径 + config.yaml 加载
├── prompts/
│   └── agent.md                  # Master prompt（8 sections）
├── tests/
│   ├── test_state_machine.py
│   ├── test_loop.py
│   ├── test_doom_loop.py
│   ├── test_compaction.py
│   ├── test_tools.py
│   └── test_persistence.py
├── docs/
│   ├── VISION.md
│   ├── DESIGN.md
│   └── SPEC.md                  # 本文档
└── pyproject.toml
```

### 2.2 Data Flow Diagram

```
用户输入（REPL / HTTP）
    │
    ▼
┌─────────────────────────────────────────────────────────────┐
│                    run_session(ctx, llm)                     │
│                    （外层 driver，显式循环）                    │
│  ┌──────────────────────────────────────────────────────┐  │
│  │                  agent_iteration(ctx, llm)            │  │
│  │                  （内层 processor，单次 stream）        │  │
│  │                                                       │  │
│  │  1. 退出条件检查（iter > max / doom_loop / overflow） │  │
│  │  2. _assemble_prompt(ctx, tools)                     │  │
│  │  3. llm.stream(prompt, tools) → AsyncIterator[Event]  │  │
│  │  4. 事件处理（text-delta / reasoning-delta / tool-*)  │  │
│  │  5. 工具并行执行（asyncio.gather，return_exceptions）  │  │
│  │  6. evidence 追加 / judgment 更新                     │  │
│  │  7. overflow 检测 → compact()                        │  │
│  │  8. save_context_to_sqlite(ctx)                      │  │
│  └──────────────────────────────────────────────────────┘  │
│                           │                                 │
│         ┌─────────────────┼─────────────────┐             │
│         ▼                 ▼                 ▼             │
│   ┌──────────┐    ┌──────────────┐   ┌──────────┐        │
│   │INTAKE    │    │ INVESTIGATE  │   │ASK_USER  │        │
│   │(解析输入) │    │ (主循环)     │   │(等用户)   │        │
│   └────┬─────┘    └──────┬───────┘   └────┬─────┘        │
│        │                 │                 │              │
│        └─────────────────┼─────────────────┘              │
│                          ▼                                │
│                   ┌──────────┐                          │
│                   │ CONCLUDE │                          │
│                   │(格式化输出)│                          │
│                   └──────────┘                          │
└─────────────────────────────────────────────────────────────┘
                          │
                          ▼
                    最终输出 / 结论
```

### 2.3 Module Dependency Graph

```
cli.py
  └─► REPL (repl/main.py)
        └─► run_session() [agent/loop.py]
              ├─► agent_iteration() [agent/loop.py]
              │     ├─► _assemble_prompt() [context/prompt.py]
              │     │     └─► read prompts/agent.md
              │     ├─► llm.stream() [llm/minimax.py]
              │     │     └─► MiniMaxClient (implements LLMClient)
              │     ├─► Tool.execute() [tools/*.py]
              │     │     └─► Tool.define() [tools/base.py]
              │     ├─► _check_doom_loop() [agent/doom_loop.py]
              │     ├─► _check_overflow() [context/compaction.py]
              │     └─► compact() [context/compaction.py]
              ├─► State handlers [agent/state.py]
              └─► save/load [persistence/sqlite.py]
                    └─► sqlite3 (stdlib)

http/api.py
  └─► run_session() [agent/loop.py]（同上）
```

> 参考 DESIGN.md §2，§6

---

## 3. Data Model

所有数据结构使用 Pydantic v2 (`BaseModel`)。每个模型都有完整字段定义。

### 3.1 Enums

```python
# src/microtrace/agent/state.py
from enum import Enum, auto


class State(str, Enum):
    """5 显式状态（REPL UI 友好）"""
    INTAKE = "INTAKE"       # 解析原始输入
    INVESTIGATE = "INVESTIGATE"  # 主推理循环
    ASK_USER = "ASK_USER"   # 等待用户补料（硬阻塞）
    CONCLUDE = "CONCLUDE"   # 格式化结论
    EXIT = "EXIT"          # 异常退出


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
    """LLM 评：证据重要性（影响 prompt 截取）"""
    CRITICAL = "critical"      # 永不压缩
    SUPPORTING = "supporting"  # 可压缩
    BACKGROUND = "background"  # 背景信息


class ContentType(str, Enum):
    """问题定位专用：evidence 内容分类（决定压缩策略）"""
    CRITICAL = "critical"      # 永不压缩
    COMPRESSIBLE = "compressible"  # 可压缩


class StreamEventType(str, Enum):
    """LLM 流式事件类型（**精简后 10 种**）"""
    START = "start"                       # 流开始
    REASONING_DELTA = "reasoning-delta"  # 推理增量（合并 start/delta/end）
    TEXT_DELTA = "text-delta"            # 文本增量（合并 start/delta/end）
    TOOL_INPUT_START = "tool-input-start"  # 工具参数收集中
    TOOL_CALL = "tool-call"               # 工具调用完整生成
    TOOL_RESULT = "tool-result"           # 工具结果
    JUDGMENT_UPDATE = "judgment-update"   # 判断更新（microtrace 独有）
    ASK_USER = "ask-user"                # 用户询问
    CONCLUDE = "conclude"                 # 结论
    ERROR = "error"                       # 错误


class ToolState(str, Enum):
    """工具 4 态子状态机"""
    PENDING = "pending"     # 参数收集中
    RUNNING = "running"    # 执行中
    COMPLETED = "completed"  # 成功
    ERROR = "error"        # 失败


class JudgmentCategory(str, Enum):
    """业务报错三分类"""
    A = "A"  # 本产品 Bug
    B = "B"  # 下游产品报错
    C = "C"  # 使用方法问题
    UNKNOWN = "UNKNOWN"
```

### 3.2 Core Models

```python
# src/microtrace/context/models.py
from __future__ import annotations
import uuid
from datetime import datetime
from typing import Annotated, Literal
from pydantic import BaseModel, Field


# ── 3.2.1 Problem ──────────────────────────────────────────────

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
    字段全部有默认值（INTAKE 解析失败时部分字段可为空）
    """
    raw_input: str = Field(description="原始用户输入")
    error_type: str | None = Field(default=None, description="错误类型描述")
    stack_frames: list[StackFrame] = Field(default_factory=list, description="堆栈帧列表")
    log_snippets: list[str] = Field(default_factory=list, description="日志片段")
    timestamp: datetime | None = Field(default=None, description="问题发生时间")
    # INTAKE 解析失败时的降级处理
    parse_error: str | None = Field(default=None, description="INTAKE 解析失败原因")


# ── 3.2.2 Judgment ─────────────────────────────────────────────

class Judgment(BaseModel):
    """
    当前判断（单例，随推理更新）
    全部版本存在 judgment_history，LLM 只看 current_judgment
    """
    category: JudgmentCategory = Field(description="A=本产品Bug, B=下游报错, C=用法问题, UNKNOWN")
    confidence: float = Field(ge=0.0, le=1.0, description="置信度 0.0~1.0")
    one_line_reason: str = Field(description="一句话理由")
    reasoning: str = Field(description="当前轮详细推理（不累积，历史在 judgment_history）")

    def to_brief(self) -> str:
        return f"{self.category}({self.confidence:.2f}): {self.one_line_reason}"


# ── 3.2.3 Evidence ─────────────────────────────────────────────

class Evidence(BaseModel):
    """
    证据（只增不减，截取在 prompt 层做）
    microtrace 独有：content_type 字段（5 条结构规则自动判定）
    OpenCode 通用：compacted 字段（标记被 PRUNE 过）
    """
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), description="唯一 ID")
    source: EvidenceSource = Field(description="来源")
    location: str = Field(description="位置描述（如 file:line 或 log:timestamp）")
    content: str = Field(description="截取后的内容（prompt 用这个）")
    raw_content: str = Field(description="原始完整内容（用于压缩评估）")

    # microtrace 独有：结构规则自动判定
    content_type: ContentType = Field(
        default=ContentType.COMPRESSIBLE,
        description="critical=永不压缩, compressible=可压缩（5 条结构规则）"
    )

    # LLM 评（只影响排序和 prompt 截取优先级）
    importance: EvidenceImportance = Field(
        default=EvidenceImportance.SUPPORTING,
        description="critical/supporting/background（LLM 自评）"
    )
    relevance: float = Field(
        default=0.5,
        ge=0.0, le=1.0,
        description="对当前判断 A/B/C 的帮助度（LLM 自评）"
    )

    # OpenCode 通用：标记被 PRUNE 过
    compacted: bool = Field(default=False, description="是否已被 compaction PRUNE")

    # microtrace 独有：保留关键行原文（即使 compacted）
    preserved_lines: str = Field(default="", description="从 tool output 提取的关键行")

    discovered_at_iteration: int = Field(description="发现轮次")
    tool_name: str | None = Field(default=None, description="来源工具名")

    model_config = {"use_enum_values": True}


# ── 3.2.4 ToolCall ─────────────────────────────────────────────

class ToolCall(BaseModel):
    """
    工具调用记录（用于 Doom Loop 检测）
    """
    name: str = Field(description="工具名")
    args: dict = Field(description="完整 args（JSON 序列化用于 Doom Loop 匹配）")
    args_summary: str = Field(description="参数摘要（显示用）")
    output_summary: str = Field(description="输出摘要（显示用）")
    output_raw: str | None = Field(default=None, description="原始输出")
    iteration: int = Field(description="调用轮次")
    state: ToolState = Field(default=ToolState.PENDING, description="工具状态")
    error: str | None = Field(default=None, description="错误信息")

    model_config = {"use_enum_values": True}


# ── 3.2.5 UserReply ────────────────────────────────────────────

class UserReply(BaseModel):
    """用户对 ASK_USER 的回复"""
    question: str = Field(description="原始问题")
    answer: str = Field(description="用户回答")
    timestamp: float = Field(description="Unix timestamp")


# ── 3.2.6 CompactionRecord ─────────────────────────────────────

class CompactionRecord(BaseModel):
    """
    Compaction 记录
    """
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


# ── 3.2.7 StreamEvent ─────────────────────────────────────────

class StreamEvent(BaseModel):
    """
    LLM 流式事件（10 种事件类型）
    对应 OpenCode processor.ts 的 13 种事件
    """
    type: StreamEventType
    # text/reasoning
    text: str | None = Field(default=None)
    reasoning_id: str | None = Field(default=None)
    # tool
    tool_name: str | None = Field(default=None)
    tool_call_id: str | None = Field(default=None)
    tool_args: dict | None = Field(default=None)
    tool_output: str | None = Field(default=None)
    tool_error: str | None = Field(default=None)
    # evidence evaluation
    evidence_relevance: float | None = Field(default=None)
    evidence_importance: EvidenceImportance | None = Field(default=None)
    evidence_reason: str | None = Field(default=None)
    # step
    finish_reason: str | None = Field(default=None)
    tokens: int | None = Field(default=None)
    cost: float | None = Field(default=None)
    # judgment
    judgment_update: Judgment | None = Field(default=None)
    # question
    question: str | None = Field(default=None)
    # conclusion
    conclusion: str | None = Field(default=None)
    # error
    error: str | None = Field(default=None)


# ── 3.2.8 QuestionPrompt / QuestionOption ─────────────────────

class QuestionOption(BaseModel):
    """ASK_USER 多选选项"""
    label: str = Field(max_length=20, description="显示文本（1-20 字）")
    description: str = Field(description="选项解释")


class QuestionPrompt(BaseModel):
    """
    ASK_USER 弹窗内容
    对应 OpenCode src/question/index.ts 的 Prompt schema
    """
    header: str = Field(max_length=30, description="短标签（max 30 字）")
    question: str = Field(description="完整问题")
    options: list[QuestionOption] = Field(description="可用选项")
    multiple: bool = Field(default=False, description="是否多选")
    custom: bool = Field(default=True, description="允许自定义答案")


# ── 3.2.9 AgentEvent ──────────────────────────────────────────

class AgentEvent(BaseModel):
    """
    事件溯源事件（append-only）
    """
    type: str = Field(description="事件类型，如 state.entered / tool.called / step.finished")
    data: dict = Field(default_factory=dict)
    timestamp: float = Field(description="Unix timestamp")
    iteration: int | None = Field(default=None)


# ── 3.2.10 Context ─────────────────────────────────────────────

class Context(BaseModel):
    """
    Agent 完整上下文（整个 session 的唯一数据容器）
    所有字段有默认值，支持部分字段为空的降级场景
    """
    # Problem（INTAKE 输出）
    problem: Problem | None = Field(default=None)

    # Judgment（版本化）
    current_judgment: Judgment = Field(
        default_factory=lambda: Judgment(
            category=JudgmentCategory.UNKNOWN,
            confidence=0.0,
            one_line_reason="尚未开始",
            reasoning=""
        )
    )
    judgment_history: list[Judgment] = Field(default_factory=list)

    # Evidence（只增不减）
    evidence: list[Evidence] = Field(default_factory=list)

    # Tool History（用于 Doom Loop 检测）
    tool_history: list[ToolCall] = Field(default_factory=list)

    # Reasoning Trace（只保留最近 MAX_REASONING_TRACE 条）
    reasoning_trace: list[str] = Field(default_factory=list)
    MAX_REASONING_TRACE: int = Field(default=10)

    # User Replies（ASK_USER 场景）
    user_replies: list[UserReply] = Field(default_factory=list)

    # Compaction 记录
    compactions: list[CompactionRecord] = Field(default_factory=list)
    cumulative_tokens: int = Field(default=0, description="累计 token 数（用于 overflow 检测）")

    # 事件溯源（append-only）
    event_store: list[AgentEvent] = Field(default_factory=list)

    # 状态机
    state: State = Field(default=State.INTAKE)

    # Loop 控制
    iteration: int = Field(default=0)
    max_iterations: int = Field(default=8)
    user_interrupt: bool = Field(default=False)
    pending_question: QuestionPrompt | None = Field(default=None)
    final_output: str | None = Field(default=None)

    # Doom Loop
    doom_loop_tool: str | None = Field(default=None)
    doom_loop_args: dict | None = Field(default=None)

    # Disabled tools（Doom Loop reject 后注入）
    disabled_tools: set[str] = Field(default_factory=set)

    # Error
    error: str | None = Field(default=None)

    # Session 元数据
    session_id: str | None = Field(default=None)
    created_at: float | None = Field(default=None)

    # ── Helper methods ─────────────────────────────────────────

    def append_reasoning(self, msg: str) -> None:
        """追加推理记录（超过 MAX_REASONING_TRACE 条时截断）"""
        self.reasoning_trace.append(msg)
        if len(self.reasoning_trace) > self.MAX_REASONING_TRACE:
            self.reasoning_trace = self.reasoning_trace[-self.MAX_REASONING_TRACE:]

    def update_judgment(self, new: Judgment) -> None:
        """
        更新 judgment（版本化）
        current_judgment 覆盖，judgment_history 追加
        LLM 只看 current_judgment，REPL 可以看 judgment_history
        """
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
            timestamp=__import__("time").time(),
            iteration=iteration or self.iteration,
        ))

    model_config = {"use_enum_values": True}


# ── 3.2.11 Config ──────────────────────────────────────────────

class AgentConfig(BaseModel):
    """Agent 配置（config.yaml 的 agent 小节）"""
    max_iterations: int = Field(default=8, ge=1, le=100)
    compaction_buffer: int = Field(
        default=20_000,
        description="固定 20K buffer（与 OpenCode 一致）"
    )


class ToolsConfig(BaseModel):
    """Tools 配置（config.yaml 的 tools 小节）"""
    log_dirs: list[str] = Field(
        default_factory=lambda: [
            # 跨平台默认（按顺序搜索）
            "/var/log",                          # Linux 常见
            "/var/log/vnfm",                     # VNFM 业务日志（Linux）
            "C:/ProgramData/VNFM/logs",          # Windows 常见
            "C:/Windows/System32/winevt/Logs",   # Windows 事件日志
        ],
        description="search_logs 工具搜索的目录列表（按顺序尝试）"
    )
    java_source_roots: list[str] = Field(
        default_factory=list,
        description="Java 源码根目录（find_class 工具搜索）"
    )
    max_file_size: int = Field(
        default=10_000_000,
        description="read_file 最大文件大小（字节），超过报错"
    )


class LLMConfig(BaseModel):
    """LLM 配置（config.yaml 的 llm 小节）"""
    provider: Literal["minimax"] = Field(default="minimax")
    model: str = Field(default="MiniMax-M3-highspeed")
    api_key: str | None = Field(default=None)
    base_url: str = Field(default="https://api.minimax.chat/v1")
    timeout: float = Field(default=120.0, description="LLM 调用超时（秒）")


class Config(BaseModel):
    """完整配置"""
    agent: AgentConfig = Field(default_factory=AgentConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    # Windows 兼容：platformdirs 路径由代码计算，不在 config 里

    @classmethod
    def load(cls, path: str | None = None) -> Config:
        """从 YAML 文件加载配置（不存在则返回默认）"""
        import yaml
        from pathlib import Path
        if path is None:
            from microtrace.config import get_config_path
            path = str(get_config_path())
        p = Path(path)
        if not p.exists():
            return cls()
        with open(p) as f:
            data = yaml.safe_load(f) or {}
        return cls.model_validate(data)

    def save(self, path: str | None = None) -> None:
        """保存配置到 YAML 文件"""
        import yaml
        from pathlib import Path
        if path is None:
            from microtrace.config import get_config_path
            path = str(get_config_path())
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w") as f:
            yaml.dump(self.model_dump(exclude_none=True), f, allow_unicode=True)
```

> 参考 DESIGN.md §3，§4，§5

---

## 4. Module Specifications

### 4.1 State Machine

#### 4.1.1 5 个 State 完整定义

```python
# src/microtrace/agent/state.py

class StateHandler:
    """
    状态 handler（enter/tick/exit 模式）
    每个状态实现 enter / tick / exit 三个方法
    """

    @staticmethod
    async def enter(ctx: Context, from_state: State | None = None) -> None:
        """进入状态时执行"""
        ctx.append_reasoning(f"[STATE→{ctx.state.value}] enter")
        ctx.append_event("state.entered", {
            "state": ctx.state.value,
            "from_state": from_state.value if from_state else None
        })
        # 审计修复 1：ASK_USER 进入时立即 save
        if ctx.state == State.ASK_USER:
            from microtrace.persistence.sqlite import save_context_to_sqlite
            from microtrace.config import get_db_path
            save_context_to_sqlite(ctx, str(get_db_path()))

    @staticmethod
    async def tick(ctx: Context) -> State | None:
        """
        态内主逻辑，返回 None=继续本态，返回目标态=切换
        INTAKE / ASK_USER / CONCLUDE / EXIT 是瞬态（tick 直接返回下一态）
        INVESTIGATE 是持续态（tick 返回 None，等待 loop 驱动）
        """
        return None  # 默认继续本态

    @staticmethod
    async def exit(ctx: Context, to_state: State, reason: str) -> None:
        """退出状态时执行"""
        ctx.append_reasoning(f"[STATE→{ctx.state.value}→{to_state.value}] exit, reason={reason}")
        ctx.append_event("state.exited", {
            "from": ctx.state.value,
            "to": to_state.value,
            "reason": reason
        })
        # 审计修复 1：ASK_USER 退出时也 save
        if ctx.state == State.ASK_USER:
            from microtrace.persistence.sqlite import save_context_to_sqlite
            from microtrace.config import get_db_path
            save_context_to_sqlite(ctx, str(get_db_path()))
```

#### 4.1.2 状态转换矩阵（19 条）

| # | From | Event | To | 触发条件 | 备注 |
|---|------|-------|----|---------|------|
| 1 | (start) | 用户输入 | INTAKE | 正常入口 | |
| 2 | (start) | 空输入 | **EXIT** | raw_input 为空 | **修复 3** |
| 3 | INTAKE | INTAKE 解析成功 | INVESTIGATE | problem 解析成功 | |
| 4 | INTAKE | INTAKE 解析降级 | INVESTIGATE(degraded) | problem 部分解析 | 仍继续 |
| 5 | INTAKE | INTAKE 致命错误 | **EXIT** | 解析彻底失败 | **修复 3** |
| 6 | INVESTIGATE | LLM conclude | CONCLUDE | stream 事件产生 conclusion | |
| 7 | INVESTIGATE | max_iter 到达 | CONCLUDE | iteration >= max_iterations | 修复 4：强制总结 |
| 8 | INVESTIGATE | 用户中断 | CONCLUDE | user_interrupt == True | |
| 9 | INVESTIGATE | LLM ask_user | ASK_USER | stream 事件产生 question | |
| 10 | INVESTIGATE | Doom Loop | ASK_USER | 3 次精确匹配 | 复用 ASK_USER 态 |
| 11 | INVESTIGATE | Overflow | COMPACTION→INVESTIGATE | isOverflow() == True | compact 后继续 |
| 12 | INVESTIGATE | tool_call | INVESTIGATE(自身) | 工具执行后继续 | |
| 13 | INVESTIGATE | judgment_update | INVESTIGATE(自身) | judgment 更新后继续 | |
| 14 | **ASK_USER 进入** | — | **(SAVE)** | — | **修复 1** |
| 15 | ASK_USER | 用户回复 | INVESTIGATE | 用户消息到达 | |
| 16 | ASK_USER | ctrl+c | (session paused) | 用户中断 | 状态保持 ASK_USER |
| 17 | ASK_USER | 用户 exit | CONCLUDE | 用户主动结束 | |
| 18 | **ASK_USER 退出** | — | **(SAVE)** | — | **修复 1** |
| 19 | (any) | fatal_error | EXIT | 捕获未处理异常 | 兜底 |

**边缘 case 处理**：
- INTAKE 失败 → EXIT（修复 3：空输入不浪费 8 轮）
- LLM 失败兜底 → judgment fallback（修复 4）
- Compaction LLM 失败 → truncated fallback（修复 4）
- 连续 Doom Loop → 信任用户，reasoning_trace 记录（接受为边缘 case）

#### 4.1.3 状态转换实现

```python
# src/microtrace/agent/state.py

async def transition(
    ctx: Context,
    target_state: State,
    reason: str,
    from_state: State | None = None
) -> None:
    """
    执行状态转换
    1. 调用当前态的 exit handler
    2. 切换 state
    3. 调用新态的 enter handler
    """
    if ctx.state == target_state:
        return  # 同一状态无需转换

    await StateHandler.exit(ctx, target_state, reason)
    previous = ctx.state
    ctx.state = target_state
    await StateHandler.enter(ctx, from_state=previous)
```

> 参考 DESIGN.md §3，§13

---

### 4.2 Agent Engine（双层结构）

#### 4.2.1 外层 Driver：run_session()

```python
# src/microtrace/agent/loop.py

async def run_session(
    initial_input: str,
    llm: LLMClient,
    tools: ToolRegistry,
    ctx: Context | None = None,
    session_id: str | None = None,
) -> Context:
    """
    外层 driver：显式循环，驱动整个 session
    - 管理 Context 生命周期
    - 调用 agent_iteration() 单次迭代
    - 处理状态转换
    - 管理 session 持久化

    Args:
        initial_input: 用户原始输入
        llm: LLM 客户端（实现 LLMClient Protocol）
        tools: 工具注册表
        ctx: 已有 Context（resume 时传入），None=新建
        session_id: session ID（resume 时传入）

    Returns:
        完整的 Context（包含 final_output）

    Raises:
        无（所有异常内部处理，ctx.error 记录）
    """
    import time

    # 初始化 Context
    if ctx is None:
        ctx = Context(
            session_id=session_id or _generate_session_id(),
            state=State.INTAKE,
            created_at=time.time(),
        )

    ctx.append_reasoning(f"[SESSION START] session_id={ctx.session_id}")

    # ── INTAKE 态 ──
    await StateHandler.enter(ctx, from_state=None)
    await _intake(ctx, initial_input, llm, tools)
    if ctx.state == State.EXIT:
        await _save_session(ctx)
        return ctx

    # ── INVESTIGATE 态：主循环 ──
    await transition(ctx, State.INVESTIGATE, reason="INTAKE 完成")

    while True:
        ctx.iteration += 1
        ctx.append_reasoning(f"[开始第 {ctx.iteration} 轮]")

        # 退出条件 1：max_iterations 到达
        if ctx.iteration > ctx.max_iterations:
            ctx.append_reasoning("[MAX_ITERATIONS] 到达，强制总结")
            await _force_max_iter_summary(ctx, llm)
            await transition(ctx, State.CONCLUDE, reason="max_iterations 到达")
            break

        # 退出条件 2：用户中断
        if ctx.user_interrupt:
            ctx.append_reasoning("[USER INTERRUPT]")
            await transition(ctx, State.CONCLUDE, reason="用户中断")
            break

        # 退出条件 3：已有 final_output（LLM 自决结束）
        if ctx.final_output:
            ctx.append_reasoning(f"[LLM 自决结束] {ctx.final_output[:50]}...")
            await transition(ctx, State.CONCLUDE, reason="LLM 自决")
            break

        # ── 单次迭代 ──
        result = await agent_iteration(ctx, llm, tools)

        # ── 状态检查（agent_iteration 内部已处理转换）──
        if ctx.state == State.ASK_USER:
            # ASK_USER 是硬阻塞：等待用户回复后由 REPL 继续
            await _save_session(ctx)
            # REPL 层等待用户输入，回调继续 loop
            await _wait_for_user_reply(ctx)
            # 用户已回复，从 ASK_USER 退出继续
            await transition(ctx, State.INVESTIGATE, reason="用户已回复")
            continue

        if ctx.state == State.EXIT:
            break

        if ctx.state == State.CONCLUDE:
            break

        # ── 每轮保存 ──
        await _save_session(ctx)

    # ── CONCLUDE 态 ──
    ctx.final_output = await _conclude(ctx)
    await _save_session(ctx)
    ctx.append_reasoning("[SESSION END]")
    return ctx
```

#### 4.2.2 内层 Processor：agent_iteration()

```python
# src/microtrace/agent/loop.py

async def agent_iteration(
    ctx: Context,
    llm: LLMClient,
    tools: ToolRegistry,
) -> None:
    """
    内层 processor：单次 LLM stream 迭代
    - 组装 prompt
    - 调用 llm.stream()
    - 处理流式事件
    - 执行工具调用
    - 更新 judgment / evidence
    - 检测 overflow / doom_loop
    - 触发 compaction

    不做状态转换（状态转换由 run_session 处理）
    """
    # ── 1. Doom Loop 检测（在调用 LLM 前）──
    if _check_doom_loop(ctx):
        last_call = ctx.tool_history[-1]
        ctx.pending_question = QuestionPrompt(
            header="Doom Loop (3次)",
            question=f"工具 {last_call.name} 连续 3 次以相同参数调用。你想怎么办？",
            options=[
                QuestionOption(label="继续", description="这一次允许"),
                QuestionOption(label="总是允许", description="整个 session 不再问"),
                QuestionOption(label="拒绝", description="禁用此工具"),
            ],
            multiple=False,
            custom=True,
        )
        await transition(ctx, State.ASK_USER, reason="Doom Loop 触发")
        return

    # ── 2. Prompt 组装 ──
    prompt_text = _assemble_prompt(ctx, tools)
    ctx.append_reasoning(f"[LLM 调用] iter={ctx.iteration}, prompt长度={len(prompt_text)}")

    # ── 3. LLM 流式调用 + 事件处理 ──
    ctx.append_event("step.started", {"iteration": ctx.iteration})

    tool_calls_to_run: list[ToolCall] = []
    judgment_update: Judgment | None = None
    question_text: str | None = None
    conclusion_text: str | None = None
    current_text_parts: list[str] = []
    current_reasoning: str = ""
    current_tool_input: dict | None = None
    current_tool_name: str | None = None
    current_tool_call_id: str | None = None

    try:
        async for event in llm.stream(prompt_text, tools=tools.schemas()):
            ctx.append_event("llm.event", {"type": event.type, "iteration": ctx.iteration})

            if event.type == StreamEventType.TEXT_DELTA:
                current_text_parts.append(event.text or "")
                ctx.append_reasoning(f"[text-delta] {event.text}")

            elif event.type == StreamEventType.REASONING_DELTA:
                current_reasoning += event.text or ""
                ctx.append_reasoning(f"[reasoning-delta] {event.text}")

            elif event.type == StreamEventType.TOOL_INPUT_START:
                current_tool_name = event.tool_name
                current_tool_input = {}
                current_tool_call_id = event.tool_call_id
                ctx.append_reasoning(f"[tool-input-start] {event.tool_name}")

            elif event.type == StreamEventType.TOOL_CALL:
                # 工具调用完整生成
                tool_calls_to_run.append(ToolCall(
                    name=event.tool_name or current_tool_name or "unknown",
                    args=event.tool_args or current_tool_input or {},
                    args_summary=_summarize_args(event.tool_args or {}),
                    output_summary="",
                    output_raw=None,
                    iteration=ctx.iteration,
                    state=ToolState.RUNNING,
                ))
                ctx.append_reasoning(f"[tool-call] {event.tool_name} args={_summarize_args(event.tool_args or {})}")

            elif event.type == StreamEventType.TEXT_END:
                text = "".join(current_text_parts)
                ctx.append_reasoning(f"[text-end] {len(text)} 字")
                # 解析 text 中是否含 conclusion/question
                parsed = _parse_text_action(text)
                if parsed.get("action") == "conclude":
                    conclusion_text = parsed.get("text", text)
                elif parsed.get("action") == "ask_user":
                    question_text = parsed.get("question", text)

            elif event.type == StreamEventType.STEP_FINISH:
                ctx.append_reasoning(f"[step-finish] reason={event.finish_reason}, tokens={event.tokens}")
                ctx.cumulative_tokens += event.tokens or 0
                if event.tokens:
                    ctx.cumulative_tokens += event.tokens

            elif event.type == StreamEventType.ERROR:
                ctx.append_reasoning(f"[LLM ERROR] {event.error}")
                ctx.error = event.error
                ctx.append_event("step.failed", {"error": event.error})

            # judgment_update 事件（由 LLM 通过 tool 响应附带）
            if event.evidence_relevance is not None:
                # 最后一个 tool call 附带 evidence evaluation
                if tool_calls_to_run:
                    tc = tool_calls_to_run[-1]
                    ctx.append_reasoning(
                        f"[evidence evaluation] relevance={event.evidence_relevance}, "
                        f"importance={event.evidence_importance}"
                    )

    except Exception as e:
        ctx.append_reasoning(f"[LLM STREAM ERROR] {e}")
        ctx.error = str(e)
        ctx.append_event("step.failed", {"error": str(e)})
        # retry 由 llm.stream() 内部处理（5 次 + 指数退避）
        raise

    ctx.append_event("step.finished", {"iteration": ctx.iteration})

    # ── 4. 响应后处理 ──
    if tool_calls_to_run:
        # 分支 1：执行工具调用（并行）
        await _execute_tools_parallel(ctx, tool_calls_to_run, tools)

    elif question_text:
        # 分支 2：主动询问用户
        ctx.pending_question = QuestionPrompt(
            header="Agent 提问",
            question=question_text,
            options=[],
            multiple=False,
            custom=True,
        )
        await transition(ctx, State.ASK_USER, reason="LLM ask_user")

    elif conclusion_text:
        # 分支 3：LLM 认为结论已充分
        ctx.final_output = conclusion_text
        ctx.append_reasoning(f"[LLM 自决结束] {conclusion_text[:100]}")

    # ── 5. Overflow 检查 ──
    if await _check_overflow(ctx, llm):
        await _trigger_compaction(ctx, llm)
        # compaction 后继续 loop

    return


# ── 辅助函数 ───────────────────────────────────────────────────


def _summarize_args(args: dict) -> str:
    """工具参数摘要（Doom Loop 显示用）"""
    items = [f"{k}={repr(v)[:30]}" for k, v in list(args.items())[:3]]
    return ", ".join(items)


def _parse_text_action(text: str) -> dict:
    """
    从 LLM 文本中解析 action 声明
    格式：{@action: conclude, text: ...} 或 {@action: ask_user, question: ...}
    """
    import re
    pattern = r'{@action:\s*(\w+)(?:,\s*(\w+):\s*([^}]*))?}'
    m = re.search(pattern, text)
    if not m:
        return {}
    action = m.group(1)
    if action == "conclude":
        return {"action": "conclude", "text": m.group(3) or text}
    elif action == "ask_user":
        return {"action": "ask_user", "question": m.group(3) or text}
    return {}


async def _wait_for_user_reply(ctx: Context) -> None:
    """
    等待用户回复（ASK_USER 硬阻塞）
    由 REPL 层调用：用户输入后设置 ctx.user_replies
    """
    import asyncio
    while ctx.pending_question and not ctx.user_replies:
        await asyncio.sleep(0.1)

---

### 4.2.3 工具并行执行

```python
# src/microtrace/agent/loop.py

async def _execute_tools_parallel(
    ctx: Context,
    tool_calls: list[ToolCall],
    tools: ToolRegistry,
) -> None:
    """
    工具并行执行
    - asyncio.gather(return_exceptions=True) 隔离错误
    - 一个工具挂了不影响其他
    - 工具结果转为 evidence 追加
    """
    import asyncio

    async def _execute_one(tc: ToolCall) -> ToolCall:
        tc.state = ToolState.RUNNING
        ctx.add_tool_call(tc)
        ctx.append_reasoning(f"[工具执行] {tc.name} 开始")

        try:
            tool = tools.get(tc.name)
            result = await tool.execute(**tc.args)
            tc.output_raw = result.content
            tc.output_summary = _summarize_output(result.content)
            tc.state = ToolState.COMPLETED
            ctx.append_reasoning(f"[工具完成] {tc.name} → {tc.output_summary[:50]}")

            # evidence evaluation（从 result 的 metadata 里取）
            if result.metadata and result.metadata.get("relevance") is not None:
                ev = Evidence(
                    source=EvidenceSource.TOOL_OUTPUT,
                    location=f"tool:{tc.name}",
                    content=tc.output_summary,
                    raw_content=tc.output_raw or "",
                    relevance=result.metadata.get("relevance", 0.5),
                    importance=result.metadata.get("importance", EvidenceImportance.SUPPORTING),
                    content_type=ContentType.COMPRESSIBLE,
                    discovered_at_iteration=ctx.iteration,
                    tool_name=tc.name,
                )
                # microtrace 独有：关键行提取
                ev.preserved_lines = extract_microtrace_critical_lines(tc.output_raw or "")
                # 5 条结构规则自动判定
                ev.content_type = determine_content_type(ev, ctx)
                ctx.add_evidence(ev)

            return tc

        except Exception as e:
            tc.state = ToolState.ERROR
            tc.error = str(e)
            tc.output_summary = f"ERROR: {e}"
            ctx.append_reasoning(f"[工具错误] {tc.name}: {e}")
            # 工具错误作为 evidence 追加（source=error）
            ev = Evidence(
                source=EvidenceSource.ERROR,
                location=f"tool:{tc.name}",
                content=f"Tool {tc.name} failed: {e}",
                raw_content="",
                importance=EvidenceImportance.BACKGROUND,
                content_type=ContentType.COMPRESSIBLE,
                discovered_at_iteration=ctx.iteration,
                tool_name=tc.name,
            )
            ctx.add_evidence(ev)
            return tc

    # 并行执行 + 错误隔离
    results: list[ToolCall] = await asyncio.gather(
        *[_execute_one(tc) for tc in tool_calls],
        return_exceptions=True,
    )

    # 处理异常结果（asyncio.gather 返回的 Exception）
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            tc = tool_calls[i]
            tc.state = ToolState.ERROR
            tc.error = str(result)
            ctx.add_tool_call(tc)
            ctx.append_reasoning(f"[工具异常] {tc.name}: {result}")
```

---

### 4.2.4 INTAKE / CONCLUDE / MAX_ITERATIONS 辅助函数

```python
# src/microtrace/agent/loop.py

async def _intake(
    ctx: Context,
    initial_input: str,
    llm: LLMClient,
    tools: ToolRegistry,
) -> None:
    """
    INTAKE 态：解析原始输入
    - 解析堆栈 / 关键词 / 时间戳
    - 解析失败用降级处理（Problem 部分字段为空）
    - 严重失败（空输入）直接 EXIT
    """
    ctx.append_reasoning("[INTAKE] 开始解析原始输入")
    ctx.append_event("state.intake.started", {})

    # 审计修复 3：空输入直接 EXIT
    if not initial_input or not initial_input.strip():
        ctx.error = "Empty input"
        ctx.state = State.EXIT
        ctx.append_reasoning("[INTAKE] 空输入，直接 EXIT")
        ctx.append_event("state.exited", {"reason": "empty_input"})
        return

    try:
        # 调用 LLM 解析（INTAKE 用 complete，不是 stream）
        parse_prompt = f"""解析以下用户输入，提取问题信息：

{initial_input}

输出 JSON 格式：
{{
  "error_type": "错误类型描述",
  "stack_frames": [
    {{"class_name": "...", "method_name": "...", "file_name": "...", "line_number": 0}}
  ],
  "log_snippets": ["日志片段..."],
  "timestamp": "问题发生时间（ISO 格式，可为空）"
}}"""
        response = await llm.complete(parse_prompt)

        import json, re
        # 提取 JSON
        m = re.search(r'\{.*\}', response, re.DOTALL)
        if m:
            data = json.loads(m.group())
            stack_frames = [
                StackFrame(
                    class_name=f.get("class_name", ""),
                    method_name=f.get("method_name", ""),
                    file_name=f.get("file_name", ""),
                    line_number=f.get("line_number", 0),
                )
                for f in data.get("stack_frames", [])
            ]
            ctx.problem = Problem(
                raw_input=initial_input,
                error_type=data.get("error_type"),
                stack_frames=stack_frames,
                log_snippets=data.get("log_snippets", []),
            )
            ctx.append_reasoning(f"[INTAKE] 解析成功: error_type={data.get('error_type')}")
        else:
            # 降级：部分解析
            ctx.problem = Problem(
                raw_input=initial_input,
                parse_error="JSON 解析失败",
            )
            ctx.append_reasoning("[INTAKE] JSON 解析失败，降级处理")

    except Exception as e:
        # 审计修复 3：解析彻底失败也 EXIT（不浪费 8 轮空跑）
        ctx.problem = Problem(
            raw_input=initial_input,
            parse_error=str(e),
        )
        ctx.error = f"INTAKE parse failed: {e}"
        ctx.state = State.EXIT
        ctx.append_reasoning(f"[INTAKE] 解析彻底失败: {e}，直接 EXIT")
        ctx.append_event("state.exited", {"reason": "parse_failed"})
        return

    ctx.append_event("state.intake.completed", {"error_type": ctx.problem.error_type})


async def _force_max_iter_summary(ctx: Context, llm: LLMClient) -> None:
    """
    MAX_ITERATIONS 到达时的强制总结
    - 注入 MAX_ITERATIONS_REACHED prompt（禁止工具调用）
    - LLM 失败用 judgment fallback 兜底
    """
    ctx.append_reasoning("[MAX_ITERATIONS] 强制总结，无工具调用")

    forced_prompt = _build_forced_summary_prompt(ctx)
    # 注意：tools=[] 禁止任何工具调用
    try:
        async for event in llm.stream(forced_prompt, tools=[]):
            if event.type == StreamEventType.TEXT_END:
                ctx.final_output = "".join(
                    part for part in event.text if part
                )
    except Exception as e:
        # 审计修复 4：LLM 失败用 judgment fallback
        ctx.append_reasoning(f"[MAX_ITERATIONS] 强制总结 LLM 失败: {e}，用 judgment 兜底")
        ctx.final_output = _format_judgment_fallback(ctx)


def _build_forced_summary_prompt(ctx: Context) -> str:
    """构建 MAX_ITERATIONS 强制总结 prompt"""
    # 从 prompts/agent.md 加载 MAX_ITERATIONS_REACHED section
    from microtrace.prompts import load_agent_prompt
    agent_prompt = load_agent_prompt()
    max_iter_section = agent_prompt.get_section("MAX_ITERATIONS_REACHED")
    return max_iter_section.format(
        max_iterations=ctx.max_iterations,
        problem=ctx.problem.raw_input if ctx.problem else "",
        current_judgment=ctx.current_judgment.to_brief() if ctx.current_judgment else "UNKNOWN",
        evidence_count=len(ctx.evidence),
        reasoning_trace="\n".join(ctx.reasoning_trace[-5:]),
    )


def _format_judgment_fallback(ctx: Context) -> str:
    """LLM 不可用时的 judgment fallback 输出"""
    judgment = ctx.current_judgment
    if judgment.category == JudgmentCategory.UNKNOWN:
        return "Agent 未能形成结论（异常退出）"

    lines = [
        "## 尽力而为的判断",
        "",
        f"**类别**: {judgment.category}",
        f"**置信度**: {judgment.confidence:.2f}",
        f"**理由**: {judgment.one_line_reason}",
        "",
        f"## 已知证据（{len(ctx.evidence)} 条）",
    ]
    for i, ev in enumerate(ctx.evidence[:5], 1):
        lines.append(f"{i}. [{ev.source}] {ev.location}")

    lines.extend([
        "",
        "⚠️ *（MAX_ITERATIONS 强制总结时 LLM 不可用，本输出为兜底）*",
    ])
    return "\n".join(lines)


async def _conclude(ctx: Context) -> str:
    """
    CONCLUDE 态：格式化输出
    """
    ctx.append_reasoning("[CONCLUDE] 格式化输出")

    if ctx.final_output:
        return ctx.final_output

    # 默认格式化
    lines = [
        "# 问题诊断结论",
        "",
        f"**类别**: {ctx.current_judgment.category}",
        f"**置信度**: {ctx.current_judgment.confidence:.2f}",
        f"**理由**: {ctx.current_judgment.one_line_reason}",
        "",
        "## 证据链",
    ]
    for ev in ctx.evidence:
        if ev.importance == EvidenceImportance.CRITICAL:
            lines.append(f"- [{ev.source}] {ev.location}: {ev.content[:100]}")

    return "\n".join(lines)


async def _save_session(ctx: Context) -> None:
    """每轮保存到 SQLite"""
    from microtrace.persistence.sqlite import save_context_to_sqlite
    from microtrace.config import get_db_path
    try:
        save_context_to_sqlite(ctx, str(get_db_path()))
    except Exception as e:
        ctx.append_reasoning(f"[SAVE] 失败: {e}")
```

> 参考 DESIGN.md §2，§4，§13

---

### 4.3 Context

#### 4.3.1 Context 完整字段

见 §3.2.10 Context 模型。

#### 4.3.2 永不压缩区 vs 可压缩区

```python
# src/microtrace/context/prompt.py

# ── 永不压缩（always include in prompt）──
NEVER_COMPACT = [
    "problem.raw_input",      # 用户原始输入
    "current_judgment",       # 最新判断
    "pending_question",       # 用户显式输入
]

# ── 按 importance 截取 ──
IMPORTANCE_ORDER = [
    EvidenceImportance.CRITICAL,    # top
    EvidenceImportance.SUPPORTING, # middle
    EvidenceImportance.BACKGROUND, # skip unless room
]

# ── 按 relevance 排序（within importance group）──
def sort_evidence(evidence: list[Evidence]) -> list[Evidence]:
    return sorted(evidence, key=lambda e: (
        -IMPORTANCE_ORDER.index(e.importance),
        -e.relevance,
    ))
```

#### 4.3.3 Prompt 装配算法

```python
# src/microtrace/context/prompt.py

def _assemble_prompt(ctx: Context, tools: ToolRegistry) -> str:
    """
    从 Context 组装 LLM prompt
    8-section 结构（与 OpenCode system prompt 对齐）

    截取规则：
    - evidence pool: 按 importance+relevance 排序，最多 5 条
    - reasoning_trace: 最近 3 步
    - 单条 evidence content: 最多 500 字
    """
    sections = []

    # 1. System Prompt（全量，不压缩）
    sections.append(_load_system_prompt())

    # 2. Problem（永不压缩）
    sections.append(_format_problem(ctx.problem))

    # 3. Judgment（永不压缩）
    sections.append(_format_judgment(ctx.current_judgment))

    # 4. Evidence Pool（按 importance+relevance 截取）
    evidence_text = _format_evidence_pool(ctx.evidence, max_items=5, max_content_len=500)
    # compaction summary 注入
    if ctx.compactions:
        evidence_text += "\n\n## 历史压缩摘要\n"
        evidence_text += _format_compactions(ctx.compactions[-2:])
    sections.append(evidence_text)

    # 5. Reasoning Trace（最近 3 步）
    sections.append(_format_reasoning_trace(ctx.reasoning_trace, max_steps=3))

    # 6. User Replies（最近 2 轮 via OpenCode DEFAULT_TAIL_TURNS）
    if ctx.user_replies:
        sections.append(_format_user_replies(ctx.user_replies))

    # 7. Disabled Tools（审计修复 2：显式注入）
    if ctx.disabled_tools:
        disabled_section = "## ⚠️ 已禁用工具（请不要调）\n"
        disabled_section += "\n".join(f"- `{t}`" for t in ctx.disabled_tools)
        sections.append(disabled_section)

    # 8. Available Tools（全量，不压缩）
    sections.append(_format_tools(tools))

    # 9. Instruction
    sections.append(_build_instruction(ctx))

    return "\n\n".join(sections)


def _format_problem(problem: Problem | None) -> str:
    if not problem:
        return "## 问题\n（尚未解析）"
    parts = [f"## 问题\n\n{problem.raw_input[:2000]}"]
    if problem.error_type:
        parts.append(f"**错误类型**: {problem.error_type}")
    if problem.stack_frames:
        parts.append("**堆栈帧**:\n" + "\n".join(
            f"- {sf.to_short_string()}" for sf in problem.stack_frames[:5]
        ))
    return "\n".join(parts)


def _format_judgment(judgment: Judgment) -> str:
    return f"""## 当前判断

**类别**: {judgment.category}
**置信度**: {judgment.confidence:.2f}
**理由**: {judgment.one_line_reason}
**推理**: {judgment.reasoning}"""


def _format_evidence_pool(
    evidence: list[Evidence],
    max_items: int = 5,
    max_content_len: int = 500,
) -> str:
    """按 importance+relevance 排序，截取最多 max_items 条"""
    sorted_ev = sort_evidence(evidence)
    selected = sorted_ev[:max_items]

    lines = ["## 证据池"]
    for ev in selected:
        content = ev.content[:max_content_len]
        lines.append(f"\n### [{ev.source}] {ev.location}")
        lines.append(f"relevance={ev.relevance:.2f}, importance={ev.importance}")
        lines.append(content)
        if ev.preserved_lines:
            lines.append(f"**关键行**: {ev.preserved_lines[:200]}")

    skipped = len(sorted_ev) - len(selected)
    if skipped > 0:
        lines.append(f"\n_（还有 {skipped} 条 evidence 已截取）_")

    return "\n".join(lines)


def _format_reasoning_trace(trace: list[str], max_steps: int = 3) -> str:
    recent = trace[-max_steps:] if trace else []
    if not recent:
        return "## 推理轨迹\n（暂无）"
    return "## 推理轨迹（最近）\n" + "\n".join(f"- {s}" for s in recent)


def _format_user_replies(replies: list[UserReply]) -> str:
    lines = ["## 用户回复"]
    for r in replies[-2:]:  # 最近 2 轮
        lines.append(f"**Q**: {r.question}")
        lines.append(f"**A**: {r.answer}")
    return "\n".join(lines)


def _format_tools(tools: ToolRegistry) -> str:
    lines = ["## 可用工具"]
    for tool in tools.list():
        lines.append(f"\n### {tool.name}")
        lines.append(tool.description)
        lines.append(f"```json\n{tool.parameters_json}\n```")
    return "\n".join(lines)


def _build_instruction(ctx: Context) -> str:
    return f"""## 指令

- 当前处于第 {ctx.iteration} 轮（共最多 {ctx.max_iterations} 轮）
- 每条结论必须引用证据（证据编号或文件:行号）
- 证据不足时，明确说"我无法判断，需要 X 信息"
- 使用工具获取事实，不要臆测
"""
```

#### 4.3.4 Disabled Tools 注入（审计修复 2）

见 `_assemble_prompt()` 中 §7 Disabled Tools。

> 参考 DESIGN.md §4，§13

---

### 4.4 Tools（4 个最小工具）

#### 4.4.1 通用 Tool Interface

```python
# src/microtrace/tools/base.py
from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Any
from pydantic import BaseModel, Field


class ToolInput(BaseModel):
    """工具输入基类（所有工具输入继承此）"""
    pass


class ToolResult(BaseModel):
    """工具执行结果"""
    content: str = Field(description="工具输出内容（文本）")
    metadata: dict[str, Any] | None = Field(
        default=None,
        description="附加元数据（用于 evidence evaluation）"
    )


class Tool(ABC):
    """
    工具基类
    使用 Tool.define 模式（类工厂）注册
    """
    name: str
    description: str
    input_model: type[ToolInput]

    @abstractmethod
    async def execute(self, input: ToolInput) -> ToolResult:
        """执行工具逻辑"""
        ...

    @property
    def schema(self) -> dict:
        """返回工具 schema（用于 LLM prompt）"""
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
        input_model: type[ToolInput],
    ) -> type[Tool]:
        """
        类工厂：创建工具类
        用法：
        ReadFileTool = Tool.define(
            name="read_file",
            description="...",
            input_model=ReadFileInput,
        )
        """
        class NewTool(cls):
            pass
        NewTool.name = name
        NewTool.description = description
        NewTool.input_model = input_model
        return NewTool
```

#### 4.4.2 read_file

```python
# src/microtrace/tools/read_file.py
from pathlib import Path
from pydantic import Field, FieldValidationInfo, field_validator
from microtrace.tools.base import Tool, ToolInput, ToolResult


class ReadFileInput(ToolInput):
    """读取代码或日志文件"""
    file_path: str = Field(description="文件路径（绝对路径或相对于项目根目录）")
    offset: int = Field(default=0, description="起始行号（0-indexed）")
    limit: int = Field(default=200, description="最多读取行数")

    @field_validator("file_path")
    @classmethod
    def validate_path(cls, v: str, info: FieldValidationInfo) -> str:
        # 安全检查：不允许 ../ 穿越
        import os
        if ".." in v:
            raise ValueError("Path traversal not allowed")
        return v


class ReadFileTool(Tool):
    name = "read_file"
    description = "读取代码或日志文件内容。支持指定行号范围。用于查看源代码或日志内容。"

    async def execute(self, input: ReadFileInput) -> ToolResult:
        import asyncio

        def _read() -> str:
            p = Path(input.file_path)
            if not p.exists():
                raise FileNotFoundError(f"文件不存在: {input.file_path}")
            if not p.is_file():
                raise ValueError(f"不是文件: {input.file_path}")

            with open(p, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()

            start = input.offset
            end = min(start + input.limit, len(lines))
            content = "".join(lines[start:end])

            header = f"--- {input.file_path} ({start+1}-{end} 行，共 {len(lines)} 行) ---\n"
            return header + content

        # 在线程池中执行（避免阻塞）
        content = await asyncio.to_thread(_read)

        return ToolResult(
            content=content,
            metadata={
                "file_path": input.file_path,
                "lines_read": min(input.limit, content.count("\n") + 1),
            },
        )


# 导出
ReadFileInputSchema = ReadFileInput
ReadFile = ReadFileTool.define(
    name="read_file",
    description=ReadFileTool.description,
    input_model=ReadFileInput,
)
```

#### 4.4.3 search_logs

```python
# src/microtrace/tools/search_logs.py
from pathlib import Path
from pydantic import Field, field_validator
from microtrace.tools.base import Tool, ToolInput, ToolResult


class SearchLogsInput(ToolInput):
    """按关键词/时间范围搜索日志"""
    keyword: str = Field(description="搜索关键词（支持多个，逗号分隔）")
    log_dir: str = Field(default="/var/log", description="日志目录")
    time_range: str | None = Field(
        default=None,
        description="时间范围，如 '10:23-10:30' 或 '2026-06-05 10:23:00-10:30:00'"
    )
    max_lines: int = Field(default=100, description="最多返回行数")

    @field_validator("keyword")
    @classmethod
    def validate_keyword(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("关键词不能为空")
        return v


class SearchLogsTool(Tool):
    name = "search_logs"
    description = "按关键词和时间范围搜索日志文件。返回匹配的行及其上下文。"

    async def execute(self, input: SearchLogsInput) -> ToolResult:
        import asyncio
        import re
        from datetime import datetime

        def _search() -> str:
            keywords = [k.strip() for k in input.keyword.split(",")]
            results: list[str] = []

            # 遍历日志目录
            log_path = Path(input.log_dir)
            if not log_path.exists():
                return f"日志目录不存在: {input.log_dir}"

            # 常见日志文件模式
            log_files = sorted(log_path.glob("*.log"))[:10]  # 最多 10 个

            for log_file in log_files:
                try:
                    with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                        for line in f:
                            # 时间过滤
                            if input.time_range:
                                # 简单时间匹配（实际实现需要解析时间格式）
                                pass

                            # 关键词匹配
                            if any(kw in line for kw in keywords):
                                results.append(f"[{log_file.name}] {line.rstrip()}")
                                if len(results) >= input.max_lines:
                                    break
                except Exception:
                    continue

                if len(results) >= input.max_lines:
                    break

            if not results:
                return f"未找到匹配 '{input.keyword}' 的日志"

            return f"找到 {len(results)} 条匹配：\n" + "\n".join(results)

        content = await asyncio.to_thread(_search)
        return ToolResult(
            content=content,
            metadata={"keyword": input.keyword, "match_count": content.count("\n")},
        )


SearchLogs = SearchLogsTool.define(
    name="search_logs",
    description=SearchLogsTool.description,
    input_model=SearchLogsInput,
)
```

#### 4.4.4 find_class

```python
# src/microtrace/tools/find_class.py
from pathlib import Path
from pydantic import Field, field_validator
from microtrace.tools.base import Tool, ToolInput, ToolResult


class FindClassInput(ToolInput):
    """按类名定位 Java 文件"""
    class_name: str = Field(description="Java 类名（不含 .java 后缀）")
    search_root: str | None = Field(
        default=None,
        description="搜索根目录（默认从当前目录搜索）"
    )

    @field_validator("class_name")
    @classmethod
    def validate_class_name(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("类名不能为空")
        if not v[0].isupper():
            raise ValueError("Java 类名必须以大写字母开头")
        return v


class FindClassTool(Tool):
    name = "find_class"
    description = "在项目目录中搜索 Java 类文件。返回文件路径和基本信息。"

    async def execute(self, input: FindClassInput) -> ToolResult:
        import asyncio

        def _find() -> str:
            root = Path(input.search_root) if input.search_root else Path.cwd()
            pattern = f"{input.class_name}.java"

            # 搜索所有 .java 文件
            matches = list(root.rglob(pattern))[:5]  # 最多 5 个

            if not matches:
                return f"未找到类: {input.class_name}"

            results = []
            for p in matches:
                rel = p.relative_to(root) if root in p.parents else str(p)
                # 读类声明行
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        for line in f:
                            if f"class {input.class_name}" in line or f"interface {input.class_name}" in line:
                                results.append(f"{rel}: {line.strip()}")
                                break
                except Exception:
                    results.append(str(rel))

            return f"找到 {len(results)} 个匹配：\n" + "\n".join(results)

        content = await asyncio.to_thread(_find)
        return ToolResult(
            content=content,
            metadata={"class_name": input.class_name},
        )


FindClass = FindClassTool.define(
    name="find_class",
    description=FindClassTool.description,
    input_model=FindClassInput,
)
```

#### 4.4.5 parse_stack_trace

```python
# src/microtrace/tools/parse_stack_trace.py
from pathlib import Path
from pydantic import Field
from microtrace.tools.base import Tool, ToolInput, ToolResult


class ParseStackTraceInput(ToolInput):
    """解析堆栈跟踪，提取关键帧"""
    stack_text: str | None = Field(
        default=None,
        description="堆栈文本（如果为空则从 problem.stack_frames 读取）"
    )
    top_n: int = Field(default=10, description="返回前 N 个堆栈帧")


class ParseStackTraceTool(Tool):
    name = "parse_stack_trace"
    description = "解析 Java 堆栈跟踪，提取 class/method/file/line 信息。用于定位异常发生位置。"

    async def execute(self, input: ParseStackTraceInput) -> ToolResult:
        import asyncio
        import re

        def _parse() -> str:
            # microtrace 关键行提取（8 种正则 pattern）
            CRITICAL_PATTERNS = [
                r"Exception in thread",          # 异常线程
                r"at\s+([\w\.]+)\(([\w\.]+):(\d+)\)",  # 标准堆栈帧
                r"Caused by:",                  # 根因标记
                r"error\s*code[:=]?\s*\d{3,4}",  # 错误码
                r"returned\s+status\s+\d{3}",    # HTTP 状态码
                r"HTTP/\d\.\d\s+\d{3}",         # HTTP 响应
                r"@Transactional",              # 事务注解
                r"@Async",                      # 异步注解
                r"@Scheduled",                  # 定时任务
                r"@FeignClient",                # Feign 调用
                r"@DubboReference",             # Dubbo 调用
                r"\b(ERROR|FATAL)\b",          # 日志级别
            ]

            if input.stack_text:
                text = input.stack_text
            else:
                return "堆栈文本为空"

            lines = text.split("\n")
            frames = []
            critical_lines = []

            for line in lines:
                # 解析堆栈帧：at com.foo.Bar.method(File.java:123)
                m = re.search(r"at\s+([\w\.]+)\.([\w<>]+)\(([\w\.]+):(\d+)\)", line)
                if m:
                    frames.append({
                        "class": m.group(1),
                        "method": m.group(2),
                        "file": m.group(3),
                        "line": int(m.group(4)),
                    })

                # 关键行提取
                for pattern in CRITICAL_PATTERNS:
                    if re.search(pattern, line):
                        critical_lines.append(line.strip())
                        break

            if not frames:
                return f"未解析到堆栈帧：\n{text[:500]}"

            top = frames[: input.top_n]
            result_lines = ["解析到的堆栈帧："]
            for i, f in enumerate(top, 1):
                result_lines.append(
                    f"  {i}. {f['class']}.{f['method']}() at {f['file']}:{f['line']}"
                )

            if critical_lines:
                result_lines.append("\n关键行：")
                for cl in critical_lines[:10]:
                    result_lines.append(f"  - {cl}")

            return "\n".join(result_lines)

        content = await asyncio.to_thread(_parse)
        return ToolResult(
            content=content,
            metadata={"frame_count": content.count(" at ")},
        )


ParseStackTrace = ParseStackTraceTool.define(
    name="parse_stack_trace",
    description=ParseStackTraceTool.description,
    input_model=ParseStackTraceInput,
)
```

#### 4.4.6 microtrace 关键行提取

```python
# src/microtrace/context/compaction.py

MICROTRACE_PRESERVE_PATTERNS: list[str] = [
    # Java 异常
    r"Exception in thread",
    r"at\s+[\w\.]+\([\w\.]+\.java:\d+\)",
    r"Caused by:",
    # 下游错误码
    r"error\s*code[:=]?\s*\d{3,4}",
    r"returned\s+status\s+\d{3}",
    r"HTTP/\d\.\d\s+\d{3}",
    # 业务关键标识
    r"@Transactional",
    r"@Async",
    r"@Scheduled",
    r"@FeignClient",
    r"@DubboReference",
    # 日志级别
    r"\b(ERROR|FATAL)\b",
]


def extract_microtrace_critical_lines(tool_output: str, max_lines: int = 20) -> str:
    """
    从 tool output 提取 microtrace 关键行（不 summarization）
    即使 evidence 被 PRUNE，这些关键行仍保留在 preserved_lines 字段
    """
    import re
    lines = tool_output.split("\n")
    critical = []
    for line in lines:
        for pattern in MICROTRACE_PRESERVE_PATTERNS:
            if re.search(pattern, line):
                critical.append(line.strip())
                break
    return "\n".join(critical[:max_lines])
```

> 参考 DESIGN.md §4，§8

---

### 4.5 LLM Client

#### 4.5.1 LLMClient Protocol

```python
# src/microtrace/llm/client.py
from __future__ import annotations
from typing import Protocol, AsyncIterator


class LLMClient(Protocol):
    """
    LLM 客户端抽象接口
    支持流式调用（async generator）
    """

    async def stream(
        self,
        prompt: str,
        tools: list[dict] | None = None,
    ) -> AsyncIterator["StreamEvent"]:
        """
        流式调用 LLM

        Args:
            prompt: 组装好的 prompt 文本
            tools: 工具 schema 列表（None=禁止工具调用）

        Yields:
            StreamEvent: 流式事件

        Raises:
            NetworkError: 网络问题（可重试）
            AuthError: 认证失败（不重试）
            RateLimitError: 限流（可重试）
            BadRequestError: 请求错误（不重试）
            ServerError: 服务端错误（可重试）
        """
        ...

    async def complete(
        self,
        prompt: str,
    ) -> str:
        """
        非流式调用（用于 INTAKE 解析等简单场景）

        Returns:
            str: LLM 响应文本
        """
        ...


# 错误层级
class LLMError(Exception):
    """LLM 错误基类"""
    pass


class NetworkError(LLMError):
    """网络问题（可重试）"""
    pass


class AuthError(LLMError):
    """认证失败（不重试）"""
    pass


class RateLimitError(LLMError):
    """限流（可重试）"""
    def __init__(self, message: str, retry_after_ms: int | None = None):
        super().__init__(message)
        self.retry_after_ms = retry_after_ms


class BadRequestError(LLMError):
    """请求错误（不重试）"""
    pass


class ServerError(LLMError):
    """服务端错误（可重试）"""
    pass


class TimeoutError(LLMError):
    """超时（可重试）"""
    pass
```

#### 4.5.2 MiniMaxClient 实现

```python
# src/microtrace/llm/minimax.py
from __future__ import annotations
import json
import asyncio
from typing import AsyncIterator
from openai import AsyncOpenAI, APIError, RateLimitError as OpenAIRateLimitError, Timeout as OpenAITimeout
from microtrace.llm.client import (
    LLMClient, StreamEvent, NetworkError, AuthError,
    RateLimitError, BadRequestError, ServerError, TimeoutError,
)
from microtrace.config import Config


RETRY_DELAYS: list[int] = [2, 4, 8, 16, 32]  # 秒
MAX_RETRIES: int = 5


class MiniMaxClient:
    """
    MiniMax LLM 客户端（使用 OpenAI SDK，base_url 改为 MiniMax）
    """

    def __init__(self, config: Config):
        self.config = config
        self.client = AsyncOpenAI(
            api_key=config.llm.api_key or "dummy",  # 运行时从环境变量或 config 覆盖
            base_url=config.llm.base_url,
            timeout=config.llm.timeout,
            max_retries=0,  # 我们自己实现 retry
        )
        self.model = config.llm.model

    async def stream(
        self,
        prompt: str,
        tools: list[dict] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """
        流式调用 MiniMax API

        实现要点：
        - 用 OpenAI SDK 的 stream 模式
        - 将 OpenAI 事件映射到 microtrace 的 StreamEvent
        - 内部处理 5 次重试 + 指数退避
        - 遵守 Retry-After header
        """
        messages = [{"role": "user", "content": prompt}]
        tools_param = tools if tools else None

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                async with self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    tools=tools_param,
                    stream=True,
                    stream_options={"include_usage": True},
                ) as stream:
                    async for chunk in stream:
                        # 映射 OpenAI chunk → microtrace StreamEvent
                        event = self._map_chunk(chunk)
                        if event:
                            yield event

                        # 检查 usage（在最后的 chunk 里）
                        if chunk.usage:
                            # 可以 yield 一个 usage 事件
                            pass

                return  # 成功完成

            except OpenAIRateLimitError as e:
                retry_after_ms = self._extract_retry_after(e)
                if attempt == MAX_RETRIES:
                    raise RateLimitError(
                        f"Rate limit after {MAX_RETRIES} retries",
                        retry_after_ms=retry_after_ms,
                    )
                wait_ms = retry_after_ms or RETRY_DELAYS[attempt - 1] * 1000
                await asyncio.sleep(wait_ms / 1000)

            except OpenAITimeout as e:
                if attempt == MAX_RETRIES:
                    raise TimeoutError(f"Timeout after {MAX_RETRIES} retries")

            except APIError as e:
                if e.status_code is not None:
                    if 400 <= e.status_code < 500:
                        raise BadRequestError(f"Bad request: {e}")
                    elif e.status_code == 401 or e.status_code == 403:
                        raise AuthError(f"Auth error: {e}")
                    else:
                        if attempt == MAX_RETRIES:
                            raise ServerError(f"Server error after {MAX_RETRIES} retries: {e}")
                        wait_ms = RETRY_DELAYS[attempt - 1] * 1000
                        await asyncio.sleep(wait_ms / 1000)
                else:
                    raise NetworkError(f"Network error: {e}")

    async def complete(self, prompt: str) -> str:
        """非流式调用（用于简单解析场景）"""
        messages = [{"role": "user", "content": prompt}]

        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                stream=False,
            )
            return response.choices[0].message.content or ""
        except APIError as e:
            raise ServerError(f"API error: {e}")

    def _map_chunk(self, chunk) -> StreamEvent | None:
        """将 OpenAI chunk 映射到 microtrace StreamEvent"""
        from microtrace.context.models import StreamEventType

        if not chunk.choices:
            return None

        choice = chunk.choices[0]

        if choice.finish_reason:
            return StreamEvent(
                type=StreamEventType.STEP_FINISH,
                finish_reason=choice.finish_reason,
            )

        delta = choice.delta
        if not delta:
            return None

        # tool_calls
        if delta.tool_calls:
            tc = delta.tool_calls[0]
            return StreamEvent(
                type=StreamEventType.TOOL_CALL,
                tool_call_id=tc.id,
                tool_name=tc.function.name,
                tool_args=json.loads(tc.function.arguments) if tc.function.arguments else {},
            )

        # content
        if delta.content:
            return StreamEvent(
                type=StreamEventType.TEXT_DELTA,
                text=delta.content,
            )

        return None

    def _extract_retry_after(self, error: OpenAIRateLimitError) -> int | None:
        """从 Response headers 提取 Retry-After"""
        if error.response is not None:
            ra = error.response.headers.get("retry-after")
            if ra:
                try:
                    return int(ra) * 1000  # 转毫秒
                except ValueError:
                    pass
        return None
```

> 参考 DESIGN.md §2，§6，§11

---

### 4.6 Persistence

#### 4.6.1 SQLite Schema

```python
# src/microtrace/persistence/sqlite.py
import sqlite3
from pathlib import Path


CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('in_progress', 'completed', 'abandoned')),
    title TEXT,
    context_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);
CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions(updated_at DESC);
"""


def init_db(db_path: str) -> None:
    """初始化 SQLite 数据库（创建表）"""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(CREATE_TABLE_SQL)
        conn.commit()
    finally:
        conn.close()
```

#### 4.6.2 save_context_to_sqlite()

```python
# src/microtrace/persistence/sqlite.py
import json
import sqlite3
from microtrace.context.models import Context, SessionStatus


def save_context_to_sqlite(ctx: Context, db_path: str) -> None:
    """
    将 Context 序列化为 JSON 保存到 SQLite
    审计修复 1：ASK_USER 进入/退出时也调用
    """
    import time

    conn = sqlite3.connect(db_path)
    try:
        # 初始化表（如不存在）
        conn.executescript(CREATE_TABLE_SQL)

        context_json = ctx.model_dump_json(exclude_none=True)

        now = time.time()
        status = SessionStatus.IN_PROGRESS.value
        if ctx.state.value == "EXIT":
            status = SessionStatus.COMPLETED.value
        elif ctx.user_interrupt:
            status = SessionStatus.ABANDONED.value

        conn.execute("""
            INSERT OR REPLACE INTO sessions (id, created_at, updated_at, status, title, context_json)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            ctx.session_id,
            ctx.created_at or now,
            now,
            status,
            _generate_title(ctx),
            context_json,
        ))
        conn.commit()
    finally:
        conn.close()


def load_context_from_sqlite(session_id: str, db_path: str) -> Context | None:
    """
    从 SQLite 加载 Context
    用于 `microtrace resume <id>`
    """
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT context_json FROM sessions WHERE id = ?",
            (session_id,)
        ).fetchone()
        if not row:
            return None
        data = json.loads(row[0])
        return Context.model_validate(data)
    finally:
        conn.close()


def list_sessions(db_path: str, limit: int = 20) -> list[dict]:
    """列出最近 N 个 session"""
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("""
            SELECT id, created_at, updated_at, status, title
            FROM sessions
            ORDER BY updated_at DESC
            LIMIT ?
        """, (limit,)).fetchall()
        return [
            {
                "id": r[0],
                "created_at": r[1],
                "updated_at": r[2],
                "status": r[3],
                "title": r[4],
            }
            for r in rows
        ]
    finally:
        conn.close()


def _generate_title(ctx: Context) -> str:
    """生成 session title（用于 sessions 列表显示）"""
    if ctx.problem and ctx.problem.error_type:
        return ctx.problem.error_type[:50]
    if ctx.final_output:
        return ctx.final_output[:50]
    return ctx.state.value
```

#### 4.6.3 CLI 命令

```python
# src/microtrace/cli.py
import typer


app = typer.Typer(help="microtrace - Java 多微服务问题定位 Agent")


@app.command()
def sessions(
    limit: int = typer.Option(20, "--limit", "-n", help="显示最近 N 个 session"),
):
    """列出最近的 session"""
    from microtrace.config import get_db_path
    from microtrace.persistence.sqlite import list_sessions

    db_path = str(get_db_path())
    rows = list_sessions(db_path, limit)

    if not rows:
        typer.echo("没有保存的 session")
        return

    typer.echo(f"{'ID':<40} {'Status':<12} {'Updated':<12} Title")
    typer.echo("-" * 80)
    for r in rows:
        import time
        updated = time.strftime("%m-%d %H:%M", time.localtime(r["updated_at"]))
        typer.echo(f"{r['id']:<40} {r['status']:<12} {updated:<12} {r['title'] or ''}")


@app.command()
def resume(
    session_id: str = typer.Argument(..., help="session ID"),
):
    """恢复一个已存在的 session"""
    from microtrace.config import get_db_path
    from microtrace.persistence.sqlite import load_context_from_sqlite
    from microtrace.repl.main import run_repl

    db_path = str(get_db_path())
    ctx = load_context_from_sqlite(session_id, db_path)
    if not ctx:
        typer.echo(f"Session '{session_id}' 不存在", err=True)
        raise typer.Exit(1)

    typer.echo(f"恢复 session {session_id}")
    typer.echo(f"状态: iter={ctx.iteration}/{ctx.max_iterations}, "
               f"evidence={len(ctx.evidence)}, judgment={ctx.current_judgment.category}")
    run_repl(ctx=ctx)


@app.command()
def delete(
    session_id: str = typer.Argument(..., help="session ID"),
    force: bool = typer.Option(False, "--force", "-f"),
):
    """删除一个 session"""
    if not force:
        typer.confirm(f"删除 session '{session_id}'？", abort=True)
    from microtrace.config import get_db_path
    import sqlite3
    db_path = str(get_db_path())
    conn = sqlite3.connect(db_path)
    conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    conn.commit()
    conn.close()
    typer.echo(f"已删除 {session_id}")
```

> 参考 DESIGN.md §6，§13

---

### 4.7 REPL

#### 4.7.1 REPL 入口

```python
# src/microtrace/repl/main.py
import asyncio
import sys
from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from microtrace.repl.renderer import RichRenderer
from microtrace.agent.loop import run_session
from microtrace.agent.state import State
from microtrace.context.models import Context, UserReply, QuestionPrompt
from microtrace.config import Config


def run_repl(ctx: Context | None = None) -> None:
    """
    REPL 主入口
    - 初始化 prompt_toolkit session
    - 处理用户输入
    - 调用 run_session
    - 显示状态 banner
    """
    _setup_windows_console()

    config = Config.load()
    session = PromptSession(
        history=FileHistory("~/.microtrace_history"),
        renderer=RichRenderer(),
        prompt_msgs=[
            ("fg:ansimagenta", "microtrace"),
            ("", "> "),
        ],
    )

    # 主循环
    while True:
        try:
            user_input = asyncio.run(_get_input(session, ctx))
        except (KeyboardInterrupt, EOFError):
            break

        if not user_input.strip():
            continue

        # 处理命令
        if user_input.startswith("/"):
            if not _handle_command(user_input, ctx):
                break  # exit 命令
            continue

        # 运行 agent session
        ctx = asyncio.run(_run_agent(user_input.strip(), config, ctx))

        # 显示状态
        _display_state(ctx)

    typer.echo("Goodbye!")


async def _get_input(session: PromptSession, ctx: Context | None) -> str:
    """获取用户输入（支持 ASK_USER 阻塞）"""
    if ctx and ctx.state == State.ASK_USER and ctx.pending_question:
        return await _ask_user_input(session, ctx.pending_question)
    return await session.aprompt()


async def _ask_user_input(session: PromptSession, question: QuestionPrompt) -> str:
    """ASK_USER 场景：显示多选弹窗，等待用户选择"""
    from rich.console import Console
    from rich.panel import Panel

    console = Console()

    # 显示问题
    console.print(Panel(
        question.question,
        title=f"[bold yellow]{question.header}[/bold yellow]",
        expand=False,
    ))

    # 显示选项
    if question.options:
        for i, opt in enumerate(question.options, 1):
            console.print(f"  [dim]{i}[/dim] {opt.label} - {opt.description}")

    # 等待用户输入
    return await session.aprompt("[用户回复] ")


def _setup_windows_console() -> None:
    """
    Windows console 兼容性处理
    审计修复 12：UTF-8 + 虚拟终端序列
    """
    if sys.platform != "win32":
        return

    import os
    import ctypes

    # 1. 强制 UTF-8
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    # 2. 启用虚拟终端序列（Win10 1607+）
    os.environ["TERM"] = "xterm-256color"
    try:
        kernel32 = ctypes.windll.kernel32
        STD_OUTPUT_HANDLE = -11
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        handle = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
        mode = kernel32.GetConsoleMode(handle)
        kernel32.SetConsoleMode(handle, mode | ENABLE_VIRTUAL_TERMINAL_PROCESSING)
    except Exception:
        # legacy console → 降级无颜色
        os.environ["NO_COLOR"] = "1"
        os.environ["TERM"] = "dumb"


def _display_state(ctx: Context) -> None:
    """显示当前状态 banner"""
    from rich.console import Console
    from rich.table import Table

    console = Console()

    # 状态颜色
    state_colors = {
        State.INTAKE: "blue",
        State.INVESTIGATE: "green",
        State.ASK_USER: "yellow",
        State.CONCLUDE: "cyan",
        State.EXIT: "red",
    }
    color = state_colors.get(ctx.state, "white")

    table = Table(show_header=False, box=None)
    table.add_column(style="bold")
    table.add_column()

    table.add_row("状态", f"[{color}]{ctx.state.value}[/{color}]")
    table.add_row("轮次", f"{ctx.iteration}/{ctx.max_iterations}")
    table.add_row("证据", f"{len(ctx.evidence)} 条")
    table.add_row("判断", ctx.current_judgment.to_brief())

    console.print(table)
    console.print()


def _handle_command(cmd: str, ctx: Context | None) -> bool:
    """
    处理 REPL 命令
    返回 True=继续，False=退出
    """
    from microtrace.repl.commands import (
        cmd_status, cmd_evidence, cmd_save, cmd_clear, cmd_config, cmd_exit,
        cmd_judgment,
    )

    commands = {
        "/status": cmd_status,
        "/evidence": cmd_evidence,
        "/save": cmd_save,
        "/clear": cmd_clear,
        "/config": cmd_config,
        "/exit": cmd_exit,
        "/quit": cmd_exit,
        "/judgment": cmd_judgment,
    }

    fn = commands.get(cmd.split()[0].lower())
    if fn:
        return fn(ctx)
    typer.echo(f"未知命令: {cmd}")
    return True
```

#### 4.7.2 REPL 命令集

```python
# src/microtrace/repl/commands.py
import typer
from microtrace.context.models import Context


def cmd_status(ctx: Context | None) -> bool:
    """显示当前状态"""
    if not ctx:
        typer.echo("No active session")
        return True

    typer.echo(f"State: {ctx.state.value}")
    typer.echo(f"Iteration: {ctx.iteration}/{ctx.max_iterations}")
    typer.echo(f"Evidence: {len(ctx.evidence)} 条")
    typer.echo(f"Judgment: {ctx.current_judgment.to_brief()}")
    return True


def cmd_evidence(ctx: Context | None) -> bool:
    """展开查看完整证据链"""
    if not ctx:
        typer.echo("No active session")
        return True

    from rich.console import Console
    from rich.syntax import Syntax

    console = Console()
    for i, ev in enumerate(ctx.evidence, 1):
        console.print(f"\n--- Evidence #{i} ---")
        console.print(f"Source: {ev.source}")
        console.print(f"Location: {ev.location}")
        console.print(f"Importance: {ev.importance} (relevance={ev.relevance:.2f})")
        console.print(Syntax(ev.content[:300], "text", theme="monokai"))
    return True


def cmd_judgment(ctx: Context | None) -> bool:
    """显示判断历史"""
    if not ctx:
        typer.echo("No active session")
        return True

    typer.echo(f"\n判断历史（{len(ctx.judgment_history)} 次更新）：\n")
    for i, j in enumerate(ctx.judgment_history, 1):
        marker = "★" if i > 1 and j.category != ctx.judgment_history[i-2].category else ""
        typer.echo(f"  #{i}  {j.category}({j.confidence:.2f}) {marker}")
        typer.echo(f"      {j.one_line_reason}\n")

    typer.echo(f"当前: {ctx.current_judgment.category} "
               f"({ctx.current_judgment.confidence:.2f})")
    return True


def cmd_save(ctx: Context | None) -> bool:
    """保存当前 session"""
    if not ctx:
        typer.echo("No active session")
        return True

    from microtrace.persistence.sqlite import save_context_to_sqlite
    from microtrace.config import get_db_path

    save_context_to_sqlite(ctx, str(get_db_path()))
    typer.echo(f"已保存到 {ctx.session_id}")
    return True


def cmd_clear(ctx: Context | None) -> bool:
    """重置会话"""
    if not typer.confirm("确定重置？"):
        return True
    # 重置 ctx（新建 session）
    return True


def cmd_config(key: str | None, value: str | None) -> bool:
    """查看/修改配置"""
    from microtrace.config import Config

    config = Config.load()
    if key is None:
        typer.echo(config.model_dump_json(indent=2))
        return True

    if value is None:
        typer.echo(f"{key} = {config.model_get(key)}")
    else:
        config.model_set(key, value)
        config.save()
        typer.echo(f"已设置 {key} = {value}")
    return True


def cmd_exit(ctx: Context | None) -> bool:
    """退出 REPL"""
    if ctx:
        from microtrace.persistence.sqlite import save_context_to_sqlite
        from microtrace.config import get_db_path
        ctx.user_interrupt = True
        save_context_to_sqlite(ctx, str(get_db_path()))
    return False
```

> 参考 DESIGN.md §7，§12

---

### 4.8 HTTP API

```python
# src/microtrace/http/api.py
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时初始化
    from microtrace.config import get_db_path
    from microtrace.persistence.sqlite import init_db
    init_db(str(get_db_path()))
    yield


app = FastAPI(
    title="microtrace API",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS 配置（Phase 1+ Web UI 需要）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Phase 1+ 限制具体域名
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatRequest(BaseModel):
    """POST /chat 请求"""
    message: str
    session_id: str | None = None


class ChatResponse(BaseModel):
    """POST /chat 响应"""
    session_id: str
    state: str
    final_output: str | None = None


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest) -> ChatResponse:
    """
    主对话接口
    - 新建 session 或继续已有 session
    - 运行 run_session
    - 返回最终输出
    """
    from microtrace.agent.loop import run_session
    from microtrace.persistence.sqlite import load_context_from_sqlite
    from microtrace.config import get_db_path, Config
    from microtrace.tools import get_tool_registry
    from microtrace.llm.minimax import MiniMaxClient

    config = Config.load()
    db_path = str(get_db_path())

    # resume 或新建
    if req.session_id:
        ctx = load_context_from_sqlite(req.session_id, db_path)
        if not ctx:
            raise HTTPException(404, f"Session {req.session_id} not found")
    else:
        ctx = None

    llm = MiniMaxClient(config)
    tools = get_tool_registry()

    # 运行 agent（暂时同步包装，实际用 run_in_executor）
    import asyncio
    ctx = await asyncio.to_thread(
        asyncio.run,
        run_session(req.message, llm, tools, ctx, req.session_id),
    )

    return ChatResponse(
        session_id=ctx.session_id or "",
        state=ctx.state.value,
        final_output=ctx.final_output,
    )


@app.get("/state/{session_id}")
async def get_state(session_id: str):
    """获取 session 状态"""
    from microtrace.persistence.sqlite import load_context_from_sqlite
    from microtrace.config import get_db_path

    ctx = load_context_from_sqlite(session_id, str(get_db_path()))
    if not ctx:
        raise HTTPException(404, f"Session {session_id} not found")

    return {
        "session_id": ctx.session_id,
        "state": ctx.state.value,
        "iteration": ctx.iteration,
        "max_iterations": ctx.max_iterations,
        "judgment": ctx.current_judgment.to_brief(),
        "evidence_count": len(ctx.evidence),
    }


@app.get("/evidence/{session_id}")
async def get_evidence(session_id: str):
    """获取证据列表"""
    from microtrace.persistence.sqlite import load_context_from_sqlite
    from microtrace.config import get_db_path

    ctx = load_context_from_sqlite(session_id, str(get_db_path()))
    if not ctx:
        raise HTTPException(404, f"Session {session_id} not found")

    return [
        {
            "id": ev.id,
            "source": ev.source,
            "location": ev.location,
            "content": ev.content[:200],
            "importance": ev.importance,
            "relevance": ev.relevance,
        }
        for ev in ctx.evidence
    ]


@app.post("/save/{session_id}")
async def save_session(session_id: str):
    """手动保存 session"""
    from microtrace.persistence.sqlite import load_context_from_sqlite, save_context_to_sqlite
    from microtrace.config import get_db_path

    ctx = load_context_from_sqlite(session_id, str(get_db_path()))
    if not ctx:
        raise HTTPException(404, f"Session {session_id} not found")

    save_context_to_sqlite(ctx, str(get_db_path()))
    return {"status": "saved"}
```

> 参考 DESIGN.md §6

---

### 4.9 Compaction

#### 4.9.1 Overflow 检测

```python
# src/microtrace/context/compaction.py
from microtrace.config import Config

COMPACTION_BUFFER: int = 20_000  # 固定 20K（与 OpenCode 一致）


def is_overflow(ctx, llm) -> bool:
    """
    检测 context 是否溢出
    触发阈值：estimated_tokens >= context_window - COMPACTION_BUFFER
    """
    config = Config.load()
    model = llm  # MiniMaxClient
    # 估算：按字符数 * 0.25 近似 token
    # 实际 production 需要精确 token 计数
    estimated_prompt_tokens = ctx.cumulative_tokens + _estimate_prompt_size(ctx)
    usable = getattr(model, 'context_window', 128_000) - COMPACTION_BUFFER
    return estimated_prompt_tokens >= usable


def _estimate_prompt_size(ctx) -> int:
    """估算当前 prompt 大小（字符数 * 0.25）"""
    import json
    size = len(json.dumps(ctx.model_dump(exclude_none=True)))
    return int(size * 0.25)
```

#### 4.9.2 PRUNE + SUMMARY 完整流程

```python
# src/microtrace/context/compaction.py

DEFAULT_TAIL_TURNS: int = 2  # OpenCode 默认：最近 2 轮全保
TOOL_OUTPUT_MAX_CHARS: int = 2000
PRUNE_PROTECTED_TOOLS: list[str] = ["skill"]
PRUNE_MINIMUM: int = 20_000


async def compact(ctx: Context, llm) -> None:
    """
    Compaction 完整流程
    1. microtrace 独有：提取关键行
    2. OpenCode 通用：PRUNE 老 tool output
    3. OpenCode 通用：SUMMARY（8-section anchored）
    4. microtrace 独有：标记 critical evidence
    5. 记录 CompactionRecord
    """
    ctx.append_reasoning("[COMPACTION] 触发")
    ctx.append_event("compaction.started", {"reason": "auto_overflow", "iteration": ctx.iteration})

    # 1. microtrace 独有：关键行提取
    for ev in ctx.evidence:
        if ev.source in ("log", "code", "tool_output"):
            ev.preserved_lines = extract_microtrace_critical_lines(ev.raw_content or "")

    # 2. OpenCode 通用：PRUNE
    pruned_count = _prune_old_tool_outputs(ctx)

    # 3. OpenCode 通用：SUMMARY
    previous_summary = ctx.compactions[-1].summary if ctx.compactions else None
    try:
        new_summary = await _summarize(ctx, llm, previous_summary)
    except Exception as e:
        ctx.append_reasoning(f"[COMPACTION] SUMMARY 失败: {e}，用 truncated fallback")
        new_summary = _truncated_fallback_summary(ctx)

    # 4. microtrace 独有：标记 critical evidence
    critical_ids = [ev.id for ev in ctx.evidence if ev.content_type == "critical"]

    # 5. 记录
    record = CompactionRecord(
        triggered_at_iteration=ctx.iteration,
        reason="auto_overflow",
        tokens_before=ctx.cumulative_tokens,
        tokens_after=len(new_summary) * 1.3,
        summary=new_summary,
        preserved_evidence_ids=critical_ids,
        pruned_count=pruned_count,
    )
    ctx.compactions.append(record)

    # 6. 精简 reasoning_trace
    ctx.reasoning_trace = [
        f"[COMPACTION] 已压缩 {pruned_count} 条 tool call",
        f"[COMPACTION] Summary: {new_summary[:200]}",
    ] + ctx.reasoning_trace[-3:]

    ctx.append_reasoning(f"[COMPACTION] 完成，pruned={pruned_count}, critical={len(critical_ids)}")
    ctx.append_event("compaction.ended", {"pruned": pruned_count, "critical": len(critical_ids)})


def _prune_old_tool_outputs(ctx: Context) -> int:
    """
    OpenCode 通用：PRUNE 老 tool output（不调 LLM）
    - 跳过最近 DEFAULT_TAIL_TURNS 轮
    - 跳过 PRUNE_PROTECTED_TOOLS
    - 清空 raw_content，标记 compacted=True

    ⚠️ Phase 1+ TODO: 当前实现按"tool call 数量"算 tail
    正确做法应该按"turn"算（一次 LLM stream = 1 turn，可能含多个 tool call）
    Phase 0 简化为按 tool call 算，复杂 turn 场景可能过早 prune
    """
    pruned = 0
    # 找最早的 tool call（非 tail turns）
    tool_calls = [tc for tc in ctx.tool_history if tc.state == "completed"]

    if len(tool_calls) <= DEFAULT_TAIL_TURNS:
        return 0

    for tc in tool_calls[:-DEFAULT_TAIL_TURNS]:
        if tc.name in PRUNE_PROTECTED_TOOLS:
            continue
        # 找对应 evidence
        for ev in ctx.evidence:
            if ev.tool_name == tc.name and not ev.compacted:
                ev.compacted = True
                ev.content = ev.content[:TOOL_OUTPUT_MAX_CHARS]  # 截取
                pruned += 1

    return pruned


async def _summarize(ctx: Context, llm, previous_summary: str | None) -> str:
    """
    OpenCode 通用：SUMMARY（调 LLM，8-section anchored）
    """
    # 收集非 critical evidence 作为 summarization 候选
    candidate_evidence = [
        ev for ev in ctx.evidence
        if ev.content_type != "critical" and not ev.compacted
    ]

    # 构建 summarization prompt（用 SUMMARY_TEMPLATE）
    prompt = _build_summary_prompt(ctx, candidate_evidence, previous_summary)

    # 调用 LLM
    response = await llm.complete(prompt)
    return response.strip()


SUMMARY_TEMPLATE = """
## Goal
- [single-sentence task summary]

## Constraints & Preferences
- [user constraints, preferences, specs, or "(none)"]

## Progress
### Done
- [completed work or "(none)"]
### In Progress
- [current work or "(none)"]
### Blocked
- [blockers or "(none)"]

## Key Decisions
- [decision and why, or "(none)"]

## Next Steps
- [ordered next actions or "(none)"]

## Critical Context
- [important technical facts, errors, or "(none)"]

## Relevant Files
- [file or directory path: why it matters, or "(none)"]

Rules:
- Keep every section, even when empty.
- Use terse bullets, not prose paragraphs.
- Preserve exact file paths, commands, error strings, and identifiers when known.
- Do not mention the summary process or that context was compacted.
"""


def _build_summary_prompt(
    ctx: Context,
    evidence: list,
    previous_summary: str | None,
) -> str:
    """构建 summary prompt（anchored update 或 new）"""
    evidence_text = "\n".join(
        f"- [{ev.source}] {ev.location}: {ev.content[:200]}"
        for ev in evidence[-10:]
    )

    if previous_summary:
        return (
            "Update the anchored summary below using the new evidence.\n"
            "Preserve still-true details, remove stale details, and merge in the new facts.\n\n"
            "<previous-summary>\n" + previous_summary + "\n</previous-summary>\n\n"
            f"New evidence:\n{evidence_text}\n\n"
            + SUMMARY_TEMPLATE
        )
    else:
        return (
            "Create a new anchored summary from the evidence.\n\n"
            f"Evidence:\n{evidence_text}\n\n"
            + SUMMARY_TEMPLATE
        )


def _truncated_fallback_summary(ctx: Context) -> str:
    """LLM summarization 失败时的 truncated fallback"""
    lines = ["## 压缩摘要 (truncated fallback)"]
    for ev in ctx.evidence[-10:]:
        if not ev.compacted:
            lines.append(f"- [{ev.source}] {ev.location}: {ev.content[:50]}...")
    return "\n".join(lines)
```

#### 4.9.3 5 条结构规则

```python
# src/microtrace/context/prompt.py

def determine_content_type(ev: Evidence, ctx: Context) -> str:
    """
    5 条结构规则：自动判定 evidence 的 content_type

    规则 1：堆栈帧里的关键 class
    规则 2：根因代码位置（@标记）
    规则 3：日志里 NPE 抛出点（"at X.java:line"）
    规则 4：早期 evidence（决定方向，iter ≤ max_iterations/2）
    规则 5：LLM 评 critical（importance 字段）
    """
    # 规则 1
    if ev.source == "stack" and ev.location:
        return "critical"

    # 规则 2
    if ev.source == "code" and "@" in ev.content:
        return "critical"

    # 规则 3
    if ev.source == "log" and "at " in ev.content and ".java:" in ev.content:
        return "critical"

    # 规则 4
    if ev.discovered_at_iteration <= ctx.max_iterations // 2:
        return "critical"

    # 规则 5
    if ev.importance == EvidenceImportance.CRITICAL:
        return "critical"

    return "compressible"
```

> 参考 DESIGN.md §8，§13

---

### 4.10 Doom Loop

#### 4.10.1 3 次精确匹配检测

```python
# src/microtrace/agent/doom_loop.py

DOOM_LOOP_THRESHOLD: int = 3  # 与 OpenCode 一致


def check_doom_loop(ctx: Context) -> bool:
    """
    Doom Loop 检测：最近 3 次 tool call 是否完全相同
    - 匹配条件：tool name + JSON 序列化 args 完全相同
    - 触发：进入 ASK_USER 弹窗
    """
    import json

    if len(ctx.tool_history) < DOOM_LOOP_THRESHOLD:
        return False

    last_calls = ctx.tool_history[-DOOM_LOOP_THRESHOLD:]
    first = last_calls[0]

    # 精确匹配
    if not all(
        tc.name == first.name and
        json.dumps(tc.args, sort_keys=True) == json.dumps(first.args, sort_keys=True)
        for tc in last_calls
    ):
        return False

    # 标记 doom loop
    ctx.doom_loop_tool = first.name
    ctx.doom_loop_args = first.args
    ctx.append_reasoning(
        f"[DOOM LOOP] 工具 {first.name} 被连续 {DOOM_LOOP_THRESHOLD} 次以相同参数调用"
    )
    return True
```

#### 4.10.2 Doom Loop 处理（4 选项）

```python
# src/microtrace/agent/doom_loop.py

from microtrace.context.models import QuestionPrompt, QuestionOption, State


async def handle_doom_loop(ctx: Context) -> None:
    """
    Doom Loop 触发后的处理
    - 构建 ASK_USER 弹窗（once/always/reject/custom 4 选项）
    - 由 REPL 层显示，用户选择后更新 allowed_tools / disabled_tools
    """
    last_call = ctx.tool_history[-1]

    ctx.pending_question = QuestionPrompt(
        header="Doom Loop (3次)",
        question=(
            f"工具 `{last_call.name}` 连续 3 次以相同参数调用。"
            f"\n输入={last_call.args_summary}。"
            f"\n你想怎么办？"
        ),
        options=[
            QuestionOption(
                label="继续",
                description="这一次允许，LLM 再调一次",
            ),
            QuestionOption(
                label="总是允许",
                description="本 session 内不再问",
            ),
            QuestionOption(
                label="拒绝",
                description="禁用此工具，LLM 必须换思路",
            ),
        ],
        multiple=False,
        custom=True,
    )
    # REPL 层会处理用户回复并调用 _apply_doom_loop_answer


async def apply_doom_loop_answer(ctx: Context, answer: str) -> None:
    """
    处理用户对 Doom Loop 弹窗的回复
    - 解析 answer（1/2/3/4 或直接文本）
    - 更新 allowed_tools / disabled_tools
    """
    if answer == "1" or answer.lower() == "继续" or answer.lower() == "once":
        # 继续：清空 doom_loop 标记，继续
        ctx.doom_loop_tool = None
        ctx.doom_loop_args = None

    elif answer == "2" or answer.lower() == "总是允许" or answer.lower() == "always":
        # 总是允许：加入 allowed_tools
        ctx.allowed_tools = getattr(ctx, 'allowed_tools', set())
        ctx.allowed_tools.add(ctx.doom_loop_tool)
        ctx.doom_loop_tool = None
        ctx.doom_loop_args = None

    elif answer == "3" or answer.lower() == "拒绝" or answer.lower() == "reject":
        # 拒绝：加入 disabled_tools（审计修复 2）
        ctx.disabled_tools.add(ctx.doom_loop_tool)
        ctx.doom_loop_tool = None
        ctx.doom_loop_args = None

    else:
        # 自定义：解析用户输入（可能是新参数或新指令）
        # 这里只是记录，实际解析由 REPL 层或 agent 继续处理
        ctx.append_reasoning(f"[DOOM LOOP] 用户自定义回复: {answer}")
        ctx.doom_loop_tool = None
        ctx.doom_loop_args = None
```

> 参考 DESIGN.md §2，§10

---

## 5. Prompts

### 5.1 prompts/agent.md 结构

```markdown
# prompts/agent.md - Master Prompt

> microtrace 的唯一 playbook。所有推理规则都在这里，不 hardcode。

## 问题类型 Taxonomy

## A. 业务报错

### 分类策略
（见下节）

### 调查流程

## B. 性能问题（Phase 1+）

## C. 内存溢出（Phase 1+）

## D. 服务重启（Phase 1+）

---

## 分类策略（Business Error）

### 三分类定义

**A. 本产品 Bug**
定义：...

**B. 下游产品报错**
定义：...

**C. 使用方法问题**
定义：...

### 分类决策树

---

## 工具使用指南

### read_file
适用场景：...

### search_logs
适用场景：...

### find_class
适用场景：...

### parse_stack_trace
适用场景：...

---

## Parallel tool calls

If you need to call multiple
### 5.2 MAX_ITERATIONS_REACHED Prompt

```markdown
## MAX_ITERATIONS_REACHED

CRITICAL - MAXIMUM ITERATIONS REACHED

The maximum number of iterations ({max_iterations}) for this investigation has been reached.
Tool calls are disabled. You MUST respond with text only.

Required content:
1. Statement that max iterations have been reached
2. Summary of what has been investigated (with evidence references)
3. Current best judgment (A/B/C) and reasoning
4. List of what you could NOT verify (remaining gaps)
5. Recommendation for what should be investigated next

DO NOT make any tool calls. Text only.
```

### 5.3 AskUserQuestion 格式

```markdown
## ASK_USER 触发格式

When you need user input, respond with:

```
{@action: ask_user, question: 你的问题内容}
```

多选格式（让 LLM 学会这样问）：

```
{@action: ask_user, question: 报错时间窗口大概是？
options:
1. 10:00 - 11:00
2. 10:23 - 10:30
3. 不知道
4. 自定义...
}
```

### 5.4 OpenCode 规则借鉴（直接写入 agent.md）

```markdown
## ASK_USER 原则（防漂移）

- Default: do the work without asking questions. Treat short tasks as
  sufficient direction; infer missing details by reading the codebase.

- Questions: only ask when you are truly blocked AND you cannot safely
  pick a reasonable default. This usually means one of:
  * The request is ambiguous in a way that materially changes the result
  * The action is destructive/irreversible, touches production
  * You need a secret/credential/value that cannot be inferred

- If you must ask: do all non-blocked work first, then ask exactly ONE
  targeted question, include your recommended default, and state what
  would change based on the answer.

- Never ask permission questions like "Should I proceed?"; proceed with
  the most reasonable option and mention what you did.
```

> 参考 DESIGN.md §7

---

## 6. Configuration

### 6.1 config.yaml Schema

```yaml
# ~/.config/microtrace/config.yaml
# Windows: %APPDATA%\microtrace\config.yaml
# macOS: ~/Library/Application Support/microtrace/config.yaml
# Linux: ~/.config/microtrace/config.yaml

agent:
  max_iterations: 8  # 默认 8，可改（Q4）
  # compaction_buffer: 20000  # Phase 1+ 启用

llm:
  provider: minimax  # 固定（Q6）
  model: MiniMax-M3-highspeed
  # api_key 从环境变量 MICROTRACE_API_KEY 读取，不写在文件里
  base_url: https://api.minimax.chat/v1
  timeout: 120.0  # 秒
```

### 6.2 环境变量

| 环境变量 | 优先级 | 说明 |
|---------|-------|------|
| `MICROTRACE_API_KEY` | 最高 | LLM API Key |
| `MICROTRACE_BASE_URL` | 次高 | LLM base URL（换 provider 时用）|
| `MICROTRACE_MODEL` | 次高 | 模型名 |

### 6.3 platformdirs 路径映射

```python
# src/microtrace/config.py
from platformdirs import user_config_dir, user_data_dir

def get_config_path() -> Path:
    """config.yaml 路径"""
    return Path(user_config_dir("microtrace", appname="microtrace")) / "config.yaml"

def get_db_path() -> Path:
    """SQLite 数据库路径"""
    return Path(user_data_dir("microtrace", appname="microtrace")) / "state.db"

def get_history_path() -> Path:
    """REPL history 路径"""
    return Path(user_data_dir("microtrace", appname="microtrace")) / "history"
```

> 参考 DESIGN.md §6，§12

---

## 7. Error Handling

### 7.1 错误层级

```python
# src/microtrace/agent/types.py

class AgentError(Exception):
    """Agent 错误基类"""
    pass


class ToolError(AgentError):
    """工具执行失败"""
    def __init__(self, tool_name: str, message: str):
        super().__init__(f"Tool {tool_name} failed: {message}")
        self.tool_name = tool_name


class ParseError(AgentError):
    """解析错误"""
    pass


class ContextOverflowError(AgentError):
    """Context 溢出（触发 compaction）"""
    pass


class RetryExhaustedError(AgentError):
    """重试次数耗尽"""
    pass


class PermanentError(AgentError):
    """不可重试错误（已耗尽重试）"""
    pass
```

### 7.2 Retry 分类

| 错误类型 | 可重试？ | 分类 |
|---------|--------|------|
| NetworkError | ✅ | transient |
| RateLimitError | ✅ | transient |
| ServerError (5xx) | ✅ | transient |
| TimeoutError | ✅ | transient |
| BadRequestError (400) | ❌ | permanent |
| AuthError (401/403) | ❌ | permanent |
| NotFoundError (404) | ❌ | permanent |
| ToolError | ❌ | permanent（作为 evidence 喂 LLM）|

### 7.3 兜底输出

| 场景 | 兜底输出 |
|------|---------|
| MAX_ITERATIONS LLM 失败 | `_format_judgment_fallback(ctx)` - 基于 last judgment |
| Compaction SUMMARY 失败 | `_truncated_fallback_summary(ctx)` - 简单截取 evidence 标题 |
| INTAKE 空输入 | "Empty input" → EXIT |
| INTAKE 解析失败 | 降级 Problem（部分字段为空）+ 继续 INVESTIGATE |

> 参考 DESIGN.md §11，§13

---

## 8. Testing Strategy

### 8.1 单元测试场景

#### State Machine
- 每个状态的 enter/tick/exit
- 19 条状态转换全覆盖
- INTAKE 空输入 → EXIT（修复 3）
- INTAKE 解析失败 → EXIT（修复 3）

#### Doom Loop
- 3 次精确匹配 → 触发
- 2 次 → 不触发
- 不同参数 → 不触发
- once/always/reject 自定义处理

#### Compaction
- overflow 检测（20K buffer）
- PRUNE 保留最近 2 轮
- SUMMARY 失败 → truncated fallback
- 关键行提取（8 种 pattern）

#### Context / Prompt
- importance+relevance 排序
- 5 条结构规则
- disabled_tools 注入（修复 2）

#### Tools
- 每个工具的 Happy Path
- 错误处理（文件不存在 / 超时）
- 参数验证

#### LLM Client
- 5 次重试 + 指数退避
- Retry-After header 遵守
- permanent error 不重试

#### Persistence
- save + load roundtrip
- ASK_USER 状态保存（修复 1）
- resume 恢复完整 ctx

### 8.2 集成测试场景

1. **完整 session**：输入 → INTAKE → INVESTIGATE × N → CONCLUDE
2. **Doom Loop 触发**：连续 3 次相同工具调用 → ASK_USER → 用户回复 → 继续
3. **Compaction 触发**：大量 evidence → overflow → compaction → 继续
4. **MAX_ITERATIONS**：8 轮后 → 强制总结
5. **Resume**：`microtrace resume <id>` → 恢复完整状态

### 8.3 Windows 验证清单

- [ ] `python -m microtrace` 启动
- [ ] REPL 中文输入/输出正常
- [ ] `microtrace sessions` 列表正常
- [ ] `microtrace resume <id>` 恢复正常
- [ ] 配置文件读写（`%APPDATA%\microtrace\`）
- [ ] SQLite DB 创建在正确路径
- [ ] 4 个工具路径处理

### 8.4 手动测试 Checklist

```
□ 启动 microtrace
□ 输入一个问题（粘贴堆栈）
□ INTAKE 解析
□ 第 1 轮工具调用（find_class / search_logs / read_file / parse_stack_trace）
□ 第 2-3 轮继续调查
□ /status 命令
□ /evidence 命令
□ /judgment 命令
□ Doom Loop 触发（连续 3 次相同调用）
□ ASK_USER 回答
□ MAX_ITERATIONS 到达（强制总结）
□ /save 保存
□ /exit 退出
□ microtrace sessions 列表
□ microtrace resume <id> 恢复
```

> 参考 DESIGN.md §13

---

## 9. Implementation Phases

### Phase 0 详细 Checklist（10-20 个任务）

| # | 任务 | 依赖 | 优先级 |
|---|------|------|--------|
| 1 | 项目结构 + pyproject.toml + 依赖安装 | — | P0 |
| 2 | Config 模块（platformdirs + config.yaml）| — | P0 |
| 3 | Context 数据模型（全部 Pydantic models）| 1 | P0 |
| 4 | State 枚举 + handler + 19 条转换矩阵 | 1 | P0 |
| 5 | LLMClient Protocol + MiniMaxClient | 1 | P0 |
| 6 | 4 个工具（base + 4 个实现）| 1 | P0 |
| 7 | ToolRegistry | 6 | P0 |
| 8 | _assemble_prompt + 5 条结构规则 | 3 | P0 |
| 9 | run_session + agent_iteration（双层结构）| 4, 5, 6, 8 | P0 |
| 10 | INTAKE / CONCLUDE / MAX_ITERATIONS 辅助 | 3, 5 | P0 |
| 11 | SQLite persistence（save + load + sessions）| 1, 3 | P0 |
| 12 | REPL 入口 + 状态 banner | 1, 4 | P0 |
| 13 | REPL 命令集（/status /evidence /save 等）| 3, 12 | P0 |
| 14 | Doom Loop 检测 + 4 选项处理 | 4, 7 | P0 |
| 15 | Compaction（PRUNE + SUMMARY + 关键行提取）| 3, 8 | P0 |
| 16 | prompts/agent.md（Master prompt）| — | P0 |
| 17 | FastAPI HTTP API | 1, 3, 9 | P1 |
| 18 | Windows console 兼容（_setup_windows_console）| 1, 12 | P0 |
| 19 | 单元测试（每个模块）| 各模块 | P0 |
| 20 | 集成测试（end-to-end）| 1-18 | P0 |

### Phase 1+ 路线图

| 阶段 | 内容 | 依赖 |
|------|------|------|
| Phase 1 | FastAPI HTTP API + CORS + Web UI 前端 | Phase 0 全部 |
| Phase 2 | 性能问题类型（CPU / 内存 / IO）| prompts/agent.md 扩展 |
| Phase 3 | OOM 问题类型（heap / metaspace）| prompts/agent.md 扩展 |
| Phase 4 | 服务重启问题类型 | prompts/agent.md 扩展 |
| Phase 5 | 案例知识库（RAG）| Phase 0 跑通 |
| Phase 6 | 多语言支持（Go / Python）| 架构扩展 |

> 参考 VISION.md §5，DESIGN.md §9

---

## 10. Open Questions / Future Work

### 10.1 YAGNI 候选

| 候选 | 说明 | 启用条件 |
|------|------|---------|
| 代码层 rate limit | AskUserGuard 类 | 发现 LLM 真乱问 |
| compaction buffer 可配置 | config.yaml 加字段 | 用户反馈 20K 不合适 |
| Judgment history 清空命令 | `/judgment clear` | history 太多 |
| 多 LLM 路由 | 换 provider | MiniMax 不够用 |
| Tool dependency 检查 | 静态分析 | LLM 经常乱序 |
| Status tracking | SessionStatus 丰富 | Phase 1+ Web UI |

### 10.2 待老板拍板（实现层面）

> 以下是实现时发现的未明确事项，需要实现时判断：

1. **SQLite db_path 默认创建**：如果目录不存在，是否自动创建？（实现时会自动创建）

2. **REPL history 路径**：是否放在 `platformdirs` 管理的目录下？（实现时放在 `get_history_path()`）

3. **judgment_history 最大长度**：无限追加还是限制？（实现时默认不限，Phase 1+ 可加 `/judgment clear`）

4. **HTTP API 认证**：Phase 1+ 做还是不做？（Phase 0 不做，Phase 1+ 加 Bearer token）

5. **Tool 超时默认值**：30s 还是 60s？（实现时默认 60s，可配置）

---

## 附录 A：项目结构

```
microtrace/
├── src/
│   └── microtrace/
│       ├── __init__.py
│       ├── __main__.py              # python -m microtrace
│       ├── cli.py                   # Typer CLI（sessions / resume / delete）
│       ├── config.py                # platformdirs + config.yaml
│       ├── agent/
│       │   ├── __init__.py
│       │   ├── state.py             # State 枚举 + handler + transition
│       │   ├── events.py            # AgentEvent
│       │   ├── loop.py              # run_session + agent_iteration
│       │   ├── doom_loop.py         # check_doom_loop + handle_doom_loop
│       │   └── types.py             # AgentError 等
│       ├── context/
│       │   ├── __init__.py
│       │   ├── models.py            # 全部 Pydantic models
│       │   ├── compaction.py        # compact + PRUNE + SUMMARY + 关键行
│       │   └── prompt.py            # _assemble_prompt + 5 条规则
│       ├── tools/
│       │   ├── __init__.py          # ToolRegistry
│       │   ├── base.py              # Tool + ToolInput + ToolResult
│       │   ├── read_file.py
│       │   ├── search_logs.py
│       │   ├── find_class.py
│       │   └── parse_stack_trace.py
│       ├── llm/
│       │   ├── __init__.py
│       │   ├── client.py            # LLMClient Protocol + 错误层级
│       │   └── minimax.py          # MiniMaxClient 实现
│       ├── repl/
│       │   ├── __init__.py
│       │   ├── main.py              # run_repl + _setup_windows_console
│       │   ├── renderer.py          # RichRenderer
│       │   └── commands.py         # /status /evidence /save 等
│       ├── http/
│       │   ├── __init__.py
│       │   └── api.py              # FastAPI /chat /state /evidence /save
│       └── persistence/
│           ├── __init__.py
│           └── sqlite.py            # save/load/list_sessions
├── prompts/
│   └── agent.md                     # Master prompt
├── tests/
│   ├── __init__.py
│   ├── test_state_machine.py
│   ├── test_loop.py
│   ├── test_doom_loop.py
│   ├── test_compaction.py
│   ├── test_tools.py
│   ├── test_context.py
│   └── test_persistence.py
├── docs/
│   ├── VISION.md
│   ├── DESIGN.md
│   └── SPEC.md
├── pyproject.toml
└── README.md
```

---

## 附录 B：环境要求

| 要求 | 版本 | 说明 |
|------|------|------|
| Python | **3.11+** | 必须（tomllib 等特性）|
| pip | 最新 | 安装依赖 |
| SQLite | 自带 | Python stdlib，无需安装 |
| 操作系统 | Windows / macOS / Linux | 跨平台 |

### Windows 安装步骤

```powershell
# 1. 安装 Python 3.11+
winget install Python.Python.3.11

# 2. 验证
python --version

# 3. 安装 microtrace
pip install -e .

# 4. 运行
microtrace
```

### macOS / Linux 安装步骤

```bash
# 1. 确保 Python 3.11+
python3 --version

# 2. 安装 microtrace
pip install -e .

# 3. 运行
microtrace
```

---

## 附录 C：参考

| 来源 | 参考内容 | SPEC 章节 |
|------|---------|---------|
| DESIGN.md §2 | Loop 设计 / 流式 / 事件驱动 | §4.2 |
| DESIGN.md §3 | 状态机设计 / 5 态 / 事件溯源 | §4.1 |
| DESIGN.md §4 | Context 数据结构 / Compaction | §3.2, §4.3, §4.9 |
| DESIGN.md §5 | v1 vs v2 差异 | §1 |
| DESIGN.md §6 | 技术选型 | §2, §6 |
| DESIGN.md §7 | prompts 设计原则 | §5 |
| DESIGN.md §8 | Compaction 策略（OpenCode 共用 + microtrace 独有）| §4.9 |
| DESIGN.md §9 | 待老板拍板 | §10 |
| DESIGN.md §11 | Retry 策略（5 次 + 指数退避）| §4.5 |
| DESIGN.md §12 | Windows 兼容 | §4.7, §6 |
| DESIGN.md §13 | 状态转换审计（4 个修复）| §4.1, §4.2, §4.6 |
| VISION.md §1 | 核心故事 | §1 |
| VISION.md §3 | 必杀技 | §1 |
| VISION.md §4 | 问题类型三分类 | §5 |
| VISION.md §6 | Phase 0 最小集 | §1 |
| OpenCode processor.ts | 流式 LLM / Doom Loop / overflow | §4.2, §4.10 |
| OpenCode compaction.ts | PRUNE / SUMMARY_TEMPLATE / tail turns | §4.9 |
| OpenCode question/index.ts | QuestionPrompt schema | §3.2.8 |
| OpenCode session-event.ts | 25+ 事件类型 | §3.2.9 |

---

*本文档由 SPEC subagent 自动生成，基于 DESIGN.md v2 + VISION.md v3。*
*如有不一致，以 DESIGN.md 为准。*
