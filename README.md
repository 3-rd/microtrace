# microtrace

Java 多微服务问题定位 Agent（VNFM 维护工程师用）。

## 安装

```bash
pip install -e .
```

## 使用

```bash
microtrace                       # 新 session
microtrace sessions              # 列历史 session
microtrace resume <session-id>   # 恢复 session
```

## 文档

- `docs/VISION.md` —— 核心故事 / 范围
- `docs/DESIGN.md` —— 决策日志（11 Q + Windows 兼容 + 状态转换审计）
- `docs/SPEC.md` —— 可执行实现规格

## 状态

Phase 0 开发中。设计已完成，实现进行中。
