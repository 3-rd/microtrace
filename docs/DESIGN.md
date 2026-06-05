# DESIGN.md - L2 Agent Core Mechanisms

> 📌 调研 + 设计稿（2026-06-05）—— 基于 OpenCode / OpenClaw 调研，针对 VNFM 问题定位场景

---

## 0. 设计目标

### 0.1 场景特征

VNFM 工程师面对的是：

- **海量代码**：千万行级 Java 多微服务，调用关系复杂（北向南向）
- **问题类型开放**：业务报错 / 性能 / OOM / 重启，不锁死
- **3 分类锚点**：A（本产品 Bug）/ B（下游报错透传）/ C（使用方法问题）
- **证据严苛**：结论必须引代码行 / 日志片段，不允许"我觉得是"
- **数据不出域**：事故数据敏感，本地部署

### 0.2 L2 设计要解决的核心问题

| 问题 | 来源 | 设计回应 |
|---|---|---|
| LLM 在多轮推理中"忘了目标" | VNFM 问题复杂，8 轮仍不够 | **状态机 + 单 judgment 持久化**，每轮对焦 |
| evidence 膨胀导致 context 溢出 | 千万行代码 + 多轮调工具 | **压缩在 prompt 组装层做**，context 层只存储 |
| 同一工具重复调但无新信息 | 日志搜索返回空结果，LLM 还搜 | **退一步策略**（step-back）+ 推理轨迹去重 |
| LLM 无法自行判断"够不够" | 过度推理（烧 token）或过早停 | **LLM 自决 + max_iter 兜底 + 证据饱和检测** |
| 用户需要补充信息怎么办 | 日志不全、代码路径不清 | **ASK_USER 态**，主动中断 loop 请求补料 |

### 0.3 指导原则

- **单例 agent**：不是 multi-agent 协作，VNFM 场景是单人推理
- **LLM 自决为主，人工干预为辅**：loop 退出靠 LLM，但用户随时可介入
- **context 只追加不回退**：压缩在 prompt 层做，不改原始 evidence
- **prompt 即 playbook**：问题类型 taxonomy 在 prompt 里，不 hardcode

---

## 1. 借鉴来源

### 1.1 OpenCode — 核心机制参考

来源：`https://github.com/anomalyco/opencode`（非 `sst/opencode`，后者不存在）

**已确认事实**（来自 GitHub README / 官方文档 / 第三方深度解析）：

#### Loop 机制

- **ReAct 模式**（而非 Plan-and-Execute）：LLM 每轮同时输出 `thought + action` 或 `text response`，工具调用由 LLM 自决策
- **max_steps 配置**：每个 agent 可配置最大迭代次数，到达后强制输出文本结论（来自 OpenCode 文档 "max steps" 章节）
- **loop 退出三元**：LLM 自决 text response / 达 max_steps / 用户中断

**对 microtrace 的影响**：采用 ReAct 而非 Plan-then-Execute，因为 VNFM 问题需要"边查边想"，计划与执行交织

#### 状态管理

- **两层状态机**（来自 DEV.to 深度解析文章）：member lifecycle 5 态（ready / busy / shutdown_requested / shutdown / error）+ execution status 10 态（跟踪 prompt loop 具体位置）
- **两层分离的原因**：UI 需要粗粒度（member status），恢复逻辑需要细粒度（execution status）

**对 microtrace 的影响**：我们的状态机不需要这么细，但"状态纯用于推理控制，不用于 UI"的思路值得借鉴

#### Context 机制

- **Auto-compaction at 95%**：上下文达到 95% 时自动触发压缩 agent（hidden system agent），将对话历史压缩为摘要
- **AGENTS.md 不压缩**：规则文件始终全量注入，不走压缩流程
- **Prompt hierarchy**：provider header → provider prompt → environment → AGENTS.md → agent-specific prompt → user override

**对 microtrace 的影响**：
- 压缩阈值 95% → 我们用 90%（VNFM 场景 evidence 更重）
- AGENTS.md 类文件（prompts/agent.md）不压缩
- Prompt 组装分层，context 只存储不压缩

#### 工具注册

- **Tool.define 模式**：`Tool.define("name", { description, parameters: ZodSchema, execute })`
- **工具声明与执行分离**：LLM 拿到的是 tool description + input schema，实际执行在本地
- **内置工具集**：BashTool / EditTool / ReadTool / GlobTool / GrepTool / ListTool / WriteTool 等

**对 microtrace 的影响**：4 个工具统一走 `Tool.define` 注册范式

#### System Prompt 示例（来自第三方解析的 Gemini prompt）：

```
You are opencode, an interactive CLI agent specializing in software engineering tasks.
...
# Operational Guidelines
## Tone and Style
- Concise & Direct: ... fewer than 3 lines of text output
- No Chitchat: Avoid conversational filler
## Tool Usage
- File Paths: Always use absolute paths
- Parallelism: Execute multiple independent tool calls in parallel
...
```

**对 microtrace 的影响**：prompt 要短、指令要具体、"少说话多做事"是正确风格

#### Multi-Agent 协作（OpenCode Agent Teams）

- **inbox + session injection**：append-only JSONL 文件做 inbox（O(1) 写），配合 session injection 做消息投递
- **fire-and-forget spawn + auto-wake**：spawn 立即返回，idle recipient 通过 auto-wake 机制被唤醒
- **peer-to-peer messaging**：任何 teammate 可直接 message 任何其他 teammate，不走 lead relay
- **doom_loop recovery**：`doom_loop` permission key 对应"agent appears stuck"时的恢复提示词

**对 microtrace 的影响**：我们单 agent 无需此复杂度，但"doom_loop 检测"思路（`permission.doom_loop`）对 VNFM 的"工具重复调用无新信息"问题有参考价值

#### 未确认信息（标注：没找到资料）

- ❌ OpenCode 的 LLM 响应解析层代码（tool_call / text response 解析逻辑）
- ❌ OpenCode 的具体 ReAct prompt 模板内容
- ❌ OpenCode 的 context 压缩具体算法（summarize 还是 truncate？）

### 1.2 OpenClaw — 架构组织参考

来源：`~/.openclaw/workspace/` 下的 workspace 文件

**已确认事实**：

#### Skills 架构

