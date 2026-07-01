# microtrace

Java 多微服务故障诊断 Agent — 帮助 VNFM 维护工程师定位"周边部门反馈的各种问题"，基于代码+日志事实推理，给出问题归属（A=本产品Bug / B=下游报错 / C=用法问题）和完整证据链。

---

## 1. 环境要求

| 依赖 | 版本 | 说明 |
|------|------|------|
| Python | **3.11+** | 必须 |
| Git | 任意 | 拉代码用 |
| pip | 最新 | 装依赖 |

---

## 2. 快速开始（5 分钟）

```bash
# 1. 拉代码
git clone <repo-url> microtrace
cd microtrace

# 2. 创建虚拟环境（推荐，避免污染系统 Python）
python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # macOS / Linux

# 3. 安装
pip install -e ".[dev]"

# 4. 配置 API Key
#    公司内部用 MiniMax API，找管理员要 key
set MICROTRACE_API_KEY=your-api-key-here    # Windows
# export MICROTRACE_API_KEY=your-api-key-here  # macOS / Linux

# 5. 跑自检（用 PetClinic 公开示例数据，不涉及公司内部信息）
pytest tests/ -q
# 看到 153 passed 就说明安装正确
```

---

## 3. 配置

### 3.1 API Key（必须）

API Key **不写在配置文件里**，通过环境变量传入：

```bash
# Windows (CMD)
set MICROTRACE_API_KEY=m3-xxxxxxxxxxxxxxxx

# Windows (PowerShell)
$env:MICROTRACE_API_KEY="m3-xxxxxxxxxxxxxxxx"

# macOS / Linux
export MICROTRACE_API_KEY=m3-xxxxxxxxxxxxxxxx
```

可选环境变量：

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `MICROTRACE_API_KEY` | LLM API Key | （空，必须设） |
| `MICROTRACE_BASE_URL` | LLM API 地址 | `https://api.minimaxi.com/anthropic` |
| `MICROTRACE_MODEL` | 模型名 | `MiniMax-M3-highspeed` |

### 3.2 日志路径（公司侧必改）

配置文件位置（自动生成）：
- Windows: `%APPDATA%\microtrace\config.yaml`
- macOS: `~/Library/Application Support/microtrace/config.yaml`
- Linux: `~/.config/microtrace/config.yaml`

```yaml
# config.yaml
tools:
  # 公司 VNFM 日志目录（改成实际路径）
  log_dirs:
    - /var/log/vnfm/
    - C:/ProgramData/VNFM/logs/

  # Java 源码根目录（find_class 工具用）
  java_source_roots:
    - /home/vnfm/project/src/
    - D:/vnfm-project/src/

agent:
  max_iterations: 8     # 推理最多 8 轮，可调大（上限 100）

llm:
  provider: minimax
  model: MiniMax-M3-highspeed
  base_url: https://api.minimaxi.com/anthropic
  timeout: 120.0
```

---

## 4. 运行方式

### 4.1 交互 REPL（最常用）

```bash
microtrace
```

输入问题文本（粘贴错误报告、堆栈、日志片段），Agent 开始诊断。状态栏实时显示当前状态、假设、证据数。

```
microtrace REPL — 输入问题开始诊断，输入 /exit 退出，输入 /help 查看命令

microtrace> 订单服务报 NullPointerException：
           at com.vnfm.order.OrderService.createOrder(OrderService.java:156)
           at com.vnfm.order.OrderController.submit(OrderController.java:89)
           Caused by: FeignClient 调用 inventory 服务返回 500

状态   INVESTIGATE
轮次   3/8
证据   5 条
假设   → [investigating] A(0.65): Feign 超时未处理 null 导致 NPE
         [candidate]    B(0.40): 下游 inventory 服务内部错误
```

### 4.2 非交互模式（脚本/CI 用）

```bash
microtrace --input "订单服务 NPE: at com.vnfm.order.OrderService.createOrder(OrderService.java:156)"
```

直接输出诊断结论，不进入 REPL。

### 4.3 Dry-run 模式（公司验证用）

```bash
microtrace --dry-run --trace-dir ./traces/ --input "订单服务 NPE: ..."
```

- 工具**只读不写**
- 每个 iteration 的完整信息（工具调用链、参数、Gate 判定、矛盾检测结果）记录到 trace 文件
- Trace 是纯结构化 JSON，**不含内部数据内容**，可以带回家分析
- 回家用好的 LLM 重新推理，调 prompt

---

## 5. REPL 命令速查

| 命令 | 说明 |
|------|------|
| `/status` | 查看当前状态（state、轮次、证据数） |
| `/evidence` | 展开查看完整证据链 |
| `/hypotheses` | 查看假设集（含四态、证据数、排除原因） |
| `/save` | 手动保存当前 session |
| `/config` | 查看当前配置 |
| `/exit` | 保存并退出 |

