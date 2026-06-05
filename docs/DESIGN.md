# DESIGN.md - L2 Agent Core Mechanisms

> 📌 v2 修订（2026-06-05 15:00）—— 基于 OpenCode 本地源码 + OpenClaw 平台
> 主要修订：恢复 Doom Loop + Compaction + 加事件溯源层 + 流式 LLM + Tool 4 态子状态机 + Retry 策略

---

## 0. 设计目标

针对 **VNFM 问题定位** 场景，为 microtrace 设计 L2 agent 核心机制（loop / state / context）。这是 microtrace 的"灵魂"——决定了它"是专门为问题定位而生的 Agent"，不是通用 Agent 套壳。

设计原则：
1. **严谨推理优先** —— 每步可追溯、可反驳、不臆造
2. **结构化状态** —— 显式状态机 + 事件溯源层（双重可观测）
3. **流式响应** —— LLM 响应是 event stream，不是单次请求
4. **资源保护** —— Doom Loop 检测 + Compaction + Retry
5. **垂直场景** —— 不预留"以后通用"的抽象，YAGNI

---

## 1. 借鉴来源

### 1.1 OpenCode — 核心机制参考（已读本地源码）

来源：`~/AIAgent/opencode/`（`anomalyco/opencode` fork，分支 `dev`）

**2026-06-05 修订说明**：之前 subagent 调研时只看了 GitHub README，**未找到完整源码**。后来发现老板本地有 `~/AIAgent/opencode/` 完整源码（22+ 个 packages），重读后设计大量修正。

**已确认事实**（来自本地源码阅读）：

#### 1.1.1 Loop 机制

**核心源码**：`packages/opencode/src/session/processor.ts`（828 行）

- **流式 LLM 调用**（非 request/response）：用 `Effect.Stream` 包装 LLM stream，事件类型 `start` / `reasoning-start` / `reasoning-delta` / `text-start` / `text-delta` / `text-end` / `tool-input-start` / `tool-call` / `tool-result` / `step-start` / `step-finish` / `finish` / `error`
- **Result 三元组**：`type Result = "compact" | "stop" | "continue"`（`processor.ts:26`）
- **Step 边界**：每次 LLM call 是一个 Step（`Step.Started` / `Step.Ended` / `Step.Failed`）
- **Doom Loop 检测**（`processor.ts:30, 362-388`）：
  ```typescript
  const DOOM_LOOP_THRESHOLD = 3
  // 看最近 3 次 tool call
  // 3 次完全相同 (tool name + JSON-stringified input)
  // → 触发 permission.ask({ permission: "doom_loop" })
  ```
- **自动 Compaction 触发**（`processor.ts:533`）：`isOverflow()` 检查 tokens >= usable capacity → `ctx.needsCompaction = true` → process 返回 `"compact"`
- **Retry 策略**（`processor.ts:768-803`）：`SessionRetry.policy({...})` 自动重试 transient errors
- **Context 溢出保护**（`src/session/overflow.ts`）：
  ```typescript
  const COMPACTION_BUFFER = 20_000  // 保留 20K 给 summary
  const count = tokens.input + tokens.output + tokens.cache.read + tokens.cache.write
  return count >= usable(input)
  ```
- **Snapshot 跟踪**（`processor.ts:140, 520`）：每个 step 开头 track 文件系统快照，用于 revert
- **Status 跟踪**（`SessionStatus`）：busy / idle / retry 状态，UI 反馈

**对 microtrace 的影响**：
- ✅ Loop 改用 stream（不单次 LLM call）
- ✅ 保留 Doom Loop（精确 3 次匹配 + ask user）
- ✅ 保留 Compaction（auto on overflow + 20K buffer）
- ✅ 保留 Retry 策略（transient vs permanent）
- ❌ 不做 Snapshot（我们只读不写，无需回滚）
- ❌ 不做 Status 跟踪（Phase 0 CLI 不需要）

#### 1.1.2 状态管理

**核心源码**：`packages/opencode/src/v2/session-event.ts`（407 行）+ `session-message-updater.ts`（417 行）

- **事件溯源设计**（不是显式状态机）：所有 state changes 都是 events
- **25+ 事件类型**：
  - Agent lifecycle：`AgentSwitched` / `ModelSwitched`
  - User input：`Prompted` / `Shell` / `Synthetic`
  - Step boundary：`Step.Started` / `Step.Ended` / `Step.Failed`
  - LLM stream：`Text.Started/Delta/Ended` / `Reasoning.Started/Delta/Ended`
  - Tool：`Tool.Input.Started/Delta/Ended` / `Tool.Called` / `Tool.Progress` / `Tool.Success` / `Tool.Failed`
  - Compaction：`Compaction.Started` / `Compaction.Delta` / `Compaction.Ended`
  - Error retry：`Retried`
- **事件 apply 模式**（`session-message-updater.ts:78-99`）：
  ```typescript
  export function update<Result>(adapter: Adapter<Result>, event: SessionEvent.Event): Result
  ```
  事件 → adapter state mutation → 投影
- **Tool 4 态子状态机**（`session-message.ts:55-83`）：
  ```typescript
  ToolStatePending   → { status: "pending" }
  ToolStateRunning   → { status: "running" }
  ToolStateCompleted → { status: "completed" }
  ToolStateError     → { status: "error" }
  ```
- **adapter memory state**（`session-message-updater.ts:25-33`）：
  ```typescript
  export type MemoryState = { messages: SessionMessage.Message[] }
  ```

**对 microtrace 的影响**：
- ✅ 底层用事件溯源（state 来自 event 投影）
- ✅ Tool 4 态子状态机
- ✅ 高层仍有显式 5 态（INTAKE / INVESTIGATE / ASK_USER / CONCLUDE / EXIT）—— REPL UI 友好
- ✅ State = 事件投影（"什么状态"= "看到了什么事件"）

#### 1.1.3 Context 机制

**核心源码**：`packages/opencode/src/v2/session-message.ts`（173 行）+ `session-prompt.ts`（49 行）

- **Context 数据结构**：
  ```typescript
  Prompt: { text, files[], agents[], references[] }
  Assistant: { content: [Text | Reasoning | Tool], tokens, cost, finish }
  Tool: { input, output, attachments, structured, error, time }
  ```
- **Prompt 组装**：未公开在 v2，**老版本有 `system.ts`**（多个 system prompt 拼接）
- **Compaction 结果作为消息插入**：`Compaction` 是一种特殊消息类型
- **AGENTS.md / Rules 始终全量注入**（不压缩）

**对 microtrace 的影响**：
- ✅ Context 用 dataclass，结构化（同我们设计）
- ✅ Agent.md（= AGENTS.md）不压缩
- ✅ Compaction 结果作为新消息插入 context

#### 1.1.4 工具注册

**核心源码**：`packages/opencode/src/tool/tool.ts` + `tool/*.ts`

- `Tool.define("name", { description, parameters: ZodSchema, execute })` 模式
- 工具 schema 用 Zod 描述（**Python 可用 Pydantic 对应**）
- 内置工具：Bash / Edit / Read / Glob / Grep / List / Write / Webfetch / Websearch / Codesearch / Todo

**对 microtrace 的影响**：
- ✅ 4 个工具用 `Tool.define(name, { description, parameters: BaseModel, execute })` 模式
- ✅ Python 用 Pydantic 描述 schema

#### 1.1.5 Multi-Agent 协作

**OpenCode 的多 agent 模式**（`src/agent/agent.ts`）：
- 每个 agent 有自己的 system prompt + 工具白名单 + 权限
- `subagent: (input) => Effect` 启动 sub-session
- Sub-session 的 parent_id 指向主 session

**对 microtrace 的影响**：
- ❌ Phase 0 不做 multi-agent（单 agent 够用）
- ⚠️ 预留 `subagent` 概念（不实现），便于未来扩展