- **SKILL.md 范式**：技能描述用 YAML frontmatter（name + description）开头，Markdown 内容紧随其后
- **Skills 发现机制**：Skills 在 `~/.openclaw/workspace/skills/`（workspace 内）和 `~/.openclaw/extensions/*/skills/`（扩展目录）下被发现
- **按需加载**：Skill 被一个 skill tool 按名称加载，内容注入 context
- **与 AGENTS.md 的区别**：AGENTS.md 总是全量注入，SKILL.md 按需加载

**对 microtrace 的影响**：
- microtrace 的 `prompts/agent.md` 走 AGENTS.md 模式（全量注入）
- 未来 L3 知识库工具可走 SKILL.md 模式（按需加载）

#### Memory 体系

- **daily notes**：`memory/YYYY-MM-DD.md`，每次会话结束追加，记录"发生了什么"
- **long-term memory**：`MEMORY.md`，从 daily notes 提炼精华， curated 而非 raw
- **加载时序**：session 启动时读 SOUL.md → USER.md → memory/ (today+yesterday) → MEMORY.md（main session only）

**对 microtrace 的影响**：
- microtrace 的案例库可借鉴此分层：raw case 日志 → 精选 case
- 但 Phase 0 不做案例库，纯代码 + 日志推理

#### Session 模型

- **main session vs isolated session**：main session 加载 MEMORY.md，isolated 不加载（隔离）
- **subagent 模式**：`sessions_spawn` 创建独立 sub-session，有自己的 context，可独立运行
- **heartbeat 机制**：定期心跳做主动任务（检查邮件、日历等），与 session 分离

**对 microtrace 的影响**：VNFM 场景用单 session 足够，但"状态机每态行为"可以借鉴 OpenClaw 的"态进入/退出时做什么"

#### Prompt 组装

- **system + context + tools** 三层拼装（从 SOUL.md / AGENTS.md 的加载顺序反推）
- **workspace 文件即 context**：所有 `.md` 文件在 session 启动时被扫描，部分注入 context

**对 microtrace 的影响**：microtrace 的 prompt 也按 system（agent.md）/ context（problem + judgment + evidence）/ tools（4 个工具）三层组装

### 1.3 Clowder AI — 已知不存在

`/Users/fucy/AIAgent/clowder-ai/` 不存在，跳过。

### 1.4 microtrace v1（ARCHITECTURE.md）

已有 ARCHITECTURE.md 的 3 态设计是初稿基础，设计时参考并明确改进点（见第 5 节）。

---

## 2. Loop 设计

### 2.1 控制结构

```python
def agent_loop(initial_input: str, ctx: Context, llm: LLMClient, tools: ToolRegistry) -> str:
    """
    主循环：状态机驱动，单 judgment 持久化，ReAct 模式
    退出路径：LLM conclude / max_iterations / 用户中断 / ASK_USER
    """
    # ── 状态机初始化 ──
    ctx.state = State.INTAKE
    ctx.iteration = 0

    # ── INTAKE 态：解析原始输入 ──
    ctx = _intake(ctx, initial_input, llm, tools)
    if ctx.state == State.EXIT:
        return ctx.final_output

    # ── INVESTIGATE 态：主循环 ──
    ctx.state = State.INVESTIGATE
    while True:
        ctx.iteration += 1
        ctx.reasoning_trace.append(f"[开始第 {ctx.iteration} 轮]")

        # 2.1 检查退出条件
        if ctx.iteration > ctx.max_iterations:
            ctx.state = State.CONCLUDE
            ctx.append_reasoning("达到最大迭代次数，强制结束")
            break

        if ctx.user_interrupt:
            ctx.state = State.CONCLUDE
            ctx.append_reasoning("用户主动中断")
            break

        # 2.2 Prompt 组装（截取 + 排序）
        prompt = _assemble_prompt(ctx, tools)

        # 2.3 LLM 推理
        response = llm.complete(prompt, tools=tools.schemas())

        # 2.4 响应解析（核心分支）
        branch = _parse_response(response)

        if branch.type == "tool_call":
            # ── 分支 1：工具调用 ──
            # 退一步检测：同一工具 + 相近参数 → 警告但不阻止
            if _is_step_back(ctx, branch.tool_name, branch.tool_args):
                ctx.append_reasoning(
                    f"[警告] 疑似重复调用 {branch.tool_name}，"
                    f"上次结果：{ctx.last_tool_result_summary}"
                )
            # 执行工具
            result = tools.invoke(branch.tool_name, branch.tool_args)
            ctx.tool_history.append(ToolCall(
                name=branch.tool_name,
                args_summary=_summarize(branch.tool_args),
                output_summary=_summarize_output(result),
                iteration=ctx.iteration,
                result=result,  # 存完整结果（用于 context 压缩评估）
            ))
            ctx.last_tool_result_summary = _summarize_output(result)
            # 证据追加
            ctx.add_evidence(_result_to_evidence(result, branch.tool_name, ctx.iteration))

        elif branch.type == "judgment_update":
            # ── 分支 2：判断更新 ──
            old_judgment = ctx.current_judgment
            ctx.current_judgment = branch.judgment
            ctx.append_reasoning(
                f"判断更新：{old_judgment.category}→{branch.judgment.category}，"
                f"理由：{branch.judgment.one_line_reason}"
            )

        elif branch.type == "ask_user":
            # ── 分支 3：主动询问用户 ──
            ctx.state = State.ASK_USER
            ctx.pending_question = branch.question
            ctx.append_reasoning(f"向用户提问：{branch.question}")
            # 让出控制权，等待用户回复
            yield "ASK_USER", branch.question
            # 用户回复后继续（不 increment iteration，因为这不是完整一轮）
            ctx.state = State.INVESTIGATE
            # 把用户回复作为 context 的一部分
            ctx.add_user_reply(branch.question, last_user_message)

        elif branch.type == "conclude":
            # ── 分支 4：LLM 认为结论已充分 ──
            ctx.state = State.CONCLUDE
            ctx.final_output = branch.output
            ctx.append_reasoning(f"LLM 自决结束：{branch.summary}")
            break

        else:
            # 解析失败，降级为文本回复（不 crash）
            ctx.append_reasoning(f"[警告] 无法解析 LLM 响应类型：{branch.raw[:100]}")
            ctx.state = State.CONCLUDE
            ctx.final_output = branch.raw
            break

    # ── CONCLUDE 态：格式化输出 ──
    return _format_output(ctx)
```

### 2.2 单次迭代的原子动作

每个 iteration = 1 次 LLM call + 0~N 次工具执行（并行工具调用由 LLM 在同一条响应里声明，OpenCode 的 parallelism 设计）。

