# Microtrace 设计方法论：从核心矛盾到机制推导

> 方法论来源：分析 Hermes Agent 时发现——它不是"加了一堆功能"，而是先定义核心矛盾（长期记忆自增长 vs 上下文劣化），然后所有模块围绕这个矛盾对齐。Microtrace 同理。

---

## 一、核心矛盾定义

> 我要让 LLM 做故障诊断（自由推理），但诊断结论必须严格可信、可追溯（约束推理）。LLM 天然会 hallucinate，而故障诊断零容忍幻觉——这两个目标天然冲突。

| | Hermes | Microtrace |
|---|--------|-----------|
| 能力目标 | Agent 越用越聪明（Skill/Memory 自增长） | LLM 自由推理发现根因 |
| 约束目标 | 上下文不能劣化（增长的东西进 prompt） | 结论必须可追溯、可信（证据闭环） |
| 天然冲突 | 增长的东西挤占上下文 → 劣化 | LLM 天然 hallucinate → 不可信 |
| **核心矛盾** | **自增长 vs 劣化** | **推理自由 vs 可追溯** |

一旦定义清楚核心矛盾，所有机制都是这个矛盾的**自然展开**——不是先有机制再找问题，是先有问题再推导机制。

---

## 二、矛盾展开 → 五机制推导

### 机制 1：证据锚定（Evidence Anchoring）

**要解决的矛盾面**：LLM 说"根因是数据库连接池耗尽"——你怎么知道这不是编的？

**推导逻辑**：LLM 必须输出断言，断言必须可信。唯一办法是**每条断言强制携带证据指针**——不是建议，是架构约束。

**设计方案**：

```
❌ LLM 自由文本输出：
   "数据库连接池耗尽导致超时"

✅ 结构化诊断输出：
   DiagnosisClaim {
     assertion: "数据库连接池耗尽导致超时"
     evidence: [
       {source: "slow_query.log:234", content: "连接等待时间 5.2s"},
       {source: "connection_pool_metrics:89", content: "活跃连接=100/100"},
     ]
     hop_count: 2                              // 症状→直接原因→根因
     confidence: "Certain"
   }
```

**架构落地**：Agent 的 `diagnose` 工具返回的不是自由文本，是 `DiagnosisClaim` 结构体。不填 `evidence` 字段 → 验证失败 → 不允许输出。

**对标 Hermes**：Memory 硬上限 2200 chars——不是建议 Agent "少写点"，是硬截断。

---

### 机制 2：逐跳验证门控（Hop-gated Verification）

**要解决的矛盾面**：3-hop 推理链，每跳 90% 准确率 → 最终准确率 72.9%。LLM 容易跳步——看到症状直接猜根因。

**推导逻辑**：推理链越长，累积误差越大。必须**强制分步、逐跳验证**——每跳通过证据门才允许进入下一跳。

**设计方案**：

```
症状 "订单服务 P99 延迟 5s"
    │
    ▼ Hop 1：定位异常组件
    │ 工具查：哪个微服务延迟最高？
    │ 发现：inventory-service P99=4.8s，其他 <200ms
    │ Gate：✓ 有 APM 指标，偏差 24x，证据充分 → 进入 Hop 2
    │
    ▼ Hop 2：定位异常指标
    │ 工具查：inventory-service 的 CPU/Mem/DB/GC？
    │ 发现：DB 查询耗时占比 90%，CPU/GC 正常
    │ Gate：✓ 有慢查询日志，锁定 DB 层 → 进入 Hop 3
    │
    ▼ Hop 3：定位根因
      工具查：具体慢 SQL + 执行计划？
      发现：SELECT * FROM inventory WHERE ... 缺索引，全表扫描 200 万行
      Gate：✓ EXPLAIN 计划确认，索引建议已生成

→ 结论输出：3-hop 完整链路，每跳独立可验证
```

**Gate 的判断标准**（不是 LLM 自己判断）：

| 条件 | 通过 | 不通过 |
|------|-----|--------|
| 证据来自工具返回？ | ✓ | ✗（LLM 自述不算） |
| 证据可定位到 source？ | ✓（文件+行号） | ✗（"日志显示…"） |
| 证据和断言有因果关系？ | ✓ | ✗（相关≠因果） |

**架构落地**：Loop 中插入 Gate 检查步骤——不是 prompt 里说"请仔细验证"，是**代码层在 Hop 之间检查**。

**对标 Hermes**：Preflight 压缩——每轮前估算 token 数，超了先压缩再调 API，不让 API 碰到超限。

---

### 机制 3：鉴别诊断（Differential Diagnosis）

**要解决的矛盾面**：一个症状可能对应多个候选根因。LLM 容易锚定第一个想到的，忽略替代假设。

**推导逻辑**：医学诊断的核心方法论——**先列所有可能，再逐一排除**。不是让 LLM "考虑一下其他可能"（软约束），而是**架构上先生成候选集、再逐一用工具验证排除**（硬约束）。

**设计方案**：

```
症状：P99 延迟飙升
    │
    ├─ Hypothesis A：DB 连接池耗尽
    │     证据：查连接池 metrics → 活跃 45/100，正常
    │     结果：排除 ✗（证据矛盾）
    │
    ├─ Hypothesis B：慢查询
    │     证据：查 slow_query.log → 命中 3 条 >1s 的查询
    │     结果：保留 ✓（证据支持）
    │
    └─ Hypothesis C：GC 停顿
          证据：查 GC 日志 → 近 30 分钟无 Full GC
          结果：排除 ✗（证据矛盾）

→ 最终诊断：慢查询（排除 A, C；确认 B）
```

**架构落地**：

```
DiagnosisState {
  hypotheses: [
    {id: "A", claim: "DB 连接池耗尽", status: "ruled-out", reason: "连接数正常"},
    {id: "B", claim: "慢查询", status: "confirmed", evidence: [...]},
    {id: "C", claim: "GC 停顿", status: "ruled-out", reason: "无 Full GC"},
  ]
}
```

**对标 Hermes**：Skill 的 active/stale/archived 三级流转——每个 skill 有明确状态，不是模糊的"可能有用"。

---

### 机制 4：置信度分层（Confidence Tier）

**要解决的矛盾面**：LLM 说"肯定是 X 的问题"——但它没有"不确定"这个概念，天然过度自信。

**推导逻辑**：不是每个诊断结论都敢自动执行修复。必须**分层——不同置信度对应不同行动权限**。

**设计方案**：

| Tier | 条件 | 行动权限 |
|------|------|---------|
| **Certain** | 3-hop 全部有工具证据闭环，所有替代假设已排除 | ✅ 可自动执行修复 |
| **Likely** | 有工具证据但存在未排除的替代假设 | ⚠️ 需人工确认后执行 |
| **Suspected** | 基于经验模式匹配，缺乏硬证据 | 👁️ 仅展示，不可操作 |
| **Ruled-out** | 已验证排除 | ❌ 不再考虑 |

Tier 不是 LLM 自己定的——是系统根据以下条件计算：

```
Certain  = hop_count >= 2 AND all_hops_have_evidence AND alternatives_ruled_out >= 1
Likely   = hop_count >= 1 AND has_hard_evidence AND alternatives_ruled_out == 0
Suspected = has_pattern_match AND !has_hard_evidence
```

**架构落地**：每个 `DiagnosisClaim.confidence` 字段对应不同的 UI 展示颜色和操作权限。Certain → 绿色 + 可操作按钮；Suspected → 灰色 + 仅展示。

**对标 Hermes**：Skill state 对应不同的可见性——active 进 system prompt，stale 不可见但可复活，archived 仅 curator 可见。**状态驱动行为**。

---

### 机制 5：矛盾强制回溯（Contradiction-triggered Backtrack）