### 1.2 OpenClaw — 平台架构参考

来源：`~/.openclaw/workspace/`（本仓库环境）

**已确认事实**：

#### 1.2.1 Skills 架构
- SKILL.md 范式：YAML frontmatter（name + description）+ Markdown 内容
- Skills 在 `~/.openclaw/workspace/skills/`（workspace 内）和 `~/.openclaw/extensions/*/skills/`（扩展目录）下被发现
- 按需加载：Skill 被一个 skill tool 按名称加载，内容注入 context

#### 1.2.2 状态模式
- Session 状态：main session vs isolated session
- 状态切换靠 `sessionTarget`（"main" / "isolated"）
- 状态对外暴露靠 cron + wake event 机制

#### 1.2.3 Subagent 模式
- `subagents: list / kill / steer` —— 子 agent 列表
- 父子状态独立：子 agent 的失败不影响主 session
- 完成事件 push-based：子 agent 完成后发事件到主 session

#### 1.2.4 Memory 体系
- `MEMORY.md`（长期）+ `memory/YYYY-MM-DD.md`（每日）
- 只在 main session 加载
- "Write It Down - No Mental Notes" 是硬规则

**对 microtrace 的影响**：
- ❌ 不引入 OpenClaw 的 skills 框架（VNFM 不需要动态技能）
- ❌ 不做多 session 模式（Phase 0 单 session）
- ✅ 学习"完成事件 push-based"思路（可观测性）
- ✅ 学习"memory 写入硬规则"（关键决策不靠记忆）

---

## 2. Loop 设计

### 2.1 控制结构（流式 + 事件驱动）

**关键修订**（基于 OpenCode 源码）：loop **不**是"LLM 调一次 → 解析响应"的 request/response 模式，而是**基于 event stream**。

```python
async def agent_loop(initial_input: str, ctx: Context, llm: LLMStream, tools: ToolRegistry) -> str:
    """
    主循环：状态机驱动，事件溯源，ReAct 模式
    退出路径：LLM conclude / max_iterations / 用户中断 / ask_user / doom_loop / overflow
    """
    # ── 状态机初始化 ──
    ctx.state = State.INTAKE
    ctx.iteration = 0

    # ── INTAKE 态：解析原始输入 ──
    await _intake(ctx, initial_input, llm, tools)
    if ctx.state == State.EXIT:
        return ctx.final_output

    # ── INVESTIGATE 态：主循环（流式）──
    ctx.state = State.INVESTIGATE

    while True:
        ctx.iteration += 1
        ctx.append_reasoning(f"[开始第 {ctx.iteration} 轮]")

        # 2.1.1 退出条件检查
        if ctx.iteration > ctx.max_iterations:
            await _transition(ctx, State.CONCLUDE, reason="max_iterations 到达")
            break

        if ctx.user_interrupt:
            await _transition(ctx, State.CONCLUDE, reason="用户中断")
            break

        # 2.1.2 Doom Loop 检测（在调用 LLM 前）
        if _check_doom_loop(ctx):
            question = f"检测到工具 {ctx.doom_loop_tool} 被连续 3 次以相同参数调用。请确认：\n1) 继续调用（确实需要）\n2) 换工具 / 换参数"
            await _enter_ask_user(ctx, question)
            continue  # ASK_USER 状态会等用户回复

        # 2.1.3 Prompt 组装（截取 + 排序）
        prompt = _assemble_prompt(ctx, tools)

        # 2.1.4 LLM 流式调用（核心变化点）
        ctx.append_reasoning(f"[LLM 调用] iter={ctx.iteration}, stream 模式")
        ctx.start_event("step.started")

        tool_calls_to_run: list[ToolCall] = []
        judgment_update: Judgment | None = None
        question: str | None = None
        conclusion: str | None = None

        # 流式接收 LLM 响应
        async for event in llm.stream(prompt, tools=tools.schemas()):
            # 2.1.5 事件处理（同步执行，记录事件）
            ctx.handle_stream_event(event)

            if event.type == "tool-input-start":
                # 工具调用开始，收集参数
                ctx.append_reasoning(f"[工具输入] {event.tool_name} 参数收集中...")
                tool_calls_to_run.append(ToolCall(
                    name=event.tool_name,
                    args=event.input,  # 流式累积
                    call_id=event.id,
                ))

            elif event.type == "tool-call":
                # 工具调用完整生成
                ctx.append_reasoning(f"[工具调用] {event.tool} args={_summarize(event.input)}")

            elif event.type == "text-end":
                # 文本流结束
                ctx.append_reasoning(f"[文本输出] {len(event.text)} 字")

            elif event.type == "step-finish":
                # LLM 调用结束
                ctx.append_reasoning(f"[Step 完成] finish_reason={event.finish_reason}, tokens={event.tokens}")

            elif event.type == "error":
                ctx.append_reasoning(f"[错误] {event.error}")
                # 错误恢复由 retry policy 处理
                raise StreamError(event.error)

        ctx.end_event("step.ended")

        # 2.1.6 响应后处理（基于累积的事件）
        if tool_calls_to_run:
            # ── 分支 1：执行工具调用（可并行）──
            if len(tool_calls_to_run) > 1 and not ctx.sequential_mode:
                # 并行执行（OpenCode 模式：parallelism when applicable）
                results = await asyncio.gather(*[
                    _execute_tool(ctx, call) for call in tool_calls_to_run
                ])
            else:
                # 串行执行（默认，避免日志混乱）
                results = []
                for call in tool_calls_to_run:
                    results.append(await _execute_tool(ctx, call))

            for call, result in zip(tool_calls_to_run, results):
                ctx.add_evidence(_result_to_evidence(result, call.name, ctx.iteration))

        elif judgment_update:
            # ── 分支 2：判断更新 ──
            old = ctx.current_judgment
            ctx.current_judgment = judgment_update
            ctx.append_reasoning(
                f"[判断更新] {old.category}→{judgment_update.category}, "
                f"confidence={judgment_update.confidence}, "
                f"reason={judgment_update.one_line_reason}"
            )

        elif question:
            # ── 分支 3：主动询问用户 ──
            await _enter_ask_user(ctx, question)
            continue  # 下一轮会从 ASK_USER 退出后继续

        elif conclusion:
            # ── 分支 4：LLM 认为结论已充分 ──
            ctx.final_output = conclusion
            ctx.append_reasoning(f"[LLM 自决结束] {conclusion[:100]}")
            break

        else:
            # 解析失败：没有 tool_call / judgment_update / question / conclusion
            ctx.append_reasoning("[警告] 流式响应未产生有效动作")
            # 继续（下一轮 LLM 会看到上次的 evidence 继续推理）

        # 2.1.7 溢出检查（Compaction 触发）
        if await _check_overflow(ctx, llm):
            await _trigger_compaction(ctx, llm)
            # compaction 后继续 loop，context 已压缩
            continue

    # ── CONCLUDE 态：格式化输出 ──
    return await _conclude(ctx)
```

### 2.2 事件溯源（State = Event Projection）

**新增章节**（基于 OpenCode 源码）：状态机的"当前状态" = 从事件流投影出来。

```python
class EventStore:
    """事件存储（append-only）"""
    events: list[Event]

    def append(self, event: Event) -> None:
        self.events.append(event)

    def project_state(self) -> State:
        """从事件流投影当前状态"""
        # 取最后一个 state 转换事件
        for event in reversed(self.events):
            if event.type.startswith("state."):
                return State(event.data["to_state"])
        return State.INTAKE  # 默认

# 使用示例
event_store.append(Event("state.entered", {"from": "INTAKE", "to": "INVESTIGATE"}))
current_state = event_store.project_state()  # → INVESTIGATE
```