```
iteration 原子动作序列：
1. 检查退出条件（iter 超限 / 用户中断）
2. 组装 prompt（system + problem + judgment + evidence + reasoning_trace + tools）
3. llm.complete() → 等待响应
4. 解析响应类型（tool_call / judgment_update / ask_user / conclude）
5. 执行对应动作（invoke tool / update judgment / yield ask_user / break）
6. 记录推理轨迹（reasoning_trace.append）
```

### 2.3 错误恢复

| 错误类型 | 处理策略 |
|---|---|
| **工具执行失败**（文件找不到 / 日志空 / 超时） | 工具返回 error result → 作为 evidence 记录（标注 source=error）→ 继续 loop |
| **LLM 解析失败**（响应不符合 schema） | 记录原始响应前 200 字到 reasoning_trace → 降级为 conlcude |
| **网络中断 / API 超时** | 最多重试 3 次，间隔 2s/4s/8s（指数退避）→ 仍失败则 abort |
| **工具超时**（单工具 > 30s） | 工具层设置 timeout → 返回 `ToolTimeoutError` → 记录 → 继续 |
| **LLM 响应为空** | 记录 warning → 重试 1 次 → 仍空则 abort |

**OpenCode 启示**：OpenCode 的 `doom_loop` permission 用于"agent appears stuck"恢复提示，microtrace 将"工具重复调用无新信息"作为显式检测点（退一步策略）。

### 2.4 主动询问用户（ASK_USER 分支）

```python
# LLM 在 INVESTIGATE 循环中说"我需要 X 信息才能继续"
# → 状态切为 ASK_USER，yield 问题给用户
# → 用户回复后 → 状态切回 INVESTIGATE → 用户回复作为 context 补充
```

**触发条件**：LLM 在推理中发现关键信息缺失（如"报错时间具体是几点"、"是哪个环境"）。

**不阻断 loop**：不是 await 死等，而是 yield 后继续，用户可在任何时候回复。

### 2.5 退一步策略（Step-Back Detection）

```python
def _is_step_back(ctx: Context, tool_name: str, tool_args: dict) -> bool:
    """检测是否在重复调用同一工具且参数相近"""
    if len(ctx.tool_history) < 2:
        return False

    last_calls = [t for t in ctx.tool_history if t.name == tool_name][-2:]
    if len(last_calls) < 2:
        return False

    prev_args_summary = last_calls[0].args_summary
    curr_args_summary = _summarize(tool_args)

    # 参数完全相同 或 高度相似（关键词重叠 > 80%）
    if curr_args_summary == prev_args_summary:
        return True
    if _keyword_overlap(curr_args_summary, prev_args_summary) > 0.8:
        return True
    return False

def _keyword_overlap(a: str, b: str) -> float:
    tokens_a = set(a.split())
    tokens_b = set(b.split())
    if not tokens_a:
        return 0.0
    return len(tokens_a & tokens_b) / len(tokens_a)
```

退一步时：记录 warning 到 reasoning_trace，但不阻止执行（LLM 可能确实需要再确认）。

### 2.6 LLM 响应解析

**核心：三元分支**（来自 ARCHITECTURE.md 的设计，沿用）：

```python
@dataclass
class LLMResponse:
    type: Literal["tool_call", "judgment_update", "ask_user", "conclude", "unknown"]
    raw: str  # 原始响应（用于 unknown 分支）
    tool_name: str | None = None
    tool_args: dict | None = None
    judgment: Judgment | None = None
    question: str | None = None
    output: str | None = None
    summary: str | None = None  # conclude 时的结论摘要

def _parse_response(response: str) -> LLMResponse:
    """
    解析 LLM 文本响应，提取 action type
    实现：JSON 模式（LLM 输出结构化 JSON）或 正则模式（fallback）
    """
    # 首先尝试 JSON 解析（LLM 被指示输出 JSON）
    try:
        data = json.loads(response)
        action = data.get("action", "unknown")

        if action == "tool_call":
            return LLMResponse(type="tool_call",
                tool_name=data["tool_name"], tool_args=data["tool_args"])
        elif action == "judgment_update":
            return LLMResponse(type="judgment_update",
                judgment=Judgment(**data["judgment"]))
        elif action == "ask_user":
            return LLMResponse(type="ask_user", question=data["question"])
        elif action == "conclude":
            return LLMResponse(type="conclude",
                output=data["output"], summary=data.get("summary", ""))
        else:
            return LLMResponse(type="unknown", raw=response)
    except (json.JSONDecodeError, KeyError):
        # Fallback：正则提取 action 关键字
        if "```json" in response:
            # 尝试从 code block 里提取 JSON
            match = re.search(r'```json\s*(.+?)\s*```', response, re.DOTALL)
            if match:
                return _parse_response(match.group(1))
        return LLMResponse(type="unknown", raw=response)
```

**JSON 模式优于纯正则**：LLM 被 system prompt 指示输出 JSON，结构化解析更可靠。

---

## 3. 状态机设计

### 3.1 状态定义（5 态）

```
                    ┌──────────────────────────────────────┐
                    │           用户输入原始报错              │
                    └──────────────────┬───────────────────┘
                                       │ agent_loop() 开始
                                       ▼
                              ┌────────────────┐
                              │    INTAKE      │  解析输入 → 结构化 Problem
                              └───────┬────────┘
                                      │ INTAKE 完成（problem 解析成功）
                                      ▼
                              ┌────────────────┐
           ┌─────────────────│  INVESTIGATE   │◀──────────┐
           │                  │  （主循环）    │            │
           │                  └───────┬────────┘            │
           │                          │                    │
           │  ┌─ LLM 请求补料 ───────┤                    │
           │  │                      │ ASK_USER 退出       │
           │  ▼                      ▼                    │
           │  ┌────────────────┐  ┌────────────┐           │
           │  │   ASK_USER     │  │  max_iter   │           │
           │  │  等待用户回复  │  │  用户中断   │           │
           │  └───────┬────────┘  └──────┬─────┘           │
           │          │ 用户回复后返回 ──┘                  │
           │          ▼                                      │
           │  ┌────────────────┐                            │
           └─►│  CONCLUDE      │  格式化输出                │
               └────────────────┘                            │
                                                              │
                          LLM 自决结论足够 ──────────────────┘