**要解决的矛盾面**：排查到一半，新证据推翻旧假设。LLM 容易"坚持"已有结论、选择性忽略矛盾证据（confirmation bias）。

**推导逻辑**：不能让 LLM 自己决定"要不要重新考虑"——必须**系统级检测矛盾，强制回滚**。

**设计方案**：

```
Turn 3: Hypothesis B（慢查询）← 当前追踪的线索
Turn 4: 工具返回：slow_query.log 近 30 分钟无记录 ← 矛盾！

系统自动检测：
  ① 当前假设 B 依赖 "slow_query.log 有记录"
  ② 工具返回 "slow_query.log 为空"
  ③ 判定：EVIDENCE_CONTRADICTION

触发自动回溯：
  ① Hypothesis B → Ruled-out（理由：slow_query.log 为空）
  ② 回到 Hop 1，重新展开替代假设
  ③ 如果有其他 candidate（A: 连接池, C: GC），展开第一个未排除的
  ④ 如果无 candidate，回到 Hop 1 重新分析症状
```

**矛盾检测规则**（代码层，不是 prompt）：

```
规则 1：当前 hypothesis 引用的 evidence source
        vs
        最新工具返回的同 source 内容
        → 不一致 → EVIDENCE_STALE

规则 2：当前 hypothesis 的断言
        vs
        最新工具返回的结论
        → 直接否定 → EVIDENCE_CONTRADICTION

规则 3：已排除 hypothesis 的排除理由
        vs
        最新工具返回的新数据
        → 排除理由不再成立 → HYPOTHESIS_REVIVE
```

**架构落地**：Post-hop Hook 在每轮工具返回后检查上述规则，触发矛盾 → 自动标记 + 回滚 + 重新展开。

**对标 Hermes**：Curator 的 `apply_automatic_transitions`——不是让 LLM 决定什么时候降级 skill，是代码按时间自动流转。**系统级规则 > LLM 判断**。

---

### 机制 6：诊断模式进化（Diagnosis Pattern Learning）

**要解决的矛盾面**：一个 VNFM 维护工程师在解决第 51 个"订单服务 NPE"时不应该从零开始。但当前 Agent 每次都是冷启动——即使上周刚解决过一个完全相同的故障，这周还是从头排查。LLM 本身没有跨 session 的学习能力。

**推导逻辑**：Hermes 解决的是同一个问题——Agent 完成复杂任务后，nudge 机制提示"你该考虑创建一个 skill 了"，然后 Agent 自己把这次经验写成 SKILL.md。下次遇到类似任务，`skill_view(name)` 直接加载，不再重学。

Microtrace 的场景完全同构：

```
Hermes:  完成任务 → nudge → 创建 SKILL.md → curator 维护生命周期 → 下次 skill_view 加载
Microtrace: 完成诊断 → 提取 → 创建 Pattern → curator 验证 → 下次症状匹配 → 注入 hint
```

**设计方案**：

```
第 1 次：订单服务 NPE → 完整 3-hop 排查 → 耗时 8 轮 → 结论：UserService.java:234 缺空值校验
    │
    ▼ CONCLUDE 后自动提取
    │
DiagnosisPattern {
  symptom_signature: "OrderService NPE + userId=null + UserService.validate",
  diagnostic_path: [
    "parse_stack_trace → UserService.java:234",
    "find_class → UserService",
    "read_file → 发现 userId 参数未校验"
  ],
  root_cause_template: "UserService.java:234 对 userId 参数缺少空值校验",
  category: "A",  # 本产品 Bug
}

第 2 次（两周后）：订单服务又报 NPE
    │
    ▼ INTAKE 后、INVESTIGATE 前：症状匹配
    │
pattern_match("OrderService NPE + userId=null") → 命中！相似度 92%
    │
    ▼ 注入 Pattern Hint（不是直接跳过排查，是给 LLM 一个 head start）
    │
    "⚠️ 历史相似案例：2026-06-12 订单服务 NPE，根因是 UserService.java:234 缺空值校验。
     本次症状与之高度相似。你可以聚焦在 UserService 的空值校验路径，
     但不要跳过正常排查——可能有新变化。"
    │
    ▼ LLM 带着这个 hint 开始 INVESTIGATE
    │  直接读 UserService.java → 确认问题 → 2 轮完成（vs 上次 8 轮）
```

**数据模型**：

```python
@dataclass
class DiagnosisPattern:
    id: str
    symptom_signature: str          # 症状特征（用于匹配）
    diagnostic_path: list[str]      # 诊断路径（hop 1→2→3）
    root_cause_template: str        # 根因模板
    category: str                   # A/B/C
    success_count: int = 1          # 成功复用次数
    last_used: datetime             # 最近一次使用
    confidence: float = 0.5         # 模式本身的可靠度
    created_from_session: str       # 来源 session id
    # Hermes 风格的生命周期
    state: Literal["active", "stale", "archived"] = "active"

@dataclass  
class PatternStore:
    patterns: list[DiagnosisPattern]
    
    def match(self, symptom: str) -> list[tuple[DiagnosisPattern, float]]:
        """返回匹配的模式及相似度"""
        # 用 LLM embedding 或简单关键词 Jaccard 做症状相似度
        ...
    
    def extract(self, ctx: Context) -> DiagnosisPattern | None:
        """从成功诊断中提取模式（CONCLUDE 后触发）"""
        ...
```

**生命周期（对标 Hermes skill 三层机制）**：

```
第一层：自动提取（时间驱动）
  CONCLUDE 后 → tier=Certain 且 hop_count≥2 → 自动调 LLM 提取 pattern
  → 存为 candidate 状态 → curator 验证后才能变 active

第二层：LLM 验证 + 合并（curator 驱动）
  后台 curator 定期检查 candidate patterns
  → 与已有 patterns 对比 → 新类型？创建。已有同类型？合并并更新。
  → candidate → active

第三层：使用中进化（使用驱动）
  每次命中 → success_count++ → confidence 提升
  每次命中但最终结论不匹配 → 添加 caveat → confidence 下降
  long time unused → active → stale → archived（和 Hermes 完全一样）
```

**INTAKE 后的匹配点**：

```python
async def _intake(ctx, initial_input, llm, tools):
    # ... 解析 problem ...
    
    # 新增：症状匹配
    patterns = ctx.pattern_store.match(initial_input)
    if patterns and patterns[0][1] > 0.7:  # 相似度 > 70%
        ctx.matched_patterns = patterns[:3]
        ctx.append_reasoning(
            f"[PATTERN MATCH] 命中 {len(patterns)} 个历史模式，"
            f"最高相似度 {patterns[0][1]:.0%}"
        )
```

Pattern hint 注入到 prompt 的 problem section 之后，告诉 LLM 这不是答案——是 head start：

```markdown
## 历史相似案例（仅供参考，不是结论）

⚠️ 以下案例的症状与本次高度相似。你可以优先排查相似路径，
但必须基于当前工具返回的证据做判断——案例可能是过时的。

1. [92%] 2026-06-12: OrderService NPE → UserService.java:234 缺空值校验 (A类)
   诊断路径: parse_stack_trace → find_class → read_file → 确认
```

**对标 Hermes 的完整对照**：

| Hermes | Microtrace | 共性 |
|--------|-----------|------|
| Agent 完成任务 | Agent 完成诊断 | 事件触发 |
| nudge→创建 SKILL.md | CONCLUDE→提取 Pattern | 自动创建 |
| curator 后台评审 | curator 验证+合并 | 离线质控 |
| active/stale/archived | 完全相同的三态流转 | 生命周期管理 |
| skill_view 按需加载 | pattern_match→注入 hint | 不自动进 prompt |
| LLM 评审改进 | 复用中 confidence 更新 | 使用中进化 |
| pinned skill 跳过流转 | pinned pattern 跳过流转 | 用户可控 |