**为什么事件溯源**：
- ✅ 状态可重放（"给我某步的状态"）
- ✅ 多个客户端可各自投影（REPL / 日志 / 测试）
- ✅ 状态转移天然可观测（每个 event 都是 log）
- ✅ 与 OpenCode 模式对齐

### 2.3 单次迭代的原子动作（流式版）

每个 iteration = 1 次 LLM stream + 0~N 次工具执行（并行可选）：

```
iteration 原子动作序列（流式）：
1. 退出条件检查（iter 超限 / 用户中断 / doom_loop）
2. Prompt 组装（system + problem + judgment + evidence + trace + tools）
3. llm.stream() → 异步迭代事件流
4. 事件处理：
   - text-delta → 实时显示（REPL）
   - reasoning-delta → 实时显示（思考过程）
   - tool-input-start → 准备执行
   - tool-call → 工具调用完整生成
   - text-end → 文本流结束
   - step-finish → LLM 调用结束
   - error → retry 触发
5. 后处理：
   - 工具并行执行（可选）
   - 证据追加
   - 判断更新
   - ask_user / conclude
6. 溢出检查 → 可能触发 compaction
7. 记录推理轨迹
```

### 2.4 错误恢复（修订：加 Retry Policy）

| 错误类型 | 处理策略 | 是否重试 |
|---|---|---|
| **工具执行失败**（文件找不到 / 日志空 / 超时） | 工具返回 error result → 作为 evidence（source=error）→ 继续 loop | ❌ 不重试 |
| **LLM 解析失败**（stream 事件不符合 schema） | 记录原始 event → 降级为"未产生动作"→ 继续下一轮 | ❌ 不重试 |
| **网络中断 / API 5xx / 限流（429）** | SessionRetry.policy 自动重试 | ✅ 重试 3 次（2s/4s/8s 指数退避） |
| **API 4xx（参数错）** | 不重试，记录错误到 reasoning_trace | ❌ permanent error |
| **工具超时**（单工具 > 30s） | 工具层 timeout → 返回 `ToolTimeoutError` → 继续 | ❌ |
| **LLM stream 截断 / 中断** | 记录 warning → 重试 1 次 | ✅ |
| **Context 溢出** | 触发 compaction → 压缩后继续 | ✅ 自动处理 |

**Retry 策略实现**（来自 OpenCode `SessionRetry.policy`）：

```python
class RetryableError(Exception):
    """可重试错误（transient）"""
    pass

class PermanentError(Exception):
    """不可重试错误（permanent）"""
    pass

# LLM client 层
async def call_with_retry(llm_func, *args, max_retries=3):
    for attempt in range(max_retries):
        try:
            return await llm_func(*args)
        except (NetworkError, RateLimitError, ServerError) as e:
            if attempt == max_retries - 1:
                raise PermanentError(f"重试 {max_retries} 次后仍失败: {e}")
            await asyncio.sleep(2 ** attempt)  # 2s/4s/8s
        except (BadRequestError, AuthError) as e:
            raise PermanentError(f"参数/认证错误，不重试: {e}")
```

### 2.5 Doom Loop 检测（修订：用 OpenCode 精确匹配模式）

**修订说明**：之前设计是"参数相似度 > 80%"启发式。OpenCode 源码显示**用精确 JSON 匹配 + 阈值 3**更可靠。

```python
DOOM_LOOP_THRESHOLD = 3  # 与 OpenCode 一致

def _check_doom_loop(ctx: Context) -> bool:
    """
    检测最近 3 次 tool call 是否完全相同（tool name + input）
    如果是，标记 ctx.doom_loop_tool，进入 ASK_USER 询问用户
    """
    if len(ctx.tool_history) < DOOM_LOOP_THRESHOLD:
        return False

    last_calls = ctx.tool_history[-DOOM_LOOP_THRESHOLD:]

    # OpenCode 模式：所有 3 次必须相同
    if not all(
        call.name == last_calls[0].name and
        json.dumps(call.args, sort_keys=True) == json.dumps(last_calls[0].args, sort_keys=True)
        for call in last_calls
    ):
        return False

    # 标记 doom loop
    ctx.doom_loop_tool = last_calls[0].name
    ctx.doom_loop_args = last_calls[0].args
    ctx.append_reasoning(
        f"[DOOM LOOP] 工具 {last_calls[0].name} 被连续 {DOOM_LOOP_THRESHOLD} 次以相同参数调用"
    )
    return True
```

**与之前"参数相似度 80%"的差异**：

| 维度 | 之前设计 | 修订后（OpenCode 模式） |
|---|---|---|
| 检测方式 | 关键词重叠 > 80% | JSON 精确匹配 |
| 阈值 | 2 次 | 3 次 |
| 触发后 | 仅 warning 记录 | **进入 ASK_USER 弹窗问用户** |
| 误判风险 | 关键词可能虚高（"日志"在多场景出现） | 极低（精确匹配） |

### 2.6 主动询问用户（ASK_USER 分支）

```python
# LLM 在 INVESTIGATE 循环中说"我需要 X 信息才能继续"
# → 状态切为 ASK_USER，yield 问题给用户
# → 用户回复后 → 状态切回 INVESTIGATE → 用户回复作为 context 补充
```

**触发条件**（两种）：
1. **LLM 自决**：在 response 里声明 `{"action": "ask_user", "question": "..."}`
2. **Doom Loop 触发**（新增）：系统检测到工具重复调用，主动 ask user

**不阻断 loop**：不是 await 死等，而是 yield 后继续。

### 2.7 LLM 响应解析

**核心变化**：解析是**流式事件**而不是单一 JSON 响应。

```python
# LLM 收到 prompt 后，输出 stream：
# event 1: { type: "text-start" }
# event 2: { type: "text-delta", delta: "我先" }
# event 3: { type: "text-delta", delta: "需要" }
# event 4: { type: "text-end", text: "我需要查一下..." }
# event 5: { type: "tool-input-start", tool_name: "find_class" }
# event 6: { type: "tool-input-delta", delta: "{..." }
# event 7: { type: "tool-input-end", input: {"class_name": "UserService"} }
# event 8: { type: "tool-call", tool: "find_class", input: {...} }
# event 9: { type: "step-finish", finish_reason: "tool_calls" }

# 解析器累积这些事件，投影出 4 种响应类型之一：
# - tool_call(s)（执行工具）
# - judgment_update（更新判断）
# - ask_user（问用户）
# - conclude（结束）
```

**LLM system prompt** 要指示 LLM 在文本流结束前**不**给出最终结论，**只**给出思考 + 工具调用（或结论）。

---

## 3. 状态机设计

### 3.1 两层设计：显式状态机 + 事件溯源

**修订**（基于 OpenCode）：状态机有**两层**：

1. **高层：5 显式态**（REPL UI 友好）
2. **底层：事件溯源**（state = event 投影）

```
                    高层（REPL 显示）              底层（事件流）
                    ┌──────────┐                  ┌──────────────┐
                    │  INTAKE  │                  │ state.entered│
                    └─────┬────┘                  │   INTAKE     │
                          │                       └──────────────┘
                          ▼
                    ┌──────────┐◀─────┐          ┌──────────────┐
                    │INVESTIGATE│     │          │state.entered │
                    │ (主循环)  │─────┘          │   INVESTIGATE│
                    └─────┬────┘                 └──────────────┘
                          │
        ┌───── LLM ask_user ────┐
        │                      │
        ▼                      │
   ┌──────────┐                │
   │ ASK_USER │                │
   └─────┬────┘                │
         │ 用户回复              │
         └──────────────────────┘
                          │
                          ▼
                    ┌──────────┐                  ┌──────────────┐
                    │ CONCLUDE │                  │state.entered │
                    └──────────┘                  │   CONCLUDE   │
                                                  └──────────────┘
                          │
                          ▼ EXIT
```

### 3.2 高层 5 态定义