```

**5 态说明**：

| 态 | 含义 | 进入条件 | 退出条件 |
|---|---|---|---|
| **INTAKE** | 解析原始报错 | agent_loop 开始 | problem 结构化完成 或 解析失败 |
| **INVESTIGATE** | 主推理循环 | INTAKE 完成 | conclude / max_iter / 用户中断 / ask_user |
| **ASK_USER** | 主动请求用户补料 | INVESTIGATE 内 LLM 请求信息 | 用户回复后返回 INVESTIGATE |
| **CONCLUDE** | 格式化结论 | 所有退出路径汇聚点 | 输出完成 |
| **EXIT** | 异常退出 | INTAKE 解析失败 / 严重错误 | 直接返回 error message |

### 3.2 每态行为定义

```python
# ── INTAKE ──
class IntakeHandler:
    @staticmethod
    def enter(ctx: Context, raw_input: str):
        ctx.state = State.INTAKE
        ctx.problem = None
        ctx.append_reasoning(f"[INTAKE] 开始解析原始输入，长度：{len(raw_input)}")

    @staticmethod
    def tick(ctx: Context, raw_input: str, llm, tools) -> bool:
        """
        返回 True = 完成（进入 INVESTIGATE）
        返回 False = 失败（进入 EXIT）
        """
        # 用 LLM 解析原始报错，提取结构化 Problem
        parse_prompt = _build_intake_prompt(raw_input)
        result = llm.complete(parse_prompt, tools=[])  # INTAKE 不给工具

        try:
            parsed = json.loads(result)
            ctx.problem = Problem(
                raw_input=raw_input,
                error_type=parsed.get("error_type", "UNKNOWN"),
                stack_frames=_parse_stack(parsed.get("stack", "")),
                log_snippets=parsed.get("log_snippets", []),
                timestamp=_parse_timestamp(parsed.get("timestamp")),
            )
            ctx.append_reasoning(
                f"[INTAKE] 完成：{ctx.problem.error_type}，"
                f"堆栈 {len(ctx.problem.stack_frames)} 帧"
            )
            return True
        except Exception as e:
            ctx.append_reasoning(f"[INTAKE] 解析失败：{e}，使用原始输入")
            ctx.problem = Problem(raw_input=raw_input, error_type="PARSE_FAILED")
            return True  # 仍继续，只是 error_type = PARSE_FAILED

    @staticmethod
    def exit(ctx: Context):
        ctx.append_reasoning("[INTAKE] 退出")


# ── INVESTIGATE ──
class InvestigateHandler:
    # 核心行为在 agent_loop() 的 while True 循环中
    # 此处定义进入/退出时做什么
    @staticmethod
    def enter(ctx: Context):
        ctx.state = State.INVESTIGATE
        ctx.append_reasoning("[INVESTIGATE] 进入主推理循环")

    @staticmethod
    def exit(ctx: Context, reason: str):
        ctx.append_reasoning(f"[INVESTIGATE] 退出，原因：{reason}")


# ── ASK_USER ──
class AskUserHandler:
    @staticmethod
    def enter(ctx: Context, question: str):
        ctx.state = State.ASK_USER
        ctx.pending_question = question
        ctx.append_reasoning(f"[ASK_USER] 进入，等待用户回复：{question}")

    @staticmethod
    def exit(ctx: Context):
        ctx.pending_question = None
        ctx.append_reasoning("[ASK_USER] 退出，用户已回复")


# ── CONCLUDE ──
class ConcludeHandler:
    @staticmethod
    def enter(ctx: Context):
        ctx.state = State.CONCLUDE
        ctx.append_reasoning("[CONCLUDE] 进入")

    @staticmethod
    def tick(ctx: Context) -> str:
        """生成最终输出"""
        output = _format_output(ctx)
        ctx.final_output = output
        ctx.append_reasoning("[CONCLUDE] 输出已生成")
        return output

    @staticmethod
    def exit(ctx: Context):
        ctx.append_reasoning("[CONCLUDE] 退出")
```

### 3.3 状态转移条件（完整矩阵）

| 当前态 | 事件 | 下一态 | 触发条件 |
|---|---|---|---|
| INTAKE | intake 完成 | INVESTIGATE | problem 解析成功（成功或失败都算完成） |
| INTAKE | 严重错误 | EXIT | LLM 完全无法解析且 raw_input 为空 |
| INVESTIGATE | LLM conclude | CONCLUDE | 响应 type == "conclude" |
| INVESTIGATE | max_iter 到达 | CONCLUDE | iteration > max_iterations |
| INVESTIGATE | 用户中断 | CONCLUDE | user_interrupt == True |
| INVESTIGATE | LLM ask_user | ASK_USER | 响应 type == "ask_user" |
| INVESTIGATE | LLM tool_call | INVESTIGATE（自身） | 响应 type == "tool_call"，iteration 不 +1 |
| INVESTIGATE | LLM judgment_update | INVESTIGATE（自身） | 响应 type == "judgment_update" |
| ASK_USER | 用户回复 | INVESTIGATE | 用户消息到达 |
| ANY | fatal_error | EXIT | 捕获未处理异常 |

### 3.4 状态转移的可观测性（Log 格式）

每条 reasoning_trace 条目格式：

```
[timestamp] [STATE] [iteration] message
```

示例：

```
[13:02:01] [INTAKE] [0] 开始解析原始输入，长度：2048
[13:02:02] [INTAKE] [0] 完成：NullPointerException，堆栈 12 帧
[13:02:02] [INVESTIGATE] [1] 开始第 1 轮
[13:02:03] [INVESTIGATE] [1] 判断更新：UNKNOWN→A，理由：NPE 发生在本产品代码
[13:02:05] [INVESTIGATE] [2] 开始第 2 轮
[13:02:06] [INVESTIGATE] [2] 向用户提问：报错是在哪个环境（生产/测试）？
[13:02:06] [ASK_USER] [2] 进入，等待用户回复
[13:02:30] [ASK_USER] [2] 退出，用户已回复
[13:02:30] [INVESTIGATE] [2] 开始第 2 轮（续）
[13:02:35] [INVESTIGATE] [2] LLM 自决结束：证据已充分
[13:02:35] [CONCLUDE] [2] 输出已生成
```

### 3.5 状态转换测试覆盖策略

```python
# tests/test_state_machine.py

def test_intake_to_investigate():
    ctx = Context()
    IntakeHandler.enter(ctx, "NullPointerException at UserService.java:42")
    assert ctx.state == State.INTAKE

    success = IntakeHandler.tick(ctx, "NullPointerException ...", llm_fake, tools_fake)
    assert success is True
    assert ctx.problem.error_type == "NullPointerException"
    # 状态转移由 agent_loop 驱动，此处只测试 handler.tick 的返回值