**关键设计决策**：Pattern hint 注入的是**方向建议**，不是**结论**。它告诉 LLM "上次这种情况是 UserService 的问题"，但 LLM 仍然要调工具验证——因为这次可能是新原因导致的相同症状。这和 Hermes 的 skill 设计一致——skill 是"如何做"，不是"答案是什么"。

---

### 机制 6 的补丁：反过拟合防护（Anti-Pattern-Overfitting）

**要解决的问题**：症状相似但根因完全不同——这是模式系统最经典的失败模式。例如：

```
第 1 次：OrderService NPE, userId=null → 根因：UserService 缺空值校验
第 2 次：OrderService NPE, userId=null → 症状完全一样！
    │
    模式匹配说 "92% 相似，上次是 UserService"
    LLM 带着 hint 去查 UserService → 校验已加上了 → 没有问题
    │
    ⚠️ 如果 LLM 过度信任模式 → 继续围绕 UserService 找 → 浪费 3-5 轮
    ⚠️ 实际上根因是：上游 Nginx 配置错误，userId 根本没传过来
```

**推导逻辑**：这和机制 5（矛盾回溯）是同构的问题——只不过矛盾的对象从"当前假设 vs 新证据"变成了"历史模式 vs 当前证据"。同理，不能靠 prompt 说"请不要太相信历史案例"——必须靠代码层硬约束。

**防护设计——三层退避**：

```
Layer 1：Pattern Hint 本身就标注了边界

Prompt 里写死的：
  "⚠️ 历史相似案例仅供参考。如果 2 轮内未能在建议路径找到证据，立即放弃该方向，
   不要继续围绕模式线索展开——症状可能是不同原因导致的相同表现。"

Layer 2：代码层检测 Pattern Staleness（硬约束）

  在 agent_iteration() 的 post-tool 阶段新增规则 4：

  规则 4：PATTERN_MISLEAD
    有活跃 pattern hint + pattern 建议的工具已调 >=2 次
    + 返回的证据均不支持 pattern 的 root_cause_template
    → 自动撤销 pattern hint + 注入 forced redirection prompt

Layer 3：Pattern 自身降级（跨 session）

  pattern 导致误诊 → pattern.confidence *= 0.5（折半）
  连续 2 次误诊 → pattern.active → stale
  curator 后续评估是否 archived
```

**具体实现**：

```python
# agent/contradiction.py

def check_pattern_staleness(ctx: Context) -> str | None:
    """检测当前匹配的模式是否在误导诊断"""
    if not ctx.matched_patterns:
        return None
    
    pattern = ctx.matched_patterns[0][0]
    
    # 条件 1：pattern hint 已经活跃了 >= 2 轮
    pattern_rounds = ctx.iteration - ctx.pattern_injected_at_iteration
    if pattern_rounds < 2:
        return None
    
    # 条件 2：pattern 建议的路径上的工具已调用
    suggested_tools = _extract_tools_from_path(pattern.diagnostic_path)
    tools_called_for_pattern = [
        tc for tc in ctx.tool_history[-pattern_rounds:]
        if tc.name in suggested_tools
    ]
    if len(tools_called_for_pattern) < 2:
        return None
    
    # 条件 3：但这些工具返回的 evidence 都不支持 pattern 的根因结论
    pattern_evidence = [
        ev for ev in ctx.evidence[-pattern_rounds:]
        if ev.tool_name in suggested_tools
    ]
    if _any_evidence_supports(pattern_evidence, pattern.root_cause_template):
        return None  # 有证据支持，模式有效
    
    # 三个条件都满足 → 模式在误导
    return "PATTERN_MISLEAD"

# Loop 中调用
async def agent_iteration(ctx, llm, tools):
    ...
    for tc in tool_calls:
        ev = _result_to_evidence(tc)
        
        # 机制 5：矛盾检测
        contradiction = check_evidence_contradiction(ctx, ev)
        if contradiction:
            _handle_contradiction(ctx, contradiction)
        
        # 机制 6 补丁：模式误导检测
        staleness = check_pattern_staleness(ctx)
        if staleness == "PATTERN_MISLEAD":
            # 硬撤销 pattern hint
            ctx.active_pattern_hint = None
            ctx.append_reasoning(
                f"[PATTERN DROP] 历史模式 '{pattern.root_cause_template[:50]}' "
                f"未能在当前证据中获得支持，已自动撤销。"
            )
            # 注入重定向提示
            ctx.pending_redirection = (
                "⚠️ 之前匹配的历史诊断模式已被排除——当前症状可能源于不同根因。"
                "请回到 Hop 1，从症状本身重新分析，不要受历史模式的干扰。"
            )
            # 跨 session：降级 pattern
            pattern.confidence *= 0.5
            if pattern.confidence < 0.2:
                pattern.state = "stale"
```

**关键设计**：

| 维度 | 软约束（prompt） | 硬约束（代码） |
|------|-----------------|--------------|
| 怎么说 | "不要太相信历史案例" | 2 轮内 pattern 无证据支持 → 强制撤销 |
| 效果 | LLM 可能仍然坚持 | pattern hint 被代码层删除，无法再读取 |
| 跨 session | 无 | pattern.confidence 折半，2 次误诊 → stale |
| 对标 Hermes | — | Curator 自动降级（完全一致） |

**面试口述版**：

> 诊断模式是一把双刃剑——它能大幅缩短重复问题的排查时间，但也可能让 Agent 过度依赖历史经验，在症状相同但根因不同的情况下走弯路。我们的防护分三层：第一层 prompt 里写死边界——"2 轮内找不到支持证据就放弃该方向"；第二层代码层在每次工具返回后自动检测——如果 pattern 建议的工具调了 2 次以上但返回的 evidence 都不支持 pattern 的根因结论，硬撤销 pattern hint，注入重定向提示；第三层跨 session——导致误诊的 pattern 自动降级，confidence 折半，2 次连续误诊直接转为 stale，curator 后续决定是否归档。
>
> 这和 Hermes 的 curator 自动降级机制完全同构——不是让 LLM 判断"这个 pattern 好不好用"，是代码层根据客观信号自动流转状态。

---

## 三、模块对齐：机制如何渗透进架构

有了五个机制后，关键是**它们不是独立模块，而是渗透进 Agent 的每个组件**——就像 Hermes 的"防劣化"不是单独一个模块，而是 Loop、System Prompt、Memory、Tool、Post-turn 全都做了适配。

| 模块 | 为"推理自由 vs 可追溯"做了什么适配 | 参与的机制 |
|------|----------------------------------|-----------|
| **System Prompt** | 固定诊断框架（Diagnosis Protocol）+ 动态证据注入 | 机制 1（锚定框架）、机制 4（Tier 定义） |
| **Agent Loop** | Pre-hop Gate（每跳前验证上一跳证据）+ 迭代预算 | 机制 2（逐跳门控）、机制 5（矛盾检测） |
| **Tool System** | 工具返回不止 data，还带 source/metadata/timestamp | 机制 1（证据锚定）、机制 5（矛盾检测源） |
| **State Management** | 持久化诊断状态（当前 hop、候选假设、已排除、置信度） | 机制 3（假设管理）、机制 4（Tier 计算） |
| **Post-hop Hook** | 矛盾检测 + 置信度重算 + 证据完整性校验 + **Pattern 误导检测** | 机制 2（Gate 判断）、机制 5（回溯触发）、机制 6（模式撤销） |
| **Permission** | 置信度 → 行动权限映射（Certain 自动/Likely 确认/Suspected 只读） | 机制 4（分层行动） |
| **Pattern Store** | 跨 session 诊断模式存储 + 匹配 + 进化 + 过拟合防护 | 机制 6（模式学习 + 反过拟合） |

---