| 态 | 含义 | 进入条件 | 退出条件 | 可用工具 |
|---|---|---|---|---|
| **INTAKE** | 解析原始报错 | agent_loop 开始 | problem 结构化完成 或 解析失败 | ❌ 无 |
| **INVESTIGATE** | 主推理循环 | INTAKE 完成 | conclude / max_iter / 用户中断 / ask_user / overflow | ✅ 4 个工具 |
| **ASK_USER** | 等待用户补料 | INVESTIGATE 内 LLM 请求 / Doom Loop 触发 | 用户回复后返回 INVESTIGATE | ❌ 无（用户在思考） |
| **CONCLUDE** | 格式化结论 | 所有退出路径汇聚点 | 输出完成 | ❌ 无 |
| **EXIT** | 异常退出 | INTAKE 严重失败 / 不可恢复错误 | 直接返回 error message | ❌ 无 |

### 3.3 状态转移事件

```python
# 事件类型
StateEntered(state: State, from_state: State | None, timestamp: float)
StateExited(state: State, to_state: State, reason: str, timestamp: float)
```

**每次状态转移都产生 2 个事件**（enter + exit），便于完整重放。

### 3.4 状态 handler（仍保留 enter/tick/exit 模式）

虽然底层是事件溯源，**handler 模式**对 REPL UI 友好：

```python
class StateHandler:
    @staticmethod
    async def enter(ctx: Context, **kwargs):
        """进入态时执行"""
        ctx.append_reasoning(f"[STATE→{ctx.state.name}] enter")
        ctx.event_store.append(Event(
            type="state.entered",
            data={"state": ctx.state.name, "from": kwargs.get("from_state")},
        ))

    @staticmethod
    async def tick(ctx: Context, **kwargs) -> bool:
        """态内主逻辑，返回 True=该退出了"""
        return True  # 各态不同

    @staticmethod
    async def exit(ctx: Context, to_state: State, reason: str):
        """退出态时执行"""
        ctx.append_reasoning(f"[STATE→{ctx.state.name}→{to_state.name}] exit, reason={reason}")
        ctx.event_store.append(Event(
            type="state.exited",
            data={"from": ctx.state.name, "to": to_state.name, "reason": reason},
        ))
```

### 3.5 状态转换完整矩阵

| 当前态 | 事件 | 下一态 | 触发条件 |
|---|---|---|---|
| INTAKE | intake 完成 | INVESTIGATE | problem 解析成功（含失败兜底） |
| INTAKE | 严重错误 | EXIT | LLM 完全无法解析 + raw_input 为空 |
| INVESTIGATE | LLM conclude | CONCLUDE | stream 事件产生 conclusion |
| INVESTIGATE | max_iter 到达 | CONCLUDE | iteration > max_iterations |
| INVESTIGATE | 用户中断 | CONCLUDE | user_interrupt == True |
| INVESTIGATE | LLM ask_user | ASK_USER | stream 事件产生 question |
| INVESTIGATE | Doom Loop | ASK_USER | 3 次精确匹配 |
| INVESTIGATE | Overflow | INVESTIGATE（compaction 后继续） | isOverflow() == True |
| INVESTIGATE | LLM tool_call | INVESTIGATE（自身） | 工具执行后继续 |
| INVESTIGATE | LLM judgment_update | INVESTIGATE（自身） | judgment 更新后继续 |
| ASK_USER | 用户回复 | INVESTIGATE | 用户消息到达 |
| ANY | fatal_error | EXIT | 捕获未处理异常 |

### 3.6 Tool 子状态机（新增）

每个 tool call 内部有 4 态子状态机：

```
pending → running → completed
                    → error
```

```python
class ToolState(Enum):
    PENDING = "pending"     # 工具参数收集中（流式 input）
    RUNNING = "running"     # 工具执行中
    COMPLETED = "completed" # 成功完成
    ERROR = "error"         # 执行失败

# 状态转换
def tool_lifecycle(call: ToolCall) -> AsyncIterator[ToolEvent]:
    yield ToolEvent("tool.pending", call)
    try:
        result = await execute_tool(call)
        yield ToolEvent("tool.completed", {"call": call, "result": result})
    except Exception as e:
        yield ToolEvent("tool.error", {"call": call, "error": str(e)})
```

**对 microtrace 的影响**：
- 工具执行有明确生命周期
- REPL 可以显示"工具 X 执行中... 5s"
- 错误能被精确捕获

### 3.7 状态转换测试覆盖策略

```python
# tests/test_state_machine.py
# 覆盖每条状态转移路径
# + Tool 4 态子状态机
# + Doom Loop 触发
# + Compaction 触发
```

---

## 4. Context 设计

### 4.1 数据结构

```python
@dataclass
class Problem:
    """问题陈述（INTAKE 输出）"""
    raw_input: str
    error_type: str
    stack_frames: list[StackFrame]
    log_snippets: list[str]
    timestamp: datetime | None

@dataclass
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
    """证据（只增不减，截取在 prompt 层做）"""
    id: str
    source: Literal["code", "log", "stack", "tool_output", "error", "user"]
    location: str
    content: str
    raw_content: str
    relevance: float             # 0.0~1.0
    discovered_at_iteration: int
    importance: Literal["critical", "supporting", "background"]

@dataclass
class CompactionRecord:
    """Compaction 记录（修订：新增）"""
    id: str
    triggered_at_iteration: int  # 在哪一轮触发
    reason: Literal["auto_overflow", "manual"]
    tokens_before: int           # 压缩前总 token
    tokens_after: int            # 压缩后总 token
    summary: str                 # 压缩摘要
    preserved_evidence_ids: list[str]  # 保留的关键证据 ID
    timestamp: float

@dataclass
class ToolCall:
    name: str
    args: dict                   # 完整 args（用于 doom loop 检测）
    args_summary: str
    output_summary: str
    output_raw: Any
    iteration: int
    error: str | None
    state: Literal["pending", "running", "completed", "error"]  # 修订：4 态

@dataclass
class Event:
    """事件溯源事件"""
    type: str                    # state.entered, tool.called, step.finished ...
    data: dict
    timestamp: float
    iteration: int | None

@dataclass
class Context:
    problem: Problem | None = None
    current_judgment: Judgment = field(
        default_factory=lambda: Judgment("UNKNOWN", 0.0, "尚未开始", "")
    )
    evidence: list[Evidence] = field(default_factory=list)
    tool_history: list[ToolCall] = field(default_factory=list)
    reasoning_trace: list[str] = field(default_factory=list)
    MAX_REASONING_TRACE = 10

    # ── 新增：Compaction 记录（修订）──
    compactions: list[CompactionRecord] = field(default_factory=list)
    cumulative_tokens: int = 0     # 用于 overflow 检测

    # ── 新增：事件溯源（修订）──
    event_store: list[Event] = field(default_factory=list)
    state: State = State.INTAKE

    # ── 原有字段（保留）──
    user_replies: list[UserReply] = field(default_factory=list)
    iteration: int = 0
    max_iterations: int = 8
    user_interrupt: bool = False
    pending_question: str | None = None
    final_output: str | None = None
    last_tool_result_summary: str = ""

    # ── Doom Loop（修订）──
    doom_loop_tool: str | None = None
    doom_loop_args: dict | None = None
```

### 4.2 内容来源

| 内容 | 来源 | 更新时机 |
|---|---|---|
| problem | 用户原始输入 + INTAKE LLM 解析 | INTAKE 一次性完成 |
| current_judgment | LLM judgment_update 分支 | INVESTIGATE 循环内 |
| evidence | 工具执行结果 | 工具返回后 |
| tool_history | 工具执行记录 | 每次工具调用 |
| reasoning_trace | 所有重要事件 | 任何状态变更时 |
| user_replies | 用户对 ask_user 的回复 | ASK_USER 退出后 |
| compactions | compaction 触发后 | overflow 检测时 |
| event_store | 所有事件 | append-only |
| final_output | CONCLUDE 态生成 | CONCLUDE 完成时 |