def test_investigate_to_conclude_by_llm():
    ctx = make_investigate_context(llm_fake_conclude)
    ctx = agent_loop("NPE error", ctx, llm_fake_conclude, tools_fake)
    assert ctx.state == State.CONCLUDE

def test_investigate_to_ask_user_and_back():
    ctx = make_investigate_context(llm_fake_ask_user)
    # 第一轮：LLM ask_user
    ctx = agent_loop_step(ctx, llm_fake_ask_user, tools_fake)
    assert ctx.state == State.ASK_USER
    assert ctx.pending_question is not None
    # 用户回复
    ctx = agent_loop_continue(ctx, user_reply="生产环境 192.168.1.100")
    assert ctx.state == State.INVESTIGATE

def test_max_iteration_guard():
    ctx = make_investigate_context(llm_fake_never_conclude)
    ctx.max_iterations = 3
    for i in range(3):
        ctx = agent_loop_step(ctx, llm_fake_never_conclude, tools_fake)
    assert ctx.iteration == 3
    assert ctx.state == State.CONCLUDE  # 强制结束
```

---

## 4. Context 设计

### 4.1 数据结构

```python
# ── 核心数据结构 ──

@dataclass
class Problem:
    """问题陈述（INTAKE 输出）"""
    raw_input: str               # 原始输入（最多 2000 字截取）
    error_type: str              # NullPointerException / OOM / ...
    stack_frames: list[StackFrame]  # 堆栈帧
    log_snippets: list[str]      # 关联日志片段
    timestamp: datetime | None  # 报错时间

@dataclass
class StackFrame:
    class_name: str
    method_name: str
    file_name: str
    line_number: int

@dataclass
class Judgment:
    """当前判断（单例，随推理更新）"""
    category: Literal["A", "B", "C", "UNKNOWN"]
    confidence: float            # 0.0~1.0
    one_line_reason: str         # 一句话理由
    reasoning: str               # 详细推理（当前轮的，不累积）

@dataclass
class Evidence:
    """证据（只增不减，压缩在 prompt 层做）"""
    id: str                      # uuid
    source: Literal["code", "log", "stack", "tool_output", "error", "user"]
    location: str                # file:line / log line range / tool name
    content: str                 # 证据内容（截取后，原始存 tool_call.result）
    raw_content: str             # 原始完整内容（用于压缩评估，不进入 prompt）
    relevance: float             # 0.0~1.0（LLM 自评）
    discovered_at_iteration: int # 第几轮发现
    importance: Literal["critical", "supporting", "background"]
    # importance 由 LLM 自评后降级：
    # - critical: 必须引用的根因证据
    # - supporting: 辅助推理的证据
    # - background: 上下文信息

@dataclass
class ToolCall:
    """工具调用记录（用于退一步检测）"""
    name: str
    args_summary: str             # 参数摘要（不存完整 args，进 prompt）
    output_summary: str           # 输出摘要（不存完整输出，进 prompt）
    output_raw: Any               # 完整输出（不进 prompt，用于压缩评估）
    iteration: int
    error: str | None            # 错误信息（如果有）

@dataclass
class UserReply:
    """用户补充的回复（ask_user 场景）"""
    question: str                 # 当时问的问题
    reply: str                   # 用户的回复
    timestamp: datetime

@dataclass
class Context:
    """全局 context"""
    # 问题描述
    problem: Problem | None = None

    # 当前判断
    current_judgment: Judgment = field(
        default_factory=lambda: Judgment(
            category="UNKNOWN", confidence=0.0,
            one_line_reason="尚未开始调查", reasoning=""
        )
    )

    # 证据池（只增不减）
    evidence: list[Evidence] = field(default_factory=list)

    # 工具历史（用于退一步检测 + 推理轨迹）
    tool_history: list[ToolCall] = field(default_factory=list)

    # 推理轨迹（固定长度滑动窗口，存最近 N 步）
    reasoning_trace: list[str] = field(default_factory=list)
    MAX_REASONING_TRACE = 10  # 超过则丢弃最旧的

    # 用户回复历史（ask_user 场景）
    user_replies: list[UserReply] = field(default_factory=list)

    # loop 控制
    iteration: int = 0
    max_iterations: int = 8
    user_interrupt: bool = False

    # 状态
    state: State = State.INTAKE
    pending_question: str | None = None

    # 输出
    final_output: str | None = None

    # ── 辅助字段 ──
    last_tool_result_summary: str = ""  # 用于退一步检测

    # ── 方法 ──
    def add_evidence(self, evidence: Evidence):
        self.evidence.append(evidence)
        self._maybe_update_reasoning_trace(f"[证据追加] {evidence.source}@{evidence.location}")

    def add_user_reply(self, question: str, reply: str):
        self.user_replies.append(UserReply(question=question, reply=reply))
        self._maybe_update_reasoning_trace(
            f"[用户回复] Q: {question[:50]} → {reply[:50]}"
        )

    def append_reasoning(self, msg: str):
        self.reasoning_trace.append(msg)
        if len(self.reasoning_trace) > self.MAX_REASONING_TRACE:
            self.reasoning_trace.pop(0)

    def _maybe_update_reasoning_trace(self, msg: str):
        """add_evidence/add_user_reply 时顺便记录推理轨迹"""
        self.append_reasoning(msg)
```

### 4.2 内容来源

| 内容 | 来源 | 更新时机 |
|---|---|---|
| problem | 用户原始输入 + INTAKE LLM 解析 | INTAKE 态一次性完成 |
| current_judgment | LLM 推理 | INVESTIGATE 循环内，LLM judgment_update 分支 |
| evidence | 工具执行结果 | INVESTIGATE 循环内，工具返回后 |
| tool_history | 工具执行记录 | 每次工具调用后 |
| reasoning_trace | 所有重要事件 | 任何状态变更时 |
| user_replies | 用户对 ask_user 的回复 | ASK_USER 退出后 |
| final_output | CONCLUDE 态生成 | CONCLUDE 态完成时 |

### 4.3 更新机制

**核心原则：只追加，不修改，不删除**

```
证据更新（工具返回后）：
  tool_call.result → _result_to_evidence() → ctx.evidence.append()

判断更新（LLM judgment_update）：
  LLM 返回的新 judgment → ctx.current_judgment = 新值（覆盖）

注意：judgment 是覆盖而非追加，因为设计上只有"当前判断"
     不保留历史判断（要保留可从 reasoning_trace 重建）
