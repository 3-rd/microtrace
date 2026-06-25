# microtrace — Development Context

> VNFM Java 多微服务故障诊断 Agent。VISION.md / DESIGN.md / SPEC.md 是权威规格，本文档是**开发上下文**——当前状态、已定决策、待定问题、下一步行动。

---

## 1. 一句话定位

帮助 VNFM 维护工程师定位"周边部门反馈的各种问题"，基于代码+日志事实推理，给出问题归属（A=本产品Bug / B=下游报错 / C=用法问题）和证据链。

---

## 2. 当前状态

**Phase 0 开发中**。设计已完成（DESIGN.md 2773行，SPEC.md 3808行），实现进行中。

```
当前代码架构：
  OpenCode 通用机制（80%）         问题定位专用（20%）
  ├─ Loop（双层 while）            ├─ Evidence 结构
  ├─ State Machine（5态）          ├─ Judgment A/B/C
  ├─ Doom Loop（3次精确匹配）      ├─ judgment_history
  ├─ Compaction（PRUNE+SUMMARY）   └─ 关键行提取 regex
  ├─ Event Sourcing
  ├─ Tool 4态子状态机
  └─ Retry Policy

目标架构（六机制落地后）：
  通用机制（30%）                 问题定位专用（70%）
  ├─ Loop/Compaction/Doom Loop     ├─ 证据锚定（DiagnosisClaim 硬验证）
  └─ Event Sourcing/Retry          ├─ 逐跳验证 Gate（规则引擎）
                                   ├─ 鉴别诊断（HypothesisSet 替代 Judgment）
                                   ├─ 置信度分层（tier→action 映射）
                                   ├─ 矛盾强制回溯（post-tool 自动检测）
                                   └─ 诊断模式进化（跨 session 模式库）
```

---

## 3. 设计方法论：核心矛盾 → 六机制推导

借鉴了 **Hermes Agent** 的设计方法论——先定义核心矛盾，再让所有模块围绕它对齐。

**核心矛盾**：LLM 推理的自由度 vs 故障诊断对可追溯性的硬要求。

从这个矛盾推导出六个硬约束机制（详见 `docs/Microtrace-核心矛盾到机制推导.md`）：

| # | 机制 | 解决的问题 | 关键设计决策 |
|---|------|---------|------------|
| 1 | **证据锚定** | LLM 结论不可信 | DiagnosisClaim 强制携带 evidence_refs，validate_claim() 硬验证 |
| 2 | **逐跳验证门控** | 推理链累积误差 | check_hop_gate() 规则引擎（非 LLM 判断），证据不够不推进 |
| 3 | **鉴别诊断** | LLM 锚定第一个假设 | HypothesisSet 替代单一 Judgment，两阶段：展开→排除 |
| 4 | **置信度分层** | LLM 过度自信 | compute_confidence_tier() 规则引擎，tier→action 硬映射 |
| 5 | **矛盾强制回溯** | 新证据推翻旧假设 | check_evidence_contradiction() post-tool 自动检测，矛盾→标记回滚 |
| 6 | **诊断模式进化** | 重复问题冷启动 | PatternStore 跨 session，症状匹配→注入 hint，三态生命周期 |

**所有机制的共性**：代码层硬约束 > prompt 软约束。Gate 是规则引擎判断的，不是 LLM 自己说"证据够了"。矛盾是系统检测的，不是 LLM "发现矛盾请重新思考"。

---

## 4. 关键设计文档

| 文档 | 用途 | 开发阶段 |
|------|------|---------|
| `docs/VISION.md` | 核心故事、目标用户、必杀技、边界 | Phase 0 搭建用 |
| `docs/DESIGN.md` | 11 Q 决策日志、OpenCode/OpenClaw 对标 | Phase 0 搭建用 |
| `docs/SPEC.md` | 可执行实现规格（数据模型、模块接口） | **当前代码权威参考** |
| `docs/ARCHITECTURE.md` | 3 态状态机 + Loop 初稿（较旧） | Phase 0 参考 |
| `docs/Microtrace-核心矛盾到机制推导.md` | **六机制设计蓝图 + 面试材料** | Phase 1 开发蓝图 |

---

## 5. 已定决策（Phase 0，来自 DESIGN.md 11 Q）

- ✅ Q1 ASK_USER：硬阻塞 + 无自动超时（跟 OpenCode 一致）
- ✅ Q2 ASK_USER 触发+护栏：纯 prompt（不代码 rate limit），多选+自定义 UI
- ✅ Q3 judgment_update：版本化 + LLM 只看 current
- ✅ Q4 max_iterations：固定 8（可配置），达上限强制总结
- ✅ Q5 evidence relevance：LLM 自评
- ✅ Q6 Session 持久化：SQLite + 每轮存 + resume 命令
- ✅ Q7 tool_call 并行：默认并行，LLM 自己判断依赖
- ✅ Q8 Compaction：OpenCode 通用 PRUNE+SUMMARY + microtrace 独有 5 条结构规则
- ✅ Q9 Compaction 阈值：固定 20K（与 OpenCode 一致）
- ✅ Q10 Doom Loop 触发后：ASK_USER 弹窗（once/always/reject）
- ✅ Q11 Windows 兼容：platformdirs + single-source 路径 + prompt_toolkit

---