## 四、面试叙事主线

当面试官问"你设计 Microtrace 的思路是什么"，用这个结构回答：

### 30 秒版

> "我先定义了核心矛盾——LLM 推理的自由度 vs 故障诊断对可追溯性的硬要求。这和 Hermes 面对的矛盾同构——Hermes 要 Agent 自增长但不能让上下文劣化，我要 LLM 自由推理但不能让结论不可信。然后我把矛盾拆成五个侧面，每个侧面用一个硬约束机制兜底：证据锚定、逐跳验证、鉴别诊断、置信度分层、矛盾回溯。这些机制不是独立模块——它们渗透进 Loop、工具系统、状态管理的每个环节。"

### 展开版（5-8 分钟）

1. **矛盾定义**（1 分钟）：对比 Hermes，讲清楚 Microtrace 的约束是什么
2. **五机制推导**（3 分钟）：每个机制——什么问题、什么推导、什么方案
3. **模块对齐**（2 分钟）：机制如何渗透进架构，不是"加功能"是"重新设计"
4. **一句话总结**（30 秒）："推理自由是能力，可追溯是约束，五机制是桥"

### 为什么这个叙事比"我用了 XX 技术"好

| 普通回答 | 这个方法论驱动的回答 |
|---------|-------------------|
| "我做了个故障诊断 Agent，用了 RAG 和 tool calling" | "我从核心矛盾出发，推导出五个必要机制，然后让它们渗透进架构" |
| 听起来像"调了 API" | 听起来像"设计了系统" |
| 面试官追问"为什么用这个"时会卡住 | 每个设计决策都有推导链条，可以无限追问下去 |
| 和其他项目没有关联 | 和 Hermes/OpenClaw 等主流产品形成对比，展现系统性思考 |

---

## 五、与其他 Agent 产品的机制对比

| 机制维度 | OpenCode | OpenClaw | Hermes | **Microtrace** |
|---------|----------|----------|--------|---------------|
| 核心矛盾 | 短任务效率 | 多频道泛化 | 长期记忆自增长 vs 劣化 | LLM 推理自由 vs 可追溯 |
| 验证方式 | 无特殊约束 | 无特殊约束 | Curator 离线评审 | **逐跳 Gate + 证据锚定** |
| 假设管理 | 无 | 无 | 无（skill 是技能不是假设） | **鉴别诊断：生成-排除-确认** |
| 置信度 | LLM 自主判断 | LLM 自主判断 | 无（skill 不分置信度） | **分层 + 权限映射** |
| 错误恢复 | retry | retry | retry | **矛盾回溯：检测-标记-回滚** |
| 状态机 | 无 | 无 | Skill 3 状态 | **Hypothesis 4 状态 + Confidence 4 Tier** |
| **持续学习** | 无 | 无 | **Skill 自创建+进化** | **Pattern 提取+匹配+反过拟合** |

---

## 六、落地方案：五机制分别改什么

> 当前代码是 OpenCode 通用机制（Loop/Doom Loop/Compaction/Event Sourcing）+ 问题定位专用结构（Evidence/Judgment）。五机制不是"加功能"，是**重排架构重心**——通用层从 80% 缩到 40%，专用层从 20% 扩到 60%。

### 现状诊断

```
Microtrace 当前架构：

  OpenCode 通用层（80%）              问题定位专用层（20%）
  ┌─────────────────────────┐    ┌─────────────────┐
  │ Loop（双层 while）       │    │ Evidence 结构     │
  │ State Machine（5态）     │    │ Judgment A/B/C   │
  │ Doom Loop（3次精确匹配） │    │ judgment_history │
  │ Compaction（PRUNE+SUMMARY）│  │ 关键行提取 regex  │
  │ Event Sourcing          │    │                  │
  │ Tool 4态子状态机         │    │                  │
  │ Retry Policy            │    │                  │
  └─────────────────────────┘    └─────────────────┘
```

**核心问题**：Evidence 只是"存了"，没有"用起来"。没有机制保证 LLM 的结论必须引用 evidence。没有机制在证据不足时拦住 LLM。没有机制在新证据推翻旧假设时强制回滚。

---

### 机制 1：证据锚定 → 改 CONCLUDE 态 + LLM 输出解析

**当前**：LLM 输出自由文本当结论，evidence 独立存储，两者无硬链接。

**要改**：

```python
# 当前：LLM 输出文本就是结论
ctx.final_output = "根因是数据库连接池耗尽"

# 要改成：结构化 claim，不带 evidence 引用就拒绝
@dataclass
class DiagnosisClaim:
    assertion: str
    evidence_refs: list[str]      # 必须引用 ctx.evidence[].id
    hop_count: int
    confidence_tier: str

# CONCLUDE 态新增验证
def validate_claim(claim: DiagnosisClaim, ctx: Context) -> bool:
    for ref in claim.evidence_refs:
        if not any(ev.id == ref for ev in ctx.evidence):
            return False           # 引用不存在的证据 → 拒绝
    if not claim.evidence_refs:
        return False               # 没有证据 → 拒绝
    return True
```

**改动范围**：`loop.py` 的 `_conclude()`、`context/models.py` 加 `DiagnosisClaim`、prompt 要求 LLM 输出结构化 claim。

---

### 机制 2：逐跳验证门控 → 改 Loop 结构

**当前**：Loop 每次 iteration 是平的——调 LLM → 执行工具 → 更新 judgment → 继续。没有"跳"的概念。

**要改**：在 `agent_iteration()` 后插入 Gate：

```python
# 当前
while True:
    await agent_iteration(ctx, llm, tools)
    if ctx.state == ...

# 要改成
while True:
    await agent_iteration(ctx, llm, tools)

    # 新增：Gate 检查（不是 LLM 判断，是规则引擎）
    gate_result = check_hop_gate(ctx)
    if gate_result == "PASS":
        ctx.current_hop += 1
        continue
    elif gate_result == "INSUFFICIENT_EVIDENCE":
        ctx.append_reasoning("[GATE] 当前 hop 证据不足，需继续收集")
        continue
    elif gate_result == "MAX_HOP_REACHED":
        await transition(ctx, State.CONCLUDE, reason="max hop")
        break
```

`check_hop_gate()` 规则引擎：

```python
def check_hop_gate(ctx: Context) -> str:
    current_hop_evidence = [e for e in ctx.evidence
                           if e.discovered_at_iteration >= ctx.last_gate_iteration]

    # 规则 1：至少有一条 tool 返回的 evidence
    if not any(e.source in ("code", "log", "stack") for e in current_hop_evidence):
        return "INSUFFICIENT_EVIDENCE"

    # 规则 2：evidence relevance 不能都低于 0.3
    if all(e.relevance < 0.3 for e in current_hop_evidence):
        return "INSUFFICIENT_EVIDENCE"

    return "PASS"
```

**改动范围**：`loop.py` 的 `run_session()`、新增 `agent/hop_gate.py`。

---

### 机制 3：鉴别诊断 → 改 State 和 Judgment 模型

**当前**：`judgment` 是单例——LLM 每次更新一个当前判断，历史存 `judgment_history` 但 LLM 不看。

**要改**：变成"先生成候选集，再逐一验证排除"：

```python
# 删掉
# current_judgment: Judgment       ← 单例模型
# judgment_history: list[Judgment]

# 改为
@dataclass
class Hypothesis:
    id: str
    claim: str
    status: Literal["candidate", "investigating", "confirmed", "ruled-out"]
    evidence_for: list[str]         # evidence id
    evidence_against: list[str]
    ruled_out_reason: str | None

@dataclass
class HypothesisSet:
    candidates: list[Hypothesis]

class Context:
    hypotheses: HypothesisSet | None = None   # 替代 judgment
```

Loop 逻辑变为两阶段：