### 4.3 更新机制

**核心原则：**
- 证据 / 事件 / tool_history：只追加，不修改，不删除
- judgment：覆盖（设计为单例）
- compactions：追加
- event_store：append-only（事件溯源的本质）

### 4.4 Compaction 机制（**新增**，基于 OpenCode 源码）

**修订说明**：之前 DESIGN.md 说"不压缩"。**错了**。VNFM 问题定位跑 5-10 轮工具调用，evidence 累积快，**必须做 compaction**。

**触发条件**（自动 + 手动）：

```python
COMPACTION_BUFFER = 20_000  # 保留 20K token 给 summary（与 OpenCode 一致）

def _check_overflow(ctx: Context, llm: LLMClient) -> bool:
    """
    检查 context 是否溢出
    参考 OpenCode overflow.ts 实现
    """
    # 当前累计 token + 本次 prompt 估算 > 模型 context - buffer
    estimated_total = ctx.cumulative_tokens + ctx.estimated_prompt_tokens
    usable = llm.context_window - COMPACTION_BUFFER
    return estimated_total >= usable
```

**Compaction 流程**：

```python
async def _trigger_compaction(ctx: Context, llm: LLMClient) -> None:
    """
    触发 compaction
    1. 把"关键 evidence"标记为 preserved（不压缩）
    2. 旧 evidence + 旧 reasoning_trace → 生成 summary
    3. summary 插入 context（作为新消息）
    4. 旧 evidence 保留在 raw_content（不删）
    """
    ctx.append_reasoning("[COMPACTION] 触发")
    ctx.event_store.append(Event("compaction.started", {"reason": "auto_overflow"}))

    # 1. 标记 preserved
    preserved = [e for e in ctx.evidence if e.importance == "critical"]

    # 2. 调 LLM 生成 summary（针对非 critical 的）
    summary_prompt = _build_compaction_prompt(
        evidence=[e for e in ctx.evidence if e.importance != "critical"],
        reasoning_trace=ctx.reasoning_trace,
    )
    summary = await llm.summarize(summary_prompt)

    # 3. 记录
    record = CompactionRecord(
        id=str(uuid.uuid4()),
        triggered_at_iteration=ctx.iteration,
        reason="auto_overflow",
        tokens_before=ctx.cumulative_tokens,
        tokens_after=len(summary) * 1.3,  # 估算
        summary=summary,
        preserved_evidence_ids=[e.id for e in preserved],
        timestamp=time.time(),
    )
    ctx.compactions.append(record)

    # 4. 更新 reasoning_trace（只保留最近 + summary）
    ctx.reasoning_trace = [
        f"[COMPACTION] 已压缩 {len(ctx.evidence) - len(preserved)} 条 evidence",
        f"[COMPACTION] Summary: {summary[:200]}",
    ] + ctx.reasoning_trace[-3:]

    # 5. 标记旧 evidence 为"已压缩"（不删，存 raw_content）
    for e in ctx.evidence:
        if e.importance != "critical":
            e.importance = "background"  # 降级，不进 prompt

    ctx.append_reasoning(f"[COMPACTION] 完成，保留 {len(preserved)} critical evidence")
    ctx.event_store.append(Event("compaction.ended", {"preserved": len(preserved)}))
```

**Compaction 策略对比**：

| 策略 | 实现 | 优 | 劣 |
|---|---|---|---|
| **A. 滚动压缩** | 保留 critical，旧的 summarization | 信息密度高 | 实现复杂 |
| **B. 全量 summarization** | 整个 context 生成 summary | 简单 | 关键证据可能丢 |
| **C. Token 预算截取** | 只在 prompt 层截取，context 完整保留 | 永不丢 | 调 LLM 时仍可能超限 |

**microtrace 选 A**（与 OpenCode 思路一致：保留关键 + 摘要 + 滚动）。

### 4.5 Prompt 组装策略（修订：加 compaction-aware）

```python
def _assemble_prompt(ctx: Context, tools: ToolRegistry) -> str:
    """
    从 context 组装 prompt
    修订：context 含 compaction 记录，组装时优先用 summary
    """
    sections = []

    # 1. System Prompt（全量）
    sections.append(_load_system_prompt())

    # 2. Problem
    sections.append(_format_problem(ctx.problem))

    # 3. Judgment
    sections.append(_format_judgment(ctx.current_judgment))

    # 4. Evidence Pool
    # 修订：只取 critical + supporting，background 进 raw_content 但不进 prompt
    # + 上次 compaction 的 summary 作为补充上下文
    evidence_text = _format_evidence_pool(
        ctx.evidence,
        max_items=5,
        max_content_len=500,
    )
    if ctx.compactions:
        evidence_text += "\n\n## 历史压缩摘要\n"
        evidence_text += _format_compactions(ctx.compactions[-2:])  # 最近 2 次
    sections.append(evidence_text)

    # 5. Reasoning Trace（最近 3 步）
    sections.append(_format_reasoning_trace(ctx.reasoning_trace, max_steps=3))

    # 6. User Replies
    if ctx.user_replies:
        sections.append(_format_user_replies(ctx.user_replies))

    # 7. Available Tools
    sections.append(_format_tools(tools))

    # 8. Instruction
    sections.append(_build_instruction(ctx))

    return "\n\n".join(sections)
```

**截取规则**（不变，**与 OpenCode 不压缩的 AGENTS.md 思路一致**）：

| 内容 | 截取规则 | 理由 |
|---|---|---|
| problem.raw_input | > 2000 字截取 | 原始输入太长时尾部不关键 |
| evidence[].content | > 500 字截取 | 单条证据太长 |
| evidence pool | 最多 5 条，按 importance+relevance | prompt 预算 |
| reasoning_trace | 最近 3 步 | 历史太长则从 context 重建 |
| tool_history[].output | 不进 prompt | 在 evidence 层处理 |
| agent.md (master prompt) | **不压缩**（与 OpenCode AGENTS.md 一致） | 推理规则始终全量 |

### 4.6 Prompt 组装 vs Compaction 的关系

```
正常情况：
  Context → Prompt 组装（截取）→ LLM

溢出情况：
  Context 累积超过阈值
    → 触发 Compaction（保留 critical + 生成 summary）
    → Context 精简
    → Prompt 组装（用 summary + critical evidence）
    → LLM
```

**关键**：compaction 是**异步可选**的，prompt 组装永远在最后做截取作为"实时保护"。

---

## 5. 与 v1（Tracekit）的差异（修订版）

### 5.1 状态机：3 态 → 5 态 + 事件溯源层

| v1 | v2 | 理由 |
|---|---|---|
| INTAKE / INVESTIGATE / CONCLUDE（3 态） | INTAKE / INVESTIGATE / ASK_USER / CONCLUDE / EXIT（5 态） | v1 漏了"主动询问用户" |
| 没有 EXIT 态 | 增加 EXIT 态 | INTAKE 严重失败有明确出口 |
| 没有事件溯源 | 加事件溯源层 | 可重放、可观测、与 OpenCode 对齐 |
| Tool 内部状态混乱 | Tool 4 态子状态机（pending/running/completed/error） | 工具生命周期显式 |

### 5.2 Loop 模式

| v1 | v2 | 理由 |
|---|---|---|
| Request/Response（单次 LLM call） | **流式 LLM stream**（基于 event） | OpenCode 模式，可实时显示思考 |
| 没有 Doom Loop 检测 | **Doom Loop 检测**（3 次精确匹配 + ask user） | 之前说"不检测"是错的 |
| 没有 Retry 策略 | **Retry 策略**（transient 重试，permanent 不重试） | 生产环境需要容错 |
| 没有 Compaction | **Compaction**（auto on overflow + 20K buffer） | 之前说"不压缩"是错的 |