```

### 4.4 压缩机制

**与 OpenCode 的差异**：OpenCode 用 hidden compaction agent 自动压缩历史；microtrace 采用"prompt 层截取 + 工具输出存 raw_content"策略。

**为什么不自动压缩**：
1. VNFM 场景 evidence 是事实基础，压缩可能丢失关键证据
2. context 只存储不压缩，压缩在 prompt 组装时做（更可控）
3. OpenCode 的压缩是因为代码编辑 session 积累大量历史对话；VNFM 场景是调查性对话，evidence 不会那么快膨胀到需要压缩

**Prompt 层截取策略**（`_assemble_prompt` 内）：

```python
def _assemble_prompt(ctx: Context, tools: ToolRegistry) -> str:
    """
    从 context 组装 prompt
    截取规则：不在 context 层压缩，在此处按预算截取
    """
    sections = []

    # ── Section 1: System Prompt（全量） ──
    sections.append(_load_system_prompt())

    # ── Section 2: Problem（固定） ──
    sections.append(_format_problem(ctx.problem))

    # ── Section 3: Judgment（固定，简洁） ──
    sections.append(_format_judgment(ctx.current_judgment))

    # ── Section 4: Evidence Pool（按 importance + relevance 截取） ──
    # 预算：整个 prompt 剩余空间的 40%
    evidence_text = _format_evidence_pool(
        ctx.evidence,
        max_items=5,          # 最多 5 条
        max_content_len=500,  # 每条最多 500 字
    )
    sections.append(evidence_text)

    # ── Section 5: Reasoning Trace（最近 3 步） ──
    sections.append(_format_reasoning_trace(ctx.reasoning_trace, max_steps=3))

    # ── Section 6: User Replies（如果有 ask_user 历史） ──
    if ctx.user_replies:
        sections.append(_format_user_replies(ctx.user_replies))

    # ── Section
    # ── Section 7: Available Tools（固定） ──
    sections.append(_format_tools(tools))

    # ── Section 8: Instruction ──
    sections.append(_build_instruction(ctx))

    return "\n\n".join(sections)


def _format_evidence_pool(evidence: list[Evidence], max_items: int, max_content_len: int) -> str:
    """按 importance + relevance 排序，截取证据池"""
    # 按 importance 降序，然后 relevance 降序
    sorted_evidence = sorted(
        evidence,
        key=lambda e: (0 if e.importance == "critical" else 1 if e.importance == "supporting" else 2, -e.relevance)
    )
    selected = sorted_evidence[:max_items]

    lines = ["## 证据池"]
    for i, ev in enumerate(selected, 1):
        content = ev.content[:max_content_len] + ("..." if len(ev.content) > max_content_len else "")
        lines.append(f"{i}. [{ev.source}] {ev.location}")
        lines.append(f"   relevance={ev.relevance:.2f} | importance={ev.importance}")
        lines.append(f"   {content}")
    return "\n".join(lines)


def _format_reasoning_trace(trace: list[str], max_steps: int) -> str:
    """推理轨迹：最近 N 步"""
    recent = trace[-max_steps:] if len(trace) > max_steps else trace
    if not recent:
        return "## 推理轨迹\n（暂无）"
    lines = ["## 推理轨迹"]
    for step in recent:
        lines.append(f"- {step}")
    return "\n".join(lines)


def _format_user_replies(replies: list[UserReply]) -> str:
    lines = ["## 用户补充信息"]
    for r in replies[-3:]:  # 最多 3 条
        lines.append(f"Q: {r.question}")
        lines.append(f"A: {r.reply}")
        lines.append("")
    return "\n".join(lines)
```

**OpenCode 启示**：OpenCode 的 auto-compaction 在 context 95% 满时触发压缩 agent；microtrace 改为"prompt 层按预算截取"（Section 4.4），因为 VNFM 场景的 evidence 是事实基础，不应被压缩。

**重要性评分**（`importance` 字段）：
- LLM 每次返回 evidence 时自评 `importance`
- `critical`：必须引用的根因证据（如根因代码行）
- `supporting`：辅助推理（如日志上下文）
- `background`：背景信息（如调用链路径）

### 4.5 Prompt 组装策略

```python
def _build_instruction(ctx: Context) -> str:
    """指令部分：告知 LLM 当前状态和可选动作"""
    lines = [
        "## 你的任务",
        "根据上述信息，决定下一步动作：",
        "",
        "1. **调用工具**（当证据不足时）：",
        "   回复格式：```json",
        '   {"action": "tool_call", "tool_name": "read_file", "tool_args": {"filePath": "..."}}',
        "   ```",
        "",
        "2. **更新判断**（当有足够证据支撑新判断时）：",
        "   回复格式：```json",
        '   {"action": "judgment_update", "judgment": {"category": "A", "confidence": 0.85, "one_line_reason": "..."}}',
        "   ```",
        "",
        "3. **请求用户补料**（当关键信息缺失时）：",
        "   回复格式：```json",
        '   {"action": "ask_user", "question": "报错是在生产环境还是测试环境？"}',
        "   ```",
        "",
        "4. **输出结论**（当证据已充分时）：",
        "   回复格式：```json",
        '   {"action": "conclude", "output": "..."}',
        '   "summary": "一句话总结"',
        "   ```",
        "",
        f"**已用 {ctx.iteration}/{ctx.max_iterations} 轮**",
    ]
    return "\n".join(lines)