```
Phase 1（展开）: LLM 看到症状 → 生成候选假设 A, B, C
Phase 2（排除）: 对每个假设调工具验证 → 排除或确认
```

**改动范围**：`context/models.py`（删 Judgment、加 HypothesisSet）、`loop.py`（改 judgment_update 分支）、prompt（教 LLM 鉴别诊断流程）。

---

### 机制 4：置信度分层 → 改 Permission 层

**当前**：`confidence: float 0.0~1.0`，没有行动权限映射。

**要改**：

```python
# Tier 计算规则（规则引擎，不是 LLM）
def compute_confidence_tier(h: Hypothesis, ctx: Context) -> str:
    has_direct_evidence = len(h.evidence_for) >= 2
    alternatives_ruled_out = sum(
        1 for c in ctx.hypotheses.candidates if c.status == "ruled-out"
    ) >= 1
    all_from_tools = all(
        any(ev.id == ref for ev in ctx.evidence if ev.source != "user")
        for ref in h.evidence_for
    )

    if has_direct_evidence and alternatives_ruled_out and all_from_tools:
        return "Certain"     # → 可自动执行修复
    elif has_direct_evidence:
        return "Likely"      # → 需人工确认
    else:
        return "Suspected"   # → 仅展示，不可操作


# CONCLUDE 输出时应用
def _conclude(ctx: Context) -> str:
    h = ctx.hypotheses.get_confirmed()
    tier = compute_confidence_tier(h, ctx)

    # Certain → 绿色 + "建议自动修复"
    # Likely → 黄色 + "建议人工确认后修复"
    # Suspected → 灰色 + "仅做参考"
```

| Tier | 条件 | 行动权限 | UI 颜色 |
|------|------|---------|---------|
| **Certain** | 2+ tool 证据 + 1+ 替代假设已排除 | 可自动执行修复 | 绿色 |
| **Likely** | 有直接证据但替代假设未排除 | 需人工确认 | 黄色 |
| **Suspected** | 仅基于模式匹配，无硬证据 | 仅展示 | 灰色 |
| **Ruled-out** | 已验证排除 | 不再考虑 | 删除线 |

**改动范围**：新增 `agent/confidence.py`、改 `_conclude()` 输出格式。

---

### 机制 5：矛盾回溯 → 改 Post-tool Hook

**当前**：有 Doom Loop（检测重复调相同工具），但**没有**证据矛盾检测。

**要改**：工具执行后加矛盾检测规则：

```python
# 新增：post-tool 矛盾检测
def check_evidence_contradiction(ctx: Context, latest: Evidence) -> str | None:
    current = ctx.hypotheses.current_investigating
    if not current:
        return None

    for ref in current.evidence_for:
        ev = next((e for e in ctx.evidence if e.id == ref), None)
        if not ev:
            continue

        # 规则 1：同 source 数据变化 → EVIDENCE_STALE
        if ev.source == latest.source and ev.location == latest.location:
            if ev.content != latest.content:
                return "EVIDENCE_STALE"

        # 规则 2：新证据直接否定当前假设
        # 例如：假设"连接池耗尽"但新证据显示"活跃连接 45/100"
        if _contradicts(latest.content, current.claim):
            return "EVIDENCE_CONTRADICTION"

    return None

# Loop 中调用
async def agent_iteration(ctx, llm, tools):
    ...
    for tc in tool_calls:
        ev = _result_to_evidence(tc)
        contradiction = check_evidence_contradiction(ctx, ev)

        if contradiction == "EVIDENCE_CONTRADICTION":
            # 自动回溯
            current = ctx.hypotheses.current_investigating
            current.status = "ruled-out"
            current.ruled_out_reason = f"新证据矛盾: {ev.content[:100]}"
            ctx.append_reasoning(f"[BACKTRACK] 假设被推翻: {current.claim}")
            # 激活下一个 candidate
            _activate_next_hypothesis(ctx)
```

**矛盾检测规则表**：

| 规则 | 触发条件 | 行为 |
|------|---------|------|
| `EVIDENCE_STALE` | 同 source + 同 location 的数据发生变化 | 标记当前假设 evidence 为过期，重新验证 |
| `EVIDENCE_CONTRADICTION` | 新证据直接否定当前假设的 claim | 假设 → Ruled-out，自动激活下一个 candidate |
| `HYPOTHESIS_REVIVE` | 之前排除某假设的理由被新证据推翻 | 恢复假设到 candidate 状态 |

**改动范围**：新增 `agent/contradiction.py`、改 `agent_iteration()` 的 post-tool 阶段。

---

### 改动总览：重排架构重心

| 层面 | 当前占比 | 目标占比 | 关键变动 |
|------|---------|---------|---------|
| OpenCode 通用机制 | 80% | 30% | Loop/Compaction/Doom Loop 保留不扩展 |
| 问题定位专用机制 | 20% | 70% | 新增 hop_gate, hypotheses, contradiction, confidence, pattern_store |
| **Context 模型** | Evidence + Judgment | **HypothesisSet + DiagnosisClaim + ConfidenceTier + PatternStore** | 删单例 Judgment，换假设集合+模式库 |
| **Loop 结构** | 平的 iteration | **两阶段（展开→排除）+ 跳间 Gate + Pattern 匹配** | run_session 加 hop 跟踪，INTAKE 后加模式匹配 |
| **CONCLUDE 态** | 格式化 final_output | **结构化 claim + evidence 引用验证 + Pattern 提取** | 输出变结构化对象，成功后自动提取模式 |
| **Post-tool Hook** | 仅追加 evidence | **矛盾检测 + 自动回溯 + Pattern 误导检测** | 每次工具返回后三重校验 |
| **跨 Session** | SQLite 存 context | **+ Pattern Store（跨 session 模式库）** | 新增模式存储、匹配、进化基础设施 |

### 机制 6 落地方案：诊断模式进化

**当前**：每次诊断是独立的 SQLite session，跨 session 之间没有知识传递。

**要改**：

```python
# 新增：Pattern Store（跨 session 的模式库）
class PatternStore:
    """管理诊断模式的提取、匹配、进化、降级"""
    
    def extract(self, ctx: Context) -> DiagnosisPattern | None:
        """CONCLUDE 后自动提取。条件：tier=Certain + hop≥2"""
        if ctx.confidence_tier != "Certain" or ctx.current_hop < 2:
            return None
        # 调 LLM 从 diagnosis claim + evidence 中提取可复用的模式
        ...
    
    def match(self, symptom: str) -> list[tuple[DiagnosisPattern, float]]:
        """INTAKE 后症状匹配。返回相似度排序的候选模式"""
        ...
    
    def on_mislead(self, pattern: DiagnosisPattern) -> None:
        """模式导致误诊时降级"""
        pattern.confidence *= 0.5
        if pattern.confidence < 0.2:
            pattern.state = "stale"
    
    def on_successful_reuse(self, pattern: DiagnosisPattern) -> None:
        """模式被成功复用时强化"""
        pattern.success_count += 1
        pattern.confidence = min(pattern.confidence * 1.2, 1.0)
        pattern.last_used = datetime.now()

# INTAKE 后：症状匹配
async def _intake(ctx, initial_input, llm, tools, pattern_store):
    # ... 解析 problem ...
    patterns = pattern_store.match(initial_input)
    if patterns and patterns[0][1] > 0.7:
        ctx.matched_patterns = patterns[:3]
        ctx.pattern_injected_at_iteration = 0  # INVESTIGATE 开始后记录

# agent_iteration 中：Pattern 误导检测（新增规则 4）
staleness = check_pattern_staleness(ctx)
if staleness == "PATTERN_MISLEAD":
    ctx.active_pattern_hint = None  # 硬撤销
    pattern_store.on_mislead(pattern)  # 跨 session 降级

# CONCLUDE 后：Pattern 提取
async def _conclude(ctx, llm, pattern_store):
    claim = _build_claim(ctx)
    validate_claim(claim, ctx)
    
    # 新增：提取模式
    if claim.confidence_tier == "Certain":
        pattern = pattern_store.extract(ctx)
        if pattern:
            pattern_store.add(pattern)
            ctx.append_reasoning(f"[PATTERN] 已提取诊断模式: {pattern.symptom_signature[:50]}")
```