### 5.3 Loop 退出条件

| v1 | v2 | 理由 |
|---|---|---|
| LLM 自决 / max_iterations | LLM 自决 / max_iterations / 用户中断 / ask_user / **doom_loop** / **overflow** | 6 条退出路径，覆盖所有异常 |

### 5.4 Context 设计

| v1 | v2 | 理由 |
|---|---|---|
| 4 字段 evidence | 6 字段 evidence（+ relevance + importance） | relevance 排序，importance 截取优先级 |
| 无 raw_content | 增加 raw_content | 压缩评估需要原始内容 |
| **无 Compaction** | **加 CompactionRecord** | 长会话必须做压缩 |
| **无事件溯源** | **加 event_store** | 与 OpenCode 模式对齐 |
| 无 user_replies | 增加 user_replies | ask_user 场景需要记录 |

### 5.5 错误处理

| v1 | v2 | 理由 |
|---|---|---|
| 无显式错误处理 | 完整错误恢复矩阵 | VNFM 生产环境需可靠 |
| 无 Retry 策略 | Retry 策略（指数退避） | transient error 容忍 |
| 无 Doom Loop 检测 | Doom Loop（3 次精确匹配） | 工具重复调无新信息是高频问题 |
| 无 Compaction | Compaction（auto on overflow） | 防止 context 溢出 |

### 5.6 Prompt 组装

| v1 | v2 | 理由 |
|---|---|---|
| "截取策略"4 点太粗 | 8-section 完整模板 | 更可控 |
| LLM 响应用正则解析 | **流式事件解析**（OpenCode 模式） | 更可靠 + 支持实时显示 |
| 无 Compaction 集成 | Compaction summary 注入 prompt | 长会话保持关键信息 |
| JSON 解析为主 + 正则 fallback | **保留**（流式事件 + JSON 块） | 兼容模式 |

---

## 6. 落地技术选型

### 6.1 实现语言

**Python 3.11+**

理由：VNFM 工程师主要语言是 Java，Python 用于 agent 胶水层足够简单。

### 6.2 LLM 调用

**MiniMax API（默认）+ 抽象接口**

```python
class LLMClient(Protocol):
    async def stream(self, prompt: str, tools: list[ToolSchema]) -> AsyncIterator[StreamEvent]: ...
    async def complete(self, prompt: str) -> str: ...  # 流式不便时用
```

**支持流式**（基于 OpenCode 模式）：

```python
# MiniMax / OpenAI / Anthropic 都支持 stream
# 统一封装成 AsyncIterator[StreamEvent]
```

### 6.3 HTTP API

**FastAPI**（与 VISION 一致）

### 6.4 REPL

**prompt_toolkit**（与 VISION 一致）

### 6.5 项目结构

```
microtrace/
├── src/
│   ├── agent/
│   │   ├── state.py            # 5 态枚举 + handler
│   │   ├── events.py           # 事件类型 + EventStore
│   │   ├── loop.py             # agent_loop（流式版）
│   │   └── doom_loop.py        # Doom Loop 检测
│   ├── tools/
│   │   ├── base.py             # Tool.define 模式
│   │   ├── read_file.py
│   │   ├── search_logs.py
│   │   ├── find_class.py
│   │   └── parse_stack_trace.py
│   ├── llm/
│   │   ├── client.py           # 抽象接口
│   │   └── minimax.py          # MiniMax 实现
│   ├── context/
│   │   ├── models.py           # Problem / Judgment / Evidence / etc
│   │   ├── compaction.py       # Compaction 机制
│   │   └── prompt.py           # Prompt 组装
│   ├── repl/
│   │   ├── main.py             # REPL 入口
│   │   └── commands.py         # /status, /evidence, /save
│   └── http/
│       └── api.py              # FastAPI
├── prompts/
│   └── agent.md                # Master prompt
├── tests/
│   ├── test_state_machine.py
│   ├── test_loop.py
│   ├── test_doom_loop.py
│   ├── test_compaction.py
│   └── test_tools.py
└── docs/
    ├── VISION.md
    ├── ARCHITECTURE.md
    └── DESIGN.md
```

### 6.6 借鉴 OpenCode 源码（明确"借鉴意图，不抄代码"）

| OpenCode 模式 | microtrace 借鉴 | 抄代码？ |
|---|---|---|
| Stream-based loop | ✅ 用 | ❌ 自己写 |
| Event sourcing 25+ 事件 | ✅ 简化到 10 个左右 | ❌ 自己定义 |
| Tool 4 态子状态机 | ✅ 用 | ❌ 自己实现 |
| Doom Loop 3 次精确匹配 | ✅ 复用阈值 | ❌ 自己实现 |
| Compaction auto + 20K buffer | ✅ 用 | ❌ 自己实现 |
| Retry policy | ✅ 简化版 | ❌ 自己实现 |
| Snapshot tracking | ❌ 不做 | ❌ |
| Status tracking | ❌ 不做 | ❌ |
| Agent/Model switching | ❌ Phase 0 不做 | ❌ |

---

## 7. 待老板拍板的问题（修订版）

### Q1. ASK_USER 态：阻塞 ✅ **已决定**

**决定**：**硬阻塞 + 无自动超时**（等用户回复，跟 OpenCode 一致）

理由（老板原话 2026-06-05）：
> "啥时候回复了，提交信息之后，再接着跑就行了。就像 Open code 现在的实现一样"

**REPL UX 设计**：

```
╭─ ⚠️ microtrace · 等待输入 ─────────────────────╮
│  状态: ASK_USER                                  │
│  第 3 轮 · 已用 2 次工具调用                      │
│                                                  │
│  Agent 在问：                                     │
│  ┌──────────────────────────────────────────┐  │
│  │ 报错时间窗口大概是？                          │  │
│  └──────────────────────────────────────────┘  │
│                                                  │
│  可选：直接输入 / skip / ctrl+c                  │
╰──────────────────────────────────────────────────╯

microtrace> 10:23 - 10:30
[15:23:45] 收到回复
[15:23:45] [INVESTIGATE] 第 4 轮：调 search_logs range=10:23-10:30 ...
```

**取消 / 跳过机制**：
- **直接输入** → 立刻继续 loop
- **输入 `skip`** → 标 "UNANSWERED"，agent 继续（不阻塞）
- **`ctrl+c`** → 整个 session 暂停，回到 REPL 顶层
- **无自动超时**——用户离开 = session 卡住（用户责任，跟 OpenCode 一致）

**关于"用户主动补料"**（Q3 待定）：
- 用户在 REPL 顶层（不在 ASK_USER 态时）输入信息时，行为待定

### Q2. ASK_USER 触发 + 护栏 + UI ✅ **已决定**（与 OpenCode 保持一致）

**决定**（老板 2026-06-05）："都跟 Open code 保持一致"

**三层设计**：

#### Q2a 触发机制

- **触发 1（主）**：LLM 自决（`prompts/agent.md` 教它何时该问）
- **触发 2（兜底）**：Doom Loop（OpenCode 模式，连续 3 次精确匹配工具调用触发 `permission.ask`，复用 ASK_USER 态）

#### Q2b 护栏：纯 Prompt（**与 OpenCode 一致**）

OpenCode **没有**代码级 rate limit。老板决定 follow 它——护栏全部在 `prompts/agent.md`。

OpenCode `codex.txt` 的核心规则（直接借鉴到我们的 `agent.md`）：

```markdown
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

#### Q2c UI：多选 + 自定义文本（**OpenCode 模式**）

`Question.Prompt` schema（来自 `src/question/index.ts`）：

```python
@dataclass
class QuestionOption:
    label: str           # 1-5 字
    description: str     # 解释选项

@dataclass
class QuestionPrompt:
    question: str
    header: str          # 短标签（max 30 字）
    options: list[QuestionOption]
    multiple: bool = False     # 是否多选
    custom: bool = True        # 允许自定义答案