```

**截取规则汇总**：

| 内容 | 截取规则 | 理由 |
|---|---|---|
| problem.raw_input | > 2000 字截取 | 原始输入太长时尾部不关键 |
| evidence[].content | > 500 字截取 | 单条证据太长时取关键片段 |
| evidence pool | 最多 5 条，按 importance+relevance | prompt 预算有限 |
| reasoning_trace | 最近 3 步 | 历史太长则从 context 重建 |
| tool_history[].output | 不进 prompt，只存 raw | 在 evidence 层处理 |

---

## 5. 与 v1（ARCHITECTURE.md）的差异

### 5.1 状态机：3 态 → 5 态

| v1 | v2（本次设计） | 理由 |
|---|---|---|
| INTAKE / INVESTIGATE / CONCLUDE（3 态） | INTAKE / INVESTIGATE / ASK_USER / CONCLUDE / EXIT（5 态） | v1 漏了"主动询问用户"场景；VNFM 场景经常需要用户补料（报错时间、环境、版本等） |
| 没有 EXIT 态 | 增加 EXIT 态 | INTAKE 解析严重失败时有明确出口，不让 agent 带病进入 INVESTIGATE |
| 状态转换没有显式 handler | 每态有 enter/tick/exit 方法 | 可测试、可观测；OpenClaw 的启发是"态进入/退出时做什么要显式定义" |

### 5.2 Loop 退出条件

| v1 | v2 | 理由 |
|---|---|---|
| LLM 自决 / max_iterations | LLM 自决 / max_iterations / 用户中断 / ask_user（4 条） | ask_user 是新退出路径（进入 ASK_USER 态），不是终止而是暂停 |

### 5.3 证据池设计

| v1 | v2 | 理由 |
|---|---|---|
| 4 字段（id/source/location/content） | 6 字段（+ relevance + importance） | relevance 用于排序；importance 用于截取优先级（critical 优先进入 prompt） |
| 无 raw_content | 增加 raw_content | 压缩评估需要原始内容，但原始内容不进 prompt |
| 无 user_replies | 增加 user_replies | ask_user 场景需要记录"我问了什么 + 用户答了什么" |

### 5.4 错误处理

| v1 | v2 | 理由 |
|---|---|---|
| 无显式错误处理设计 | 完整错误恢复矩阵（工具失败/LLM 解析失败/网络中断/超时） | VNFM 生产环境需要可靠运行，不能因为单次失败就 crash |
| 无退一步策略 | 退一步检测 + warning 记录 | 工具重复调无新信息是高频问题，需要显式检测 |

### 5.5 Context 压缩

| v1 | v2 | 理由 |
|---|---|---|
| "不压缩，截取 + 排序"（无具体设计） | 完整的 prompt 层截取策略（预算分配、各字段截取规则） | v1 的截取规则太模糊，实际实现会各行其是 |
| 无 raw_content | raw_content 分离 | "存原始 vs 进 prompt"要明确分开 |

### 5.6 Prompt 组装

| v1 | v2 | 理由 |
|---|---|---|
| "截取策略"在 prompt 组装层做（无具体设计） | 完整 8-section prompt 模板（system / problem / judgment / evidence / reasoning / user_replies / tools / instruction） | v1 只有"截取策略"4 点，太粗 |
| LLM 响应用正则解析（"三选一"） | JSON 解析为主 + 正则 fallback | OpenCode 的工具调用用 AI SDK 标准化；microtrace 用 JSON schema 让 LLM 输出结构化响应更可靠 |

---

## 6. 落地技术选型

### 6.1 实现语言

**Python 3.11+**

理由：VNFM 工程师主要语言是 Java，Python 用于 agent 胶水层足够简单；MiniMax API / HTTP 调用已有成熟 SDK。

### 6.2 LLM 调用

**MiniMax API（默认）+ 抽象接口**

```python
class LLMClient(Protocol):
    def complete(self, prompt: str, tools: list[dict]) -> str:
        """返回 LLM 原始文本响应"""
        ...

class MiniMaxClient:
    def __init__(self, api_key: str, model: str = "MiniMax-M2"):
        self.api_key = api_key
        self.model = model

    def complete(self, prompt: str, tools: list[dict]) -> str:
        # 调用 MiniMax Chat API
        # tools 参数序列化为 function_call 格式
        ...
```

**工具声明格式**：参考 OpenCode 的 AI SDK tool 定义，走 JSON Schema：

```python
TOOL_DEFINITIONS = [
    {
        "name": "read_file",
        "description": "读取文件内容",
        "parameters": {
            "type": "object",
            "properties": {
                "filePath": {"type": "string", "description": "文件路径"},
                "offset": {"type": "integer", "description": "行号偏移（0-base）"},
                "limit": {"type": "integer", "description": "最多读几行"},
            },
            "required": ["filePath"]
        }
    },
    # ... search_logs / find_class / parse_stack_trace
]
```

### 6.3 工具注册中心

```python
class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Callable] = {}

    def register(self, name: str, func: Callable, schema: dict):
        self._tools[name] = {"func": func, "schema": schema}

    def invoke(self, name: str, args: dict) -> Any:
        if name not in self._tools:
            raise ToolNotFoundError(f"Tool {name} not found")
        return self._tools[name]["func"](**args)

    def schemas(self) -> list[dict]:
        return [t["schema"] for t in self._tools.values()]
```

参考 OpenCode 的 `Tool.define` 范式，但简化：注册 + schema + 执行三合一。

### 6.4 HTTP 服务

**FastAPI**（来自 VISION.md Phase 0 形态）

```python
from fastapi import FastAPI

app = FastAPI()

@app.post("/chat")
async def chat(message: str):
    ctx = load_or_create_session(session_id)
    result = agent_loop(message, ctx, llm, tools)
    return {"output": result, "state": ctx.state.value}

@app.get("/state")
async def state(session_id: str):
    ctx = load_session(session_id)
    return {
        "state": ctx.state.value,
        "judgment": ctx.current_judgment,
        "evidence_count": len(ctx.evidence),
        "iteration": ctx.iteration,
    }
```

### 6.5 REPL

**prompt_toolkit**（来自 VISION.md Phase 0 形态）

```python
from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory

session = PromptSession(history=FileHistory("~/.microtrace/history"))
while True:
    user_input = await session.prompt_async("microtrace> ")
    if user_input in ("exit", "quit"):
        break
    result = await http_client.chat(user_input)
    print(result["output"])
```

### 6.6 测试

**pytest + pytest-asyncio**

```python
# tests/test_loop.py
import pytest
from microtrace.agent import agent_loop, Context

@pytest.fixture
def ctx():
    return Context(max_iterations=3)

@pytest.fixture
def llm_fake():
    class FakeLLM:
        def __init__(self, responses: list[str]):
            self.responses = responses
            self.idx = 0
        def complete(self, prompt, tools):
            r = self.responses[self.idx]
            self.idx += 1
            return r
    return FakeLLM

def test_investigate_to_conclude(ctx, llm_fake):
    ctx.state = State.INVESTIGATE
    result = agent_loop("NPE at UserService.java:42", ctx, llm_fake, tools_fake)
    assert ctx.state == State.CONCLUDE
    assert ctx.final_output is not None