## 6. 待定决策（六机制落地前必须拍板）

这些是切换到开发前需要完成的设计决策（2026-06-26 讨论中识别）：

### 第一优先级：模型层

**Q1: HypothesisSet 替代 Judgment 的迁移**
- 当前 `ctx.current_judgment: Judgment` + `ctx.judgment_history: list[Judgment]`
- 目标 `ctx.hypotheses: HypothesisSet`（candidate/investigating/confirmed/ruled-out 四态）
- 需要决定：Phase 0 直接改模型不顾兼容？还是写 migration？
- **建议**：直接改，Phase 0 没有真实用户

**Q2: Hypothesis 精确字段定义**
- `evidence_for: list[str]` — 一条 evidence 能否同时支持多个 hypothesis？（应该能）
- `status` 谁来改？LLM 决策还是规则引擎自动流转？
- `ruled_out_reason` 自由文本还是枚举？（建议自由文本，LLM 生成）

**Q3: DiagnosisClaim 和 final_output 的关系**
- `DiagnosisClaim` 是内部验证结构
- `final_output` 是格式化后的人类可读文本
- 流程：LLM 输出 → 解析为 `DiagnosisClaim` → `validate_claim()` → 通过后格式化为 `final_output`
- **建议**：两种并存，Claim 用于验证，final_output 用于展示

### 第二优先级：流程层

**Q4: Gate 插入的精确位置**
- 方案 A：Gate 在 `agent_iteration()` 内部末尾
- 方案 B：Gate 在 `run_session()` 外层（agent_iteration 返回后检查）
- **建议方案 B**：Gate 是编排决策，不属于单次推理

**Q5: Pattern 匹配时机**
- 方案 A：状态转换时匹配一次（INTAKE→INVESTIGATE 之间）
- 方案 B：每次 agent_iteration 的 prompt 组装时匹配
- **建议方案 A**：匹配一次存入 `ctx.matched_patterns`，后续只检查是否要撤销

### 第三优先级：跨 session 层

**Q6: Pattern 存储**
- 选项 A：同一个 SQLite，新表 `patterns`（**建议**）
- 选项 B：独立 JSON 文件
- 选项 C：独立 SQLite

**Q7: symptom_signature 生成方式**
- **建议**：LLM 生成摘要 + 结构化字段（error_type + stack top class）同时存，匹配用 embedding 相似度

### 第四优先级：验证策略

**Q8: 六机制效果验证**
- 每个机制需要 metric 埋点来验证有效性（详见方法论文档 §六）
- Phase 1 开发时同步加 metrics，不可事后补

---

## 7. 最小落地路径（按此顺序开发）

```
Step 1: 改模型（context/models.py）
  ├─ 删 Judgment（current_judgment + judgment_history）
  ├─ 加 HypothesisSet + Hypothesis（4 态）
  ├─ 加 DiagnosisClaim（evidence_refs 硬验证）
  └─ 加 ConfidenceTier 枚举

Step 2: 改 prompt（prompts/agent.md）
  ├─ 教 LLM 输出 hypothesis 列表而非单个 judgment
  ├─ 教 LLM 鉴别诊断两阶段流程（展开→排除）
  └─ 教 LLM 输出结构化 DiagnosisClaim（含 evidence_refs）

Step 3: 改 loop（agent/loop.py）
  ├─ _conclude() 加 validate_claim()
  ├─ run_session() 加 hop 跟踪 + Gate 检查
  └─ agent_iteration() post-tool 阶段加矛盾检测

Step 4: 新增模块
  ├─ agent/hop_gate.py（逐跳验证规则引擎）
  ├─ agent/contradiction.py（矛盾检测 + 自动回溯 + Pattern 误导检测）
  ├─ agent/confidence.py（tier 计算规则 + action 映射）
  └─ agent/pattern_store.py（模式提取、存储、匹配、进化、降级）

Step 5: 跨 session 基础设施
  └─ persistence/sqlite.py 加 patterns 表 + 迁移
```

---

## 8. 技术栈

- Python 3.11+，Pydantic v2，FastAPI，prompt_toolkit，Typer，rich
- LLM：MiniMax API（OpenAI SDK 兼容），抽象 LLMClient Protocol
- 存储：SQLite（标准库 sqlite3）
- 包管理：hatchling，pyproject.toml

---

## 9. 相关外部项目（已读源码，用于设计对标）

| 项目 | 路径 | 借鉴了什么 |
|------|------|----------|
| OpenCode | `~/AIAgent/opencode/` | Loop（stream+事件）、Doom Loop（3次精确匹配）、Compaction（PRUNE+SUMMARY） |
| OpenClaw | `G:\AI\openclaw-main\` | Memory 体系、Skill 架构、Subagent 模式 |
| Hermes Agent | `G:\AI\hermes-agent-main\` | **设计方法论**（核心矛盾→机制推导）、Curator 生命周期、Pattern 进化对标 Skill 自创建 |

---

## 10. 开发约定

- **先动模型，再动逻辑**——模型是地基，模型不对上层白写
- **硬约束优先软约束**——能用代码校验的不用 prompt 建议
- **Phase 0 不兼容老数据**——没有真实用户，不需要 migration
- **每个机制加 metric 埋点**——不做不可测量的优化
- **YAGNI**——VISION.md 明确写了"不做"的就坚决不做