```

**REPL 渲染**：

```
╭─ ⚠️ microtrace · 等待输入 (Q2) ─────────────────╮
│  触发: LLM 自决                                  │
│  第 3 轮 · 已用 2 次工具调用                      │
│                                                  │
│  报错时间窗口大概是？                              │
│  (用于精确定位 search_logs)                        │
│                                                  │
│  [1] 10:00 - 11:00   # 整点范围                   │
│  [2] 10:23 - 10:30   # 我看到日志有报错时间        │
│  [3] 不知道          # 让 agent 自己查              │
│  [4] 自定义...                                     │
│                                                  │
│  → 输入数字 1/2/3/4 选中                          │
╰──────────────────────────────────────────────────╯

microtrace> 2
[15:23:45] 收到回复：[10:23 - 10:30]
```

#### Q2d 4 条原则（**全部采纳**写进 `prompts/agent.md`）

1. **默认不问** —— "do the work without asking questions"
2. **infer 优先** —— "Treat short tasks as sufficient direction; infer missing details"
3. **3 个明确 OK 场景** —— 模糊 / 破坏性 / 需要 secret（其他都不要问）
4. **一次 1 个问题** —— "ask exactly ONE targeted question, include your recommended default"

#### 为什么放弃代码级 rate limit

- 老板明确跟 OpenCode 走
- OpenCode 实战验证了纯 prompt 够用
- 代码 rate limit 留作"未来真出问题再加"的 YAGNI 候选
- prompt 写得好的话 LLM 会自己学会不乱问

#### 关于我之前提议的"代码 rate limit"——降级为**可选**

- Phase 0 不做代码 rate limit
- 如果将来发现 LLM 真的乱问（比如 prompt 写得不好），**再加** `AskUserGuard` 类
- 但**默认不启用**

### Q3. judgment_update：覆盖 vs 版本化 ✅ **已决定 → 版本化**

**决定**（老板 2026-06-05）：**版本化 + 保留 current**。

老板原话：
> "我觉得是不是有这种历史记录的比较好，至少知道都判断过哪些可能性... 这样在我们目前的 agent 能力构建阶段，还没有这么强的时候，我可以基于这个进行一些调试和优化"

**数据结构**：

```python
@dataclass
class Context:
    current_judgment: Judgment           # 最新值（单例）
    judgment_history: list[Judgment]     # 全部历史（append-only）
    
    def update_judgment(self, new: Judgment) -> None:
        old = self.current_judgment
        self.current_judgment = new
        self.judgment_history.append(new)
        self.append_reasoning(
            f"[判断更新 #{len(self.judgment_history)}] "
            f"{old.category}→{new.category}, "
            f"confidence={old.confidence:.2f}→{new.confidence:.2f}"
        )
```

**REPL 增强**（`/judgment` 命令）：

```
microtrace> /judgment

判断历史（4 次更新）：

  #1  iter=1  UNKNOWN→A  0.50→0.55
      "NPE 在我们代码，没看到校验拦截"
      
  #2  iter=3  A→A  0.55→0.70
      "读代码确认 userId 来自 OrderService"
      
  #3  iter=5  A→B  0.70→0.82  ★ 翻车
      "日志显示 downstream OMS returned 500"
      
  #4  iter=6  B→B  0.82→0.88

当前: B (下游产品报错)  0.88
```

**关键标记**：
- `★ 翻车` —— 类别方向变化（UNKNOWN→A、A→B、C→A 等）
- `→同` —— 类别不变，置信度变化

**为什么不是覆盖**：
- 开发阶段 agent 还不强，**history 是 debug 神器**
- 出错时能直接看"当时怎么想的"
- 调优 prompt 时看"agent 在哪里翻来覆去"
- 成本几乎为零（几 KB 存储）

**未来**（agent 强了以后）：
- 如果觉得 history 啰嗦，可以加 `/judgment clear` 命令手动清
- 或者只保留最近 N 次（但默认不砍，保留全量）

#### Q3.5: history 进 LLM context 吗？✅ **已决定 → 不进**

**决定**（老板 2026-06-05）：**LLM 不感知 judgment history**

**理由**：
- LLM 每轮做的是"**现在**该判什么"，不需要"我之前判过啥"
- 看 history 反而**引入确认偏倚**——LLM 倾向"维持一致"或"刻意不同"
- transition 已被 `reasoning_trace` 捕获（"判断更新 #3: A→B, 0.70→0.82"）
- 8 条 × 200 字 ≈ 12.8K token 浪费（VNFM 调查 evidence 已重）
- 跟 OpenCode 保持一致

**数据流**：

```
工具调用 / 推理
    ↓
LLM 输出 judgment_update
    ↓
update_judgment()
    ├─→ ctx.current_judgment = new（覆盖）──→ 喂 LLM
    └─→ ctx.judgment_history.append(new)（追加）──→ 只给 REPL
    ↓
build_prompt() 只放 current_judgment
```

**例外**：如果未来 LLM 真要"回看"（Phase 1+），可以加 `/trace` 工具让它主动读 history。**Phase 0 不实现**（YAGNI）。

### Q4. max_iterations：固定上限 + 可配置 ✅ **已决定**

**决定**（老板 2026-06-05）：
- **固定上限**（默认 8，**不跟 OpenCode 的 Infinity**）
- **通过配置文件可配**（不是硬编码）
- **达上限行为跟 OpenCode 保持一致**（注入 MAX_ITERATIONS prompt，强制 LLM 总结）

**为什么不选 Infinity**（OpenCode 模式）：
- Phase 0 agent 还不强，**固定上限是安全网**
- VNFM 调查有界（3-8 轮就够），不像代码编辑需要 30-50 步
- OpenCode 选 Infinity 是因为它有 **Doom Loop + Compaction + Question + Permission** 多重保护
- 我们保护没那么多，固定上限兜底
- **未来观察 1-2 个月**，看 agent 是否"经常到 8 轮还在干有价值的工作"——是的话升到 12

**配置**：

```yaml
# ~/.config/microtrace/config.yaml
agent:
  max_iterations: 8  # 默认 8，可改
```

**CLI 改**：
```bash
microtrace config set max_iterations 12
```

**达上限行为**（跟 OpenCode 一致）：

```python
# Loop 主逻辑末尾
if ctx.iteration >= ctx.max_iterations:
    # 不 break，注入 MAX_ITERATIONS 提示，强制 LLM 总结
    forced_prompt = _build_forced_summary_prompt(ctx)
    # 注意：tools=[] 禁止任何工具调用
    response = await llm.stream(forced_prompt, tools=[])
    ctx.final_output = response.text
    ctx.append_reasoning("[MAX_ITERATIONS] 强制总结，无工具调用")
    # 进入 CONCLUDE 态
```

**MAX_ITERATIONS prompt**（仿 OpenCode `max-steps.txt`）：

```markdown
CRITICAL - MAXIMUM ITERATIONS REACHED

The maximum number of iterations (8) for this investigation has been reached.
Tool calls are disabled. You MUST respond with text only.

Required content:
1. Statement that max iterations have been reached
2. Summary of what has been investigated (with evidence references)
3. Current best judgment (A/B/C) and reasoning
4. List of what you could NOT verify (remaining gaps)
5. Recommendation for what should be investigated next

