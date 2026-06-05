# ARCHITECTURE.md - microtrace

> 📌 初稿（2026-06-05）—— 待老板逐节刷新

---

## 1. 状态机（3 态）

```
            ┌──────────┐
            │  INTAKE  │  解析输入
            └─────┬────┘
                  │
                  ▼
            ┌──────────┐ ◀──────┐
            │ INVESTIG │ ─────┘ （loop 内部循环）
            │  ATE     │
            └─────┬────┘
                  │  LLM 说"够了" / 达上限
                  ▼
            ┌──────────┐
            │ CONCLUDE │  格式化输出
            └──────────┘
```

### 各态职责

| 态 | 触发进入 | 职责 | 退出条件 |
|---|---|---|---|
| **INTAKE** | 收到原始报错 | 提取 error_type、stack class+method+line、timestamp、log snippets → 结构化 `Problem` | 解析完成 |
| **INVESTIGATE** | INTAKE 完成 | LLM 决策：调工具 / 更新判断 / 输出结论（loop 在这里） | LLM 给"够了我要输出" / 达 max_iterations / 证据饱和 |
| **CONCLUDE** | INVESTIGATE 退出 | 把 judgment + evidence + reasoning 格式化成给用户看的结论 | 输出完成 |

---

## 2. Loop（INVESTIGATE 内部）

```python
def investigate_loop(context: Context, llm: LLMClient, tools: ToolRegistry) -> None:
    while context.iteration < context.max_iterations:
        context.iteration += 1
        
        # 1. 组装 prompt
        prompt = build_prompt(context)  # 截取 + 排序
        
        # 2. LLM 推理
        response = llm.complete(prompt, tools=tool_schemas)
        
        # 3. 解析响应，三选一
        if response.is_tool_call:
            result = tools.invoke(response.tool_name, response.tool_args)
            context.add_evidence(result)
            context.tool_history.append(...)
        elif response.is_judgment_update:
            context.current_judgment = response.judgment
        elif response.is_conclusion:
            context.final_output = response.output
            return  # 退出 loop
        
        # 4. safety：检查 stop 信号
```

**关键设计决策**：
- 永远只有一个 `current_judgment`（**不是多假设竞争**）
- loop 退出由 LLM 自决（**不是外部规则**）
- max_iterations 是兜底（**不是主退出条件**）
- evidence 只增不减（**截取在 prompt 组装层做**）

---

## 3. Context 数据模型

```python
@dataclass
class Problem:
    raw_input: str                  # 用户原始报错
    error_type: str                 # NullPointerException 等
    stack_frames: list[StackFrame]  # 堆栈（含 class+method+line）
    log_snippets: list[str]         # 关联日志片段（如果有）
    timestamp: datetime | None      # 报错时间

@dataclass
class Judgment:
    category: Literal["A", "B", "C", "UNKNOWN"]
    confidence: float               # 0~1
    one_line_reason: str            # 一句话理由
    reasoning: str                  # 详细推理

@dataclass
class Evidence:
    id: str
    source: Literal["code", "log", "stack", "tool_output"]
    location: str                   # file:line / log line range / tool name
    content: str                    # 证据内容（截取后）
    relevance: float                # 0~1（LLM 自评或工具打分）
    discovered_at: int              # 第几轮发现（用于 trace）

@dataclass
class ToolCall:
    name: str
    args_summary: str               # 参数摘要（不存完整内容）
    output_summary: str             # 输出摘要
    iteration: int

@dataclass
class Context:
    problem: Problem
    current_judgment: Judgment
    evidence: list[Evidence]        # 只增不减
    tool_history: list[ToolCall]    # 避免重复调
    reasoning_trace: list[str]      # 最近 N 步推理（"调了 X，发现 Y，所以 Z"）
    final_output: str | None
    iteration: int = 0
    max_iterations: int = 8         # 兜底
```

---

## 4. Prompt 组装策略

喂给 LLM 的 prompt 结构（**不是把 context 整个塞进去**）：

```
1. 问题描述
   - 原始输入（截取到 2000 字）
   - 结构化 Problem（class+method+line+timestamp）

2. 当前判断
   - 类别 + 置信度 + 一句话理由

3. 证据池（按 relevance 排序，截前 5 条）

4. 推理轨迹（最近 3 步）

5. 可用工具列表（tool 描述 + 参数 schema）

6. 指令
   - 你的任务：决定下一步 [调工具 / 更新判断 / 输出结论]
   - 退出条件：你认为证据已充分
   - 兜底：已用 {iteration}/{max_iterations} 轮
```

**截取策略**（**不在 context 层做，在 prompt 组装层做**）：
- 问题原始输入：超过 2000 字截取
- 证据池：按 relevance 倒序，取前 5
- 推理轨迹：取最近 3 步
- 单条证据 content：超过 500 字截取

---

## 5. 反 v1 陷阱

| v1 干了 | microtrace 不做 |
|---|---|
| INTAKE/ASSESSMENT/HYPOTHESIZING 等 6 态 | **3 态** |
| Bayesian 多假设 posterior_probability | **单 judgment + confidence** |
| 状态机里塞 Phase 概念 | **状态只代表"在干啥"** |
| 复杂 Evidence 子类 | **简单 4 字段** |
| context 压缩策略 | **不压缩，截取 + 排序** |
| 反事实验证（停滞触发） | **不用，loop 退出靠 LLM 自决** |
| Doom Loop 检测 | **不用，简单轮数上限** |
| 危险操作分级 | **不用，Phase 0 工具都安全** |

---

## 6. 待刷新（TODO）

- [ ] 3 态够不够（要不要把"初始判断"单独成 HYPOTHESIZE 态？）
- [ ] loop 退出条件（靠 LLM 自决够不够稳，要不要"连续 2 轮无新证据"保险？）
- [ ] evidence 截取策略（relevance 排还是时间倒序？）
- [ ] Prompt 模板（具体怎么写）
- [ ] 工具 schema（4 个最小工具的输入输出定义）
- [ ] 错误处理（工具失败、LLM 解析失败、context 溢出）
