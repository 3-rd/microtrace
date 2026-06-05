# VISION.md - microtrace

> 📌 初稿 v3（2026-06-05 11:59 刷新）—— 待老板逐节继续刷新

---

## 1. 核心故事

> **microtrace 帮助 VNFM 维护工程师定位"周边部门反馈的各种问题"，基于代码 + 日志事实推理，给出问题归属和证据链。**
>
> **问题类型不限**：业务报错、性能问题、内存溢出、服务重启等。
> **分类策略不 hardcode**：每种问题类型的内部分类写在 `prompts/agent.md` 的 playbook 里，加新类型 = 加一节 prompt，**零代码改动**。
> **多轮对话是核心**：不是"输入一次出结果"，而是工程师跟 Agent 持续质疑、补料、追问的对话过程。

---

## 2. 目标用户

**核心用户**：VNFM 维护工程师（7 级左右，有 Java 代码能力）
**不是**：周边部门的反馈人（他们不需要 microtrace，只把报错转给维护工程师）

**目标部署形态**：每个工程师本机一份，不做共用后端。
- 案例/知识库共享走 git（独立 repo）
- 事故数据敏感性高（核心网网管），不出域

---

## 3. 必杀技

**严格基于事实的推理**：每条结论必须能引用证据（代码行 / 日志片段 / 调用链），不允许"我觉得是"。

具体含义：
- **可追溯**：每个判断都标注"我看了 X 才得出 Y"
- **可反驳**：用户能指着证据说"这条是错的"，Agent 能识别并修正
- **不臆造**：证据不足时，明确说"我无法判断，需要 X 信息"

---

## 4. 问题类型与分类策略（**示例：业务报错**）

业务报错是 **Phase 0 唯一会接触的问题类型**（最常见），它的内部三分类：

| 分类 | 含义 | microtrace 调查终点 |
|---|---|---|
| **A. 本产品 Bug** | 我们代码有错 | 根因代码位置（file:line）+ 证据链 |
| **B. 下下游产品报错** | 我们调用下游，下游返错，我们透传或包装 | 透传/包装点 + 下游原始错误码 |
| **C. 使用方法问题** | 用户用法错，被我们内部校验拦住 | 校验点 + 拦截规则 + 业务逻辑说明 |

**其他问题类型**（性能 / OOM / 重启）暂时不做，**等 Phase 0 跑通后**：
- 性能问题：内存 / CPU / IO 阻塞 / 锁竞争
- 内存溢出：heap / metaspace / thread stack / off-heap
- 服务重启：OOM kill / 进程崩溃 / 健康检查失败 / 主动重启循环

每种类型的分类策略都写在 `prompts/agent.md` 里。

---

## 5. 场景边界

### 做
- ✅ Java 多微服务（千万行级）
- ✅ 多种问题类型（业务报错 / 性能 / OOM / 重启，**但不锁死**）
- ✅ 基于事实的证据链
- ✅ 多轮对话（质疑、补料、追问）

### 不做（Phase 0 明确不做）
- ❌ 知识库 / RAG（**Phase 0 不依赖任何知识库**）
- ❌ 多语言（**只 Java**，Go / Python 后续阶段）
- ❌ TUI app（OpenCode 式多视图交互）
- ❌ Web UI（**Phase 1+ 才考虑**）
- ❌ One-shot CLI（**REPL 才是形态**，不是输入一次出结果）
- ❌ 持久化
- ❌ 配置化 / DSL
- ❌ 远程部署 / 共用后端
- ❌ 自动修复
- ❌ 多 LLM 路由（**只 1 家**，默认 MiniMax）

### 4 个最小工具**只服务"业务报错"类型**
- 其他类型（性能 / OOM / 重启）需要新工具，**等 Phase 0 跑通再加**

---

## 6. Phase 0 最小集

### 形态
- **REPL**（基于 `prompt_toolkit`）：用户跟 Agent 多轮对话
- **HTTP API**（FastAPI）：agent 暴露为 HTTP 服务，REPL 和未来 Web UI 都是它的客户端