DO NOT make any tool calls. Text only.
```

**为什么不 hard break**（粗暴结束）：
- 用户拿到"尽力而为的答案 + 明确的未完成项" > 拿到"什么都没"
- 强制总结把已知 evidence 沉淀下来
- 比"无声失败"好得多

**未来调整**：
- 跑 30-50 个真实样本后重新评估
- 如果 agent 经常"8 轮卡在有价值的工作上" → 改默认到 12
- 如果 compaction 验证稳定 → 考虑 12 或 15

### Q5. evidence relevance：谁评？✅ **已决定 → LLM 自评**

**决定**（老板 2026-06-05）：**LLM 自评**

老板原话：
> "opencode 应该没有这个相关能力，我建议由 llm 给吧，工具无法做到语义或者全局上的评价"

**为什么是 LLM 自评**：
- Relevance 是**语义级**评价（"这条证据对判断 A/B/C 有多少帮助"）
- **只有 LLM 能做全局语义评估**
- 工具只能做局部指标（关键词密度、字符长度等），不能跨工具 / 跨证据评估
- OpenCode **没有**这个概念（grep 确认），我们独立设计

**OpenCode 调研结果**（2026-06-05）：
- ❌ OpenCode 没有 evidence relevance 概念
- 只有 Copilot SDK 的 file-search 有个 `score`，那是 SDK 内部用的，跟 agent 无关

**实现方式**：

LLM 在每次 tool_call 后返回的响应里**多带一个字段**：

```json
{
  "action": "tool_call",
  "tool_name": "find_class",
  "tool_args": {"class_name": "UserService"},
  "evidence_evaluation": {
    "relevance": 0.85,
    "importance": "critical",
    "reason": "this is the class that threw the NPE"
  }
}
```

**字段含义**：
- `relevance` (0-1) —— 这条证据对当前判断 A/B/C 的帮助度
- `importance` (critical / supporting / background) —— 这条证据在整体证据池中的位置
- `reason` —— 一句话解释为什么给这个分

**存储**：

```python
@dataclass
class Evidence:
    id: str
    source: str
    location: str
    content: str
    relevance: float       # LLM 自评
    importance: str        # LLM 自评
    discovered_at_iteration: int
```

**用量**：
- 每个 tool_call 之后 LLM 多 1 个字段（约 50-100 token）
- 8 轮 = 800 token 额外成本（**可接受**）

**使用场景**：
- 喂 LLM 时按 `relevance` 降序排序（高分优先进入 prompt）
- Compaction 时按 `importance` 决定保留（critical 不压）
- 截取时按 `relevance` 选 top-N

### Q6. Session 持久化 ✅ **已决定 → SQLite + 每轮存 + 命令 resume**

**决定**（老板 2026-06-05）："都按你推荐来"

#### Q6a 存储后端：SQLite（标准库 `sqlite3`）

**为什么 SQLite**（之前我推荐 JSON 错了，修正）：
- Python 标准库自带 `sqlite3`，**0 依赖**
- 单文件 `~/.local/share/microtrace/state.db`
- 自动创建表结构
- 事务安全（崩溃不损坏）
- **用户感知不到"数据库"**——跟 Photos.app / Notes.app 一样

**OpenCode 用 SQLite 是真的最简方案**，不是"规模大了才上"。

#### Q6b 保存时机：每轮 iteration 存

```python
# Loop 主逻辑
while ctx.iteration <= ctx.max_iterations:
    # 1. LLM call + tool execution + judgment update
    # 2. 一轮结束，存一次
    save_context_to_sqlite(ctx, db)
```

**保存粒度对比**：

| 方案 | I/O 次数/session | 数据丢失风险 |
|---|---|---|
| 每个 event 存（OpenCode 全量）| 30-50 | 几乎 0 |
| **每轮 iteration 存（**我推荐**）** | **8** | **最多 1 轮** |
| 只在 CONCLUDE 存 | 1 | 中途崩 = 全部白费 |

**为什么是 iteration 不是 event**：
- 我们 agent 简单（4 工具，3 态），8 轮 8 次 I/O 足够
- 不像 OpenCode 一次对话几十个 event
- 8 毫秒级 I/O，可接受
- 跟 OpenCode "不丢有价值工作" 的精神一致

#### Q6c Resume 机制：`microtrace resume <session_id>`

```bash
$ microtrace
> New session started
> Session ID: 2026-06-05-163000-npe-userservice

$ microtrace sessions
ID                                       Status        Updated
2026-06-05-163000-npe-userservice        in_progress   5min ago
2026-06-05-100230-bug-validation         completed     3h ago

$ microtrace resume 2026-06-05-163000-npe-userservice
> Resuming session 2026-06-05-163000-npe-userservice
> Loaded context: iter 3/8, 5 evidence, judgment=B (0.75)
> [REPL starts where you left off]
```

**session id 怎么取**：
- 启动时 REPL 显示
- `microtrace sessions` 列表查
- **不自动恢复最近一个**（避免混淆多事故）

**Abandoned 状态**：
- ctrl+c 时如果没完成 → 标 `abandoned`
- abandoned 也能 `resume`（不是"坏的"）
- Phase 1+ 可以加自动清理

#### Q6d Schema：整个 Context 一个 JSON 列

```sql
CREATE TABLE sessions (
  id TEXT PRIMARY KEY,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  status TEXT NOT NULL,  -- 'in_progress' / 'completed' / 'abandoned'
  title TEXT,
  context_json TEXT NOT NULL  -- 整个 Context 序列化
);

CREATE INDEX idx_sessions_status ON sessions(status);
CREATE INDEX idx_sessions_updated ON sessions(updated_at DESC);
```

**为什么不拆 events / judgments / evidence 表**：
- Phase 0 不需要 SQL 查询"所有 B 类 judgment"
- JSON 一次读写 = 一次 SQL 操作
- Schema 简单 = 不容易出错

**未来要拆**（学 OpenCode）：
- 写迁移脚本"从 context_json 拆 events / judgments / evidence"
- OpenCode Migration 2 给完整范本

#### 关键 UX：**用户感知不到数据库**

启动时：
- 自动在 `~/.local/share/microtrace/state.db` 创建 DB
- 用户只看到 "New session started" 的提示
- 不需要任何"连接数据库"操作
- 失败是文件 I/O 错误，不是"DB 连不上"

管理命令：
- `microtrace sessions` —— 列表
- `microtrace resume <id>` —— 恢复
- `microtrace delete <id>` —— 删除（Phase 1+）

清理：直接 `rm` 那个 .db 文件（**真·零运维**）

### Q7. tool_call 并行？
**修订**：**默认并行**（OpenCode 模式）
- 选项 A：收到多个则并行（**当前倾向**）
- 选项 B：一次只一个
- 选项 C：LLM 显式声明

### Q8. **新增** Compaction 策略：滚动 vs 全量？
- A）滚动压缩（保留 critical + 摘要其他，**当前倾向**）
- B）全量 summarization
- C）只在 prompt 层截取，不在 context 层压缩

**当前倾向**：A（与 OpenCode 思路一致）

### Q9. **新增** Compaction 触发阈值：buffer = 20K？
- A）固定 20K（**当前倾向，与 OpenCode 一致**）
- B）动态调整（按模型 context 比例）
- C）可配置（用户改）

### Q10. **新增** Doom Loop 触发后行为？
- A）进入 ASK_USER 弹窗（**当前倾向**）
- B）自动终止本轮
- C）自动换工具重试

**当前倾向**：A（让用户决策，最安全）

### Q11. **新增** Retry 次数？
- A）固定 3 次（**当前倾向**）
- B）固定 5 次
- C）可配置

---

## 附录：OpenCode 技术栈一览

| 组件 | OpenCode | microtrace |
|---|---|---|
| Runtime | Bun (TypeScript) | Python 3.11 |
| LLM 客户端 | Vercel AI SDK | MiniMax SDK + 抽象接口 |
| 流式 LLM | Effect.Stream | asyncio + AsyncIterator |
| 事件溯源 | 25+ 事件类型 | 10 个左右（简化） |
| HTTP | Hono | FastAPI |
| TUI | Go + Bubble Tea | prompt_toolkit |
| 状态持久化 | SQLite | 轻量 JSON（Phase 0）|
| Snapshot | ZFS-like | ❌ 不做 |