**改动范围**：新增 `agent/pattern_store.py`（模式存储+匹配+进化）、`agent/pattern_guard.py`（反过拟合检测）、改 `loop.py` 的 `_intake()` 和 `_conclude()`。

### 最小落地路径

1. **先动模型**（`context/models.py`）：删 `Judgment`，加 `HypothesisSet` + `DiagnosisClaim` + `ConfidenceTier` + `DiagnosisPattern`。模型一定，上层自然跟着变
2. **再改 prompt**（`prompts/agent.md`）：让 LLM 输出 hypothesis 列表而非单个 judgment，教它鉴别诊断流程 + pattern hint 使用边界
3. **最后改 loop**（`agent/loop.py`）：加 hop gate + 矛盾检测 + pattern 匹配/误导检测，这是从"软约束"到"硬约束"的关键一步
4. **跨 session 基础设施**（`agent/pattern_store.py`）：模式库的存储和进化独立于 session，可渐进叠加

---

## 七、待实现

- [ ] `context/models.py`：删 Judgment 单例，加 HypothesisSet + DiagnosisClaim + ConfidenceTier + DiagnosisPattern
- [ ] `agent/loop.py`：run_session 加 hop 跟踪，agent_iteration 加 Gate 检查 + Pattern 匹配/撤销
- [ ] `agent/hop_gate.py`：逐跳验证规则引擎（新增）
- [ ] `agent/contradiction.py`：证据矛盾检测 + 自动回溯 + Pattern 误导检测（新增）
- [ ] `agent/confidence.py`：置信度 tier 计算规则 + 行动权限映射（新增）
- [ ] `agent/pattern_store.py`：模式提取、存储、匹配、进化、降级（新增）
- [ ] `prompts/agent.md`：教 LLM 鉴别诊断流程（两阶段：展开→排除）+ pattern hint 使用边界
- [ ] `context/models.py`：DiagnosisClaim 结构化输出 schema
- [ ] `loop.py`：`_conclude()` 输出结构化 claim + evidence 引用验证 + Pattern 自动提取

---

## 八、面试讲解材料

> 以下从"已实现"的角度编写。面试时根据对方给的时长选择 30 秒 / 2 分钟 / 详细版。

---

### 8.1 30 秒电梯演讲

> Microtrace 解决的核心矛盾是：**LLM 能自由推理，但故障诊断要求每条结论必须可追溯。** 这两个目标天然冲突——LLM 会 hallucinate，而故障诊断零容忍幻觉。
>
> 我借鉴了 Hermes Agent 的方法论——它定义"长期记忆自增长 vs 上下文劣化"为核心矛盾，然后所有模块围绕它对齐。我也是：从核心矛盾推导出五个硬约束机制——**证据锚定、逐跳验证、鉴别诊断、置信度分层、矛盾回溯**——每个机制都不是"加功能"，而是渗透进 Agent Loop、工具系统、上下文管理的架构约束。

---

### 8.2 两分钟概述

> **背景**：Microtrace 是给 VNFM 维护工程师用的 Java 多微服务故障诊断 Agent。目标用户是 7 级左右的 Java 工程师，他们收到周边部门的报错反馈，需要快速定位问题是本产品 Bug、下游报错、还是使用方法问题。
>
> **设计起点**：我做之前先研究了四个主流 Agent 产品的架构——OpenCode、OpenClaw、Pi-Agent、Hermes。发现一个规律：**最好的产品不是功能最多的，是核心矛盾定义最清晰的。** Hermes 最典型——它的核心矛盾是"Skill/Memory 自增长 vs 上下文劣化"，然后 Curator、Focus Mode、Context Compressor、Iteration Budget、Memory 硬上限——所有模块都是这个矛盾的展开。
>
> **Microtrace 的核心矛盾**：LLM 推理的自由度 vs 故障诊断对可追溯性的硬要求。LLM 天然会 hallucinate——编造不存在的日志行号、跳过推理步骤直接猜答案、锚定第一个想到的假设忽略其他可能。而故障诊断要求每条结论都能引用具体证据（代码行/日志片段），要求推理链完整可审计，要求排除了替代假设才能下结论。
>
> **五机制推导**：我把这个矛盾拆成六个侧面，推导出六个硬约束机制。第一，LLM 输出的断言怎么保证不是编的？→ 证据锚定。第二，多步推理链的累积误差怎么控制？→ 逐跳验证门控。第三，LLM 只追第一个假设怎么办？→ 鉴别诊断。第四，怎么防止过度自信导致误操作？→ 置信度分层。第五，新证据推翻旧结论时 LLM 不肯回头怎么办？→ 矛盾强制回溯。第六，第 51 个 NPE 能不能不复现 50 次完整排查？→ 诊断模式进化。
>
> **关键设计原则**：这五个机制都不是 prompt 里的软约束——"请仔细验证你的结论"——而是代码层的硬约束。Gate 不是 LLM 自己判断，是规则引擎判断。矛盾检测不是等 LLM 自己发现，是每次工具返回后系统自动执行。这和 Hermes 的做法一致——Curator 不是让 LLM 决定什么时候归档 skill，是代码按时间自动流转。

---

### 8.3 每个机制详细讲解

#### 机制 1：证据锚定（Evidence Anchoring）

**解决的问题**：LLM 说"根因是数据库连接池耗尽"——你怎么知道这不是编的？

**为什么不是 prompt 能解决的**：你可以在 system prompt 里写"请基于证据推理"，但这只是软约束。LLM 仍然可以生成看起来合理的断言，编造不存在的日志行号，或者引用它"记忆中的"而非实际工具返回的数据。故障诊断场景零容忍这种行为。

**我们的方案**：每条诊断断言必须是 `DiagnosisClaim` 结构体，强制携带 `evidence_refs` 字段——引用 `ctx.evidence[].id`。CONCLUDE 态有验证器——如果引用的 evidence id 不存在，或者根本没有引用，拒绝输出，要求 LLM 重新生成。

```python
# 不是自由文本
ctx.final_output = "根因是数据库连接池耗尽"

# 是结构化对象，不带 evidence 引用就编译不过
DiagnosisClaim(
    assertion="数据库连接池耗尽导致超时",
    evidence_refs=["ev-001", "ev-003"],  # 必须引用真实 evidence id
    hop_count=2,
    confidence_tier="Certain"
)
```

**面试时可以强调的**：这是从 OpenCode/OpenClaw 的教训中学到的。它们没有证据锚定——Agent 的输出就是自由文本，用户只能选择"信或不信"。Hermes 的 memory 有硬上限 2200 chars 也是同理——不是建议"少写点"，是硬截断。

---

#### 机制 2：逐跳验证门控（Hop-gated Verification）

**解决的问题**：故障诊断通常是多步推理链——症状→异常组件→异常指标→根因。但 LLM 两个毛病：一是跳步——看到症状就直接猜根因；二是累积误差——每跳 90% 准确率，三跳只剩 73%。

**为什么不是 prompt 能解决的**：你可以说"请逐步推理"，但 LLM 可以在文本里写"第一步…第二步…"而实际没有等工具返回就继续。文本的"步骤"不等于实际的 hop。

**我们的方案**：在 `agent_iteration()` 和下一次 `agent_iteration()` 之间插入代码层的 Gate。Gate 不是 LLM 自己判断"我觉得证据够了"，而是规则引擎检查：