### Agent 核心
- **CLI/REPL 入口**：`microtrace` 命令
- **HTTP API**（FastAPI）：`- chat` / `- state` / `- evidence` / `- save` 端点
- **Agent Loop**：驱动 INVESTIGATE 内部的循环
- **状态机**：3 态（INTAKE / INVESTIGATE / CONCLUDE）
- **Context 管理**：problem / judgment / evidence / tool_history / reasoning_trace
- **LLM 客户端**：抽象接口 + 1 家实现
- **工具注册中心**：所有工具走统一入口

### Master Prompt
- **`prompts/agent.md`**：内置问题类型 taxonomy + 每种类型的分类策略 + 工具使用指南
- 加新问题类型 = 在 agent.md 加一节
- **不分多个 prompt 文件**

### 最小工具集（4 个，**只服务业务报错**）
- `read_file`：读代码 / 读日志文件
- `search_logs`：按关键词 / 时间搜日志
- `find_class`：按类名定位文件
- `parse_stack_trace`：解析堆栈，提取 class+method+line

### 输出层
- `output/models.py`：DiagnosisOutput 数据结构（dataclass）
- `output/text.py`：rich 渲染（REPL 用）
- `output/json.py`：JSON 序列化（未来 Web UI 预留接口）

### REPL 命令集
- `<自然语言>`：用户问题，Agent 自动处理
- `status`：查看当前状态
- `evidence`：展开看完整证据链
- `save [--case]`：保存案例到本地
- `config set <key>=<val>`：改配置
- `clear`：重置会话
- `exit` / `quit`：退出

---

## 7. v1 教训（明确不重蹈）

| v1 干了 | microtrace 不做 |
|---|---|
| 6 态状态机 | 3 态 |
| 贝叶斯多假设竞争 | 单 judgment + 置信度 |
| 反事实验证 | 不用，loop 退出靠 LLM 自决 |
| 危险操作分级 | 不用，Phase 0 工具都安全 |
| Doom Loop 检测 | 不用，简单轮数上限 |
| 上下文压缩 | 不用，截取 + 排序代替 |
| RAG / Case store | 不用，Phase 0 纯代码 + 日志推理 |
| 把所有问题都套 A/B/C | **类型在 prompt 里声明**，不 hardcode |
| One-shot CLI | **REPL 多轮对话** |
| 复杂 TUI app | **REPL + rich**（简单、SSH 友好） |
| 共用后端 / 远程部署 | **每工程师本机一份 + 案例 git 共享** |

---

## 8. prompts 设计原则（**防漂移硬规则**）

1. **问题类型 taxonomy 在 prompt 里**（**不是代码**）
2. **加新类型 = 在 master prompt 加一节**（不改代码）
3. **不预留"以后要 hardcode"的抽象**（YAGNI）
4. **agent.md 是 agent 的唯一 playbook**（不分多个文件）
5. **任何"按问题类型分支"的设计冲动都先压住**——LLM 自己会判断

---

## 9. 形态与配置原则（**防漂移硬规则**）

1. **Phase 0 = REPL + HTTP API**——agent 是 HTTP 服务，REPL 是它的客户端
2. **Web UI = Phase 1+**——加在 HTTP API 之上，**不改 agent**
3. **多轮对话是核心**——不是"输入一次出结果"
4. **配置走文件 + CLI 子命令**（学 `gh` / `kubectl` / `aws`）——不上 TUI 表单，不上 Web 表单
5. **案例/知识库走 git**——不上中心化服务
6. **不预留"以后做 web/远程"的抽象**（v1 教训：能不抽象就不抽象）

---

## 10. 待刷新（TODO）

- [ ] 核心故事措辞（再润色）
- [ ] 必杀技"严格"具体怎么衡量
- [ ] 证据不足时 microtrace 怎么跟用户交互（一次性 vs 多轮 ← **已确认多轮**）
- [ ] ARCHITECTURE TODO：3 态 vs 4 态 / loop 退出条件 / evidence 截取
- [ ] `prompts/agent.md` 的具体写法
- [ ] 4 个最小工具的输入输出 schema
- [ ] REPL / HTTP API 的具体接口设计