### 管理命令

```bash
microtrace sessions              # 列出历史 session
microtrace sessions -n 50       # 最近 50 个
microtrace resume <session-id>  # 恢复某个 session
microtrace delete <session-id>  # 删除
microtrace patterns             # 列出诊断模式库
microtrace serve                # 启动 HTTP API（开发用）
```

---

## 6. 在家开发 ↔ 公司验证工作流

```
┌─────────────────────────────────┐    ┌─────────────────────────────────┐
│          在家（开发机）           │    │         公司（办公电脑）           │
│                                 │    │                                 │
│  1. 改代码 / 调 prompt          │    │  1. git pull 最新代码             │
│  2. pip install -e ".[dev]"     │    │  2. pip install -e ".[dev]"      │
│  3. 用 PetClinic 示例数据自测    │    │  3. 改 config.yaml → VNFM 日志路径 │
│     pytest tests/ -q            │    │  4. 设 MICROTRACE_API_KEY         │
│     153 passed ✅               │    │  5. microtrace --input "真实报错"  │
│  4. git push                    │    │  6. 看诊断质量，记 LLM 问题        │
│                                 │    │  7. microtrace --dry-run → trace  │
│                                 │    │  8. trace 文件带回家分析           │
│                                 │    │                                 │
│  ← 带 trace 回家重放 ←──────────┼────┼                                 │
│  ← 根据 trace 调 prompt ←──────┼────┼                                 │
└─────────────────────────────────┘    └─────────────────────────────────┘
```

---

## 7. 首次在公司验证的推荐步骤

```bash
# Step 1: 确认安装正确
pytest tests/ -q
# 预期输出: 153 passed in ~1s

# Step 2: 用 PetClinic 示例跑通流程（确保 LLM 能连通）
microtrace --input "PetClinic OwnerController NPE at OwnerController.java:87"
# 预期: Agent 开始推理，调工具，输出假设集

# Step 3: 改配置指向公司日志
notepad %APPDATA%\microtrace\config.yaml
# 改 tools.log_dirs 为实际 VNFM 日志路径

# Step 4: 跑真实案例
microtrace
# 粘贴真实报错信息，观察诊断过程

# Step 5: 记录反馈
# - 诊断结论是否正确？
# - 哪一步推理有问题？
# - LLM 有没有"忽悠"（无证据下结论）？
# - 用 dry-run 导出 trace 带回家
```

---

## 8. 项目结构

```
microtrace/
├── src/microtrace/
│   ├── agent/                    # Agent 核心
│   │   ├── loop.py               #   主循环（双层 + Gate + 矛盾检测）
│   │   ├── state.py              #   5 态状态机
│   │   ├── doom_loop.py          #   死循环检测
│   │   ├── confidence.py         #   置信度分层规则引擎    [机制 ④]
│   │   ├── hop_gate.py           #   逐跳验证 Gate       [机制 ②]
│   │   ├── contradiction.py      #   矛盾检测 + 自动回溯   [机制 ⑤]
│   │   └── pattern_store.py      #   诊断模式进化         [机制 ⑥]
│   ├── context/
│   │   ├── models.py             #   全部数据模型
│   │   ├── prompt.py             #   Prompt 组装
│   │   └── compaction.py         #   上下文压缩
│   ├── tools/                    # 4 个诊断工具
│   ├── llm/                      # LLM 客户端（MiniMax）
│   ├── persistence/              # SQLite 存储
│   ├── repl/                     # 交互式终端
│   └── http/                     # HTTP API
├── prompts/agent.md              # Master Prompt（LLM 的 playbook）
├── data/                         # 公开示例数据（PetClinic，不涉及公司）
├── tests/                        # 153 个测试用例
├── docs/                         # 设计文档
│   ├── VISION.md                 #   核心故事 / 范围
│   ├── DESIGN.md                 #   决策日志（11 Q）
│   ├── SPEC.md                   #   可执行实现规格（权威参考）
│   └── Microtrace-核心矛盾到机制推导.md  # 六机制设计蓝图
└── CLAUDE.md                     # 开发上下文（当前状态、待定决策）
```

---

## 9. 设计文档导航

| 文档 | 什么时候读 |
|------|-----------|
| `docs/VISION.md` | 想了解这个项目要解决什么问题、边界在哪 |
| `docs/DESIGN.md` | 想知道某个设计决策为什么这么做（OpenCode/OpenClaw 对标） |
| `docs/SPEC.md` | 想改代码，需要查具体的数据模型/接口约定 |
| `docs/Microtrace-核心矛盾到机制推导.md` | 想理解六机制的设计逻辑和面试材料 |
| `CLAUDE.md` | 想了解当前开发状态、待定决策、下一步行动 |

---

## 10. License

MIT