- 当前 hop 是否至少有一条来自工具（code/log/stack）的 evidence？如果只有 LLM 自己的推理 → 不通过
- 当前 hop 的 evidence relevance 是否至少有一条 ≥ 0.3？如果全都低相关 → 不通过

只有 Gate 返回 `PASS`，`ctx.current_hop` 才 +1，才允许进入下一跳。不通过就继续当前 hop 收集证据。

```python
while True:
    await agent_iteration(ctx, llm, tools)  # 单次 LLM call + 工具执行

    gate_result = check_hop_gate(ctx)        # 代码层规则引擎
    if gate_result == "PASS":
        ctx.current_hop += 1                 # 允许进入下一跳
    elif gate_result == "INSUFFICIENT_EVIDENCE":
        continue                             # 继续当前 hop 收集证据
```

**为什么要区分"hop"和"iteration"**：一次 hop 可能包含多次 iteration（调了多个工具才收集够证据）。Gate 关心的是"这个推理跳的证据够不够"，不是"调了多少次 LLM"。

**面试时可以强调的**：这和 Hermes 的 preflight 压缩是同一种思路——"在调 API 之前先检查"。只不过 Hermes 检查的是 token 数，我们检查的是证据充分性。

---

#### 机制 3：鉴别诊断（Differential Diagnosis）

**解决的问题**：一个症状（P99 延迟飙升）可能对应多个根因——DB 连接池耗尽、慢查询、GC 停顿、网络抖动。LLM 容易锚定第一个想到的假设，然后所有工具调用都在"验证"它，忽略矛盾信号。这在医学诊断中叫 confirmation bias（确认偏倚），是导致误诊的首要原因。

**为什么不是 prompt 能解决的**：你可以说"请考虑多种可能"，但 LLM 的注意力机制天然倾向于维持已有方向——它生成了"可能是连接池问题"之后，后续 token 更可能延续这个方向而非自我否定。

**我们的方案**：借鉴医学鉴别诊断方法论——先列所有可能（differential），再逐一排除（diagnosis）。数据模型从单一 `Judgment` 改为 `HypothesisSet`：

```
Phase 1（展开）：LLM 看到症状 → 生成候选假设
  Hypothesis A: DB 连接池耗尽
  Hypothesis B: 慢查询
  Hypothesis C: GC 停顿

Phase 2（排除）：对每个假设调工具验证
  Hypothesis A → 查连接池 metrics → 活跃 45/100，正常 → Ruled-out ✗
  Hypothesis B → 查 slow_query.log → 命中 3 条 >1s → Confirmed ✓
  Hypothesis C → 查 GC 日志 → 近 30 分钟无 Full GC → Ruled-out ✗
```

关键设计：
- **排除必须基于工具返回的数据**，不能是 LLM 觉得"不太可能"
- **每条排除理由可追溯**——"为什么排除 A？因为 connection_pool_metrics:89 显示活跃连接 45/100"
- **确认不等于停止排除**——确认 B 后还要把 C 排完，确保没有共因

**面试时可以强调的**：Hermes 的 skill 有三种状态（active/stale/archived），Microtrace 的假设有四种状态（candidate/investigating/confirmed/ruled-out）。状态机是管理不确定性的通用模式——不是只有 workflow engine 才用状态机。

---

#### 机制 4：置信度分层（Confidence Tier）

**解决的问题**：LLM 说"肯定是连接池的问题"——但 LLM 没有"不确定"这个概念。它输出一个 float 的 confidence，但 0.8 可以在一次对话里意味着"我猜的"、另一次意味着"我证实了"。而且如果没有 tier→action 映射，0.8 和 0.9 对系统的行为没有区别。

**我们的方案**：Tier 不是 LLM 自己评的 float，是规则引擎根据客观条件计算的离散等级：

| Tier | 条件（规则引擎判定） | 行动权限 | UI |
|------|---------------------|---------|-----|
| **Certain** | 2+ 条 tool 证据 + 1+ 替代假设已排除 | ✅ 可自动执行修复 | 绿色 |
| **Likely** | 有直接 tool 证据，替代假设未全部排除 | ⚠️ 建议人工确认 | 黄色 |
| **Suspected** | 仅基于模式匹配，无硬证据 | 👁️ 仅展示，不可操作 | 灰色 |
| **Ruled-out** | 已验证排除 | ❌ 不再考虑 | 删除线 |

```python
def compute_confidence_tier(h: Hypothesis, ctx: Context) -> str:
    has_direct_evidence = len(h.evidence_for) >= 2
    alternatives_ruled_out = any(
        c.status == "ruled-out" for c in ctx.hypotheses.candidates
    )
    all_from_tools = all(
        ref_has_tool_source(ref, ctx.evidence) for ref in h.evidence_for
    )

    if has_direct_evidence and alternatives_ruled_out and all_from_tools:
        return "Certain"
    elif has_direct_evidence:
        return "Likely"
    else:
        return "Suspected"
```

**关键设计决策——为什么不是 LLM 自评**：LLM 的 confidence score 不可靠——它会给出 0.95 但基于的是"我见过类似模式"而非"我验证了这个具体实例"。规则引擎用客观信号（几条证据、哪些来源、排除了多少替代）判断，和 LLM 的输出解耦。

**面试时可以强调的**：这是从自动驾驶借来的概念——L1-L5 不是 AI 自评的，是客观条件（需要人类接管吗？能处理所有路况吗？）判定的。Microtrace 的 Certain/Likely/Suspected 同理。

---

#### 机制 5：矛盾强制回溯（Contradiction-triggered Backtrack）

**解决的问题**：排查到一半，第三个工具的结果证明第一个假设是错的。LLM 容易"坚持"已有结论——选择性忽略矛盾证据，或者把矛盾解释成"异常波动"。这是人类工程师也会犯的错，LLM 更严重——因为它的自回归生成天然倾向维持已输出的方向。

**和 Doom Loop 的区别**：Doom Loop 检测的是"重复调用相同工具"——量的问题。矛盾回溯检测的是"新证据否定旧假设"——质的问题。一个有价值的 Agent 可能在同一个工具上做不同参数的调用（不是 Doom Loop），但新证据恰好推翻了上一轮的假设（是矛盾回溯要捕获的）。

**我们的方案**：每次工具返回后，自动执行三条规则：

```
规则 1：EVIDENCE_STALE
  同 source + 同 location 的数据发生变化
  → 标记当前假设的 evidence 为过期，重新验证

规则 2：EVIDENCE_CONTRADICTION
  新证据直接否定当前假设的 claim
  → 假设 → Ruled-out，自动激活下一个 candidate

规则 3：HYPOTHESIS_REVIVE
  之前排除某假设的理由被新证据推翻
  → 恢复假设到 candidate 状态（"排除错了"）
```

具体例子：

```
Turn 3: 当前追踪 Hypothesis B（慢查询）
Turn 4: 工具返回 slow_query.log 近 30 分钟无记录

系统检测：
  ① Hypothesis B 依赖 "slow_query.log 有记录"
  ② 工具返回 "slow_query.log 为空"
  ③ 判定：EVIDENCE_CONTRADICTION

自动回溯：
  ① Hypothesis B → Ruled-out（理由：slow_query.log 为空）
  ② 激活下一个 candidate（Hypothesis A 或 C）
  ③ 如果无剩余 candidate → 回到 Hop 1 重新分析症状
```

**面试时可以强调的**：Hermes 的 curator 按时间自动降级 skill（active→stale→archived），不是让 LLM 决定。Microtrace 的矛盾回溯同理——不是让 LLM "发现矛盾请重新思考"，是代码检测到矛盾后强制标记。**系统级规则 > LLM 自主判断**，这是四个 Agent 产品最一致的教训。