```

### 6.7 项目结构（推荐）

```
microtrace/
├── agent/
│   ├── __init__.py
│   ├── loop.py          # agent_loop() 主循环
│   ├── state.py         # 状态机（State enum + handlers）
│   ├── context.py       # Context 数据结构
│   ├── prompt.py        # _assemble_prompt() + 各 _format_* 函数
│   └── parse.py         # _parse_response() LLM 响应解析
├── tools/
│   ├── __init__.py
│   ├── registry.py      # ToolRegistry
│   ├── read_file.py     # read_file 实现
│   ├── search_logs.py   # search_logs 实现
│   ├── find_class.py    # find_class 实现
│   └── parse_stack_trace.py  # parse_stack_trace 实现
├── llm/
│   ├── __init__.py
│   ├── base.py          # LLMClient 抽象接口
│   └── minimax.py       # MiniMax API 实现
├── api/
│   ├── __init__.py
│   └── main.py          # FastAPI app
├── repl/
│   └── __init__.py      # prompt_toolkit REPL
├── prompts/
│   └── agent.md         # Master prompt（AGENTS.md 模式，全量注入）
└── tests/
    ├── test_loop.py
    ├── test_state.py
    └── test_context.py
```

---

## 7. 待老板拍板的问题

### Q1. ASK_USER 态：阻塞还是非阻塞？

**当前设计**：非阻塞（yield 后继续 loop，用户可在任何时候回复）

**争议点**：如果用户不回复，agent 应该：
- A）继续 investigate（基于"缺失信息仍尝试推理"）
- B）等待（停在 ASK_USER，直到用户回复）

**当前倾向**：A，因为 VNFM 工程师可能同时看多个问题，不应被一个 ask_user 阻塞整个 agent

### Q2. ASK_USER 的问题应该由 LLM 决定还是硬编码触发？

**争议点**：当前设计是 LLM 自行决定"证据不足要问用户"，但这可能导致：
- LLM 频繁 ask_user（每轮都问一点）
- LLM ask 一些 agent 可以自己推断的信息（如"报错时间"，可从日志提取）

**选项**：
- A）完全由 LLM 自决（当前设计）
- B）prompt 里加"只有以下情况才 ask_user"的硬编码白名单
- C）混合：白名单优先，白名单之外才让 LLM 自决

**当前倾向**：A，因为 prompt 的 playbooks 会逐步积累"何时该问"的经验

### Q3. judgment_update 应该是覆盖还是版本化？

**当前设计**：覆盖（只有 current_judgment）

**争议点**：如果 judgment 多次更新，review 时无法看到演变过程

**选项**：
- A）覆盖（当前设计）+ reasoning_trace 里记录"判断从 X 变 Y"
- B）版本化：ctx.judgment_history = list[Judgment]

**当前倾向**：A，因为 reasoning_trace 已经有记录，不需要额外的 judgment_history

### Q4. max_iterations = 8 是否合适？

**当前设计**：8 轮

**争议点**：VNFM 问题复杂度差异大（简单 NPE 可能 3 轮，复杂调用链可能需要 15 轮）

**选项**：
- A）固定 8（简单）
- B）可配置（默认 8，用户可改）
- C）动态：LLM 可请求延长（特殊 action "request_more_iterations"）

**当前倾向**：B，可配置 + 在 prompts/agent.md 里说明"一般不超过 X 轮"

### Q5. evidence 的 relevance 谁来评？

**当前设计**：LLM 自评（每次 tool_call 后 LLM 返回 relevance）

**争议点**：LLM 自评可能偏高（因为是自己查的）

**选项**：
- A）LLM 自评（当前设计）
- B）工具层打分（如 search_logs 按"关键词匹配密度"给 relevance）
- C）混合：工具给初分数 + LLM 调整

**当前倾向**：A，因为 VNFM 场景 relevance 最终是"对判断 A/B/C 有多少帮助"，只有 LLM 能评估

### Q6. 是否需要 session 持久化？

**当前设计**：Phase 0 不做（每次 REPL 启动是全新 session）

**争议点**：VNFM 工程师可能：
- 上午调查一个问题 → 下午继续
- 中途关闭 REPL → 不想重新开始

**选项**：
- A）Phase 0 不做，Phase 1 再考虑
- B）Phase 0 做 session 保存（JSON 文件），但 agent_loop 仍是内存状态
- C）Phase 0 做完整持久化（SQLite/JSON 文件）+ agent_loop 可从磁盘恢复

**当前倾向**：B（轻量 session 保存），因为 VISION.md 明确说"Phase 0 不做持久化"，但 session 保存比"完整 agent 持久化"轻得多

### Q7. tool_call 的并行执行：是 LLM 声明即可并行，还是需要显式协调？

**当前设计**：LLM 可以在一次响应里声明多个工具调用（参考 OpenCode 的 parallelism）

**争议点**：如果 LLM 同时调用 read_file 和 search_logs，两个都返回后才进下一轮 LLM call。但实现上：
- A）收到多个 tool_call 时，先执行所有，汇总结果后进下一轮
- B）只接受一个 tool_call（简化，但可能降低效率）
- C）LLM 显式声明"parallel: true"时才并行

**当前倾向**：A（收到多个则并行执行），因为 OpenCode 明确说"parallelism when applicable"，这对 VNFM 场景（搜日志 + 读代码常可并行）很有价值

---

## 附录：OpenCode 技术栈一览

| 组件 | 技术选型 | 对 microtrace 的参考 |
|---|---|---|
| Runtime | Bun (JavaScript) | — |
| HTTP Server | Hono | FastAPI（Python） |
| TUI | Go + Bubble Tea | prompt_toolkit（Python） |
| LLM SDK | AI SDK（provider-agnostic） | 自行封装 MiniMax API |
| 工具定义 | Tool.define (description + ZodSchema + execute) | 自定义 ToolRegistry |
| 配置格式 | JSON / Markdown agent 文件 | JSON + prompts/agent.md |
| 状态管理 | 两层状态机（member + execution） | 单层 5 态 |
| Context | Auto-compaction at 95% | Prompt 层截取 |
| Multi-agent | inbox JSONL + session injection | 单 agent（Phase 0） |

---

## 附录：OpenClaw 关键机制一览

| 组件 | 设计 | 对 microtrace 的参考 |
|---|---|---|
| Skills | SKILL.md（name + description frontmatter） | prompts/agent.md 走 AGENTS.md 模式（全量） |
| Memory | daily notes + long-term MEMORY.md | 案例库分层可借鉴 |
| Session | main vs isolated | 单 session 足够 |
| Subagent | sessions_spawn 独立 context | Phase 0 不做 subagent |
| Prompt | system + context + tools | microtrace 同理 |

---