---

#### 机制 6：诊断模式进化（Diagnosis Pattern Learning）

**解决的问题**：一个 VNFM 工程师在解决第 51 个"订单服务 NPE"时不应该从零开始——但 Agent 每次都是冷启动。LLM 本身没有跨 session 的学习能力。Hermes 解决了同一个问题——Agent 完成任务后把经验写成 SKILL.md，下次遇到类似任务直接加载。

**为什么这是 Hermes 方法论最直接的继承**：

```
Hermes:   完成任务 → nudge → 创建 SKILL.md → curator 维护 → skill_view 加载
Microtrace: 完成诊断 → 提取 → 创建 Pattern → curator 验证 → 症状匹配 → 注入 hint
```

**我们的方案**：跨 session 的诊断模式存储和进化系统。

*1. 自动提取*：CONCLUDE 后，如果 confidence tier = Certain 且 hop_count ≥ 2，自动调 LLM 从本次诊断中提取可复用模式——症状特征、诊断路径、根因模板。

```python
DiagnosisPattern {
  symptom_signature: "OrderService NPE + userId=null + UserService.validate",
  diagnostic_path: [
    "parse_stack_trace → UserService.java:234",
    "find_class → UserService",
    "read_file → userId 参数未校验"
  ],
  root_cause_template: "UserService.java:234 对 userId 参数缺少空值校验",
  success_count: 1,
  confidence: 0.5,
  state: "active"       # 和 Hermes 完全一致的三态
}
```

*2. 症状匹配*：INTAKE 后、INVESTIGATE 前，用 symptom_signature 做语义匹配。相似度 > 70% → 注入 Pattern Hint——不是答案，是 head start。

*3. 三态生命周期*：完全对标 Hermes 的 skill 三态——active（正常使用）、stale（长时间未使用或连续误诊）、archived（curator 确认归档）。

*4. 使用中进化*：每次成功复用 → confidence 提升；每次命中但最终结论不匹配 → confidence 折半。

**关键案例**：

```
第一次：OrderService NPE → 8 轮完整排查 → 根因：UserService 缺空值校验
    → 提取 Pattern

第二次（两周后）：OrderService 又报 NPE
    → 症状匹配 92% → 注入 hint："上次这种情况是 UserService 的问题"
    → LLM 直接读 UserService → 确认 → 2 轮完成（vs 上次 8 轮）
```

**反过拟合防护**：症状相同但根因不同的情况——系统级硬约束，不是 prompt 建议。

```
第二次：OrderService NPE（症状完全一样）
    Pattern 匹配 92% → 注入 hint → LLM 查 UserService → 校验已加上了，没问题

    ⚠️ 如果 LLM 过度信任 pattern → 继续围绕 UserService 找 → 浪费 3-5 轮
    ✅ 实际上根因是：上游 Nginx 配置错误，userId 根本没传过来

代码层防护：
    规则 4：PATTERN_MISLEAD
      有活跃 pattern hint + pattern 建议的工具已调 >= 2 次
      + 返回的证据均不支持 pattern 的根因模板
      → 自动撤销 pattern hint + 注入重定向提示 + pattern.confidence *= 0.5
      → 连续 2 次误诊 → pattern.active → stale
```

**面试时可以强调的**：这是从 Hermes 学到的三个设计决策的直接应用——**自动提取**（nudge → skill_manage create）、**curator 管理生命周期**（active/stale/archived）、**按需加载不自动注入**（skill_view → pattern_match hint）。而且加了一个 Hermes 没有的补丁——**反过拟合防护**——因为诊断场景的模式误用比技能场景更危险。技能用错了最多效率低，模式用错了会导致错误诊断。

### 8.4 面试常见追问

**Q1: 为什么不直接用 OpenCode/OpenClaw 改？**

> 我们确实借鉴了 OpenCode 的通用 Agent 机制——Doom Loop 检测、Compaction、Event Sourcing、Retry Policy。这些是"每个 Agent 都需要的基础设施"。但问题定位场景有三个 OpenCode 解决不了的问题：一是结论必须可追溯（OpenCode 输出自由文本就行），二是假设必须可管理（OpenCode 没有 hypothesis 概念），三是证据矛盾必须自动检测（OpenCode 只有 Doom Loop 没有 contradiction detection）。所以通用层用了他们的方案，专用层自己设计。

**Q2: 这些机制会不会让 Agent 变得太"僵化"？LLM 的优势不就是灵活性吗？**

> 这是个好问题。我们的设计原则是：**LLM 决定"往哪走"，规则引擎决定"能不能走"。** 假设生成、证据解读、推理方向——这些 LLM 自由发挥。但"这个 hop 证据够不够"、"新证据是否和已有假设矛盾"、"置信度到了哪一档"——这些是规则引擎判定的。就像自动驾驶：AI 决定方向盘往哪打，但"速度不能超过 120"是硬编码的。我们不是在限制 LLM 的推理能力，是在给它一个安全边界。

**Q3: 五机制里哪个最难实现？**

> 矛盾回溯。因为"新证据是否否定已有假设"涉及语义判断——不是简单的字符串对比。我们的当前方案是规则 1 和 3 靠字符串/结构匹配（source+location 相同但 content 不同 → stale），规则 2 依赖 LLM 在输出 evidence 时标注 `contradicts_hypothesis_id` 字段。完全自动化的语义矛盾检测需要专门的模型，这是 Phase 2 的事。

**Q4: 和其他故障诊断工具（如 APM 的告警关联）有什么区别？**

> APM 告警关联是纯规则/统计的——"CPU 高 + DB 慢 → 可能是 DB 问题"。它不需要理解代码语义。Microtrace 处理的场景更复杂——业务报错可能涉及代码逻辑 bug、下游透传、配置问题等，需要读代码、读日志、理解调用链。LLM 的语义理解是必需的，但 LLM 的不可靠性也是必须解决的——所以才有五机制。

**Q5: 你从 Hermes Agent 学到了什么？**

> 最大的启发不是某个具体功能，而是**设计方法论**：先定义核心矛盾，再让所有模块围绕它对齐。Hermes 的每个模块——System Prompt 的 3-tier 结构、Memory 的硬上限、Curator 的自动流转、Context Compressor 的 preflight 检查——全部可以追溯到"防止上下文劣化"这个核心矛盾。
>
> 具体设计决策上，Microtrace 的诊断模式系统直接继承了 Hermes skill 的三个模式：自动提取（nudge → skill_manage create → pattern extract）、curator 管理生命周期（active/stale/archived）、按需加载不自动注入（skill_view → pattern_match hint）。此外在反过拟合防护上比 Hermes 更激进——因为诊断场景的模式误用比技能误用更危险，技能用错了最多效率低，模式用错了会导致错误诊断。所以加了代码层的硬撤销机制——2 轮内 pattern 无证据支持 → 强制删除 hint + pattern 降级。

**Q6: 诊断模式会不会让 Agent 过度依赖历史经验，在新问题上误判？**

> 这是我们首先考虑的风险——症状相似但根因完全不同的情况在微服务系统里非常常见。我们有三层防护：第一层 prompt 边界——"2 轮内找不到支持证据就放弃该方向"；第二层代码硬撤销——每次工具返回后自动检测，如果 pattern 建议的工具调了 2 次以上但返回的 evidence 都不支持 pattern 的根因结论，代码层直接删除 pattern hint，注入 forced redirection prompt；第三层跨 session 降级——导致误诊的 pattern confidence 折半，连续 2 次误诊自动转为 stale，curator 后续决定是否归档。
>
> 这和机制 5（矛盾回溯）是同构的思路——矛盾回溯是"当前假设 vs 新证据"的检测，反过拟合是"历史模式 vs 当前证据"的检测。两个都是代码层硬约束而非 prompt 软建议。
