# Yada

**Yet Another DeepSeek Agent**：一个专为 DeepSeek V4 构建的小型、可审计
Coding Agent Harness。

[English README](README.md)

Yada 有意保持克制：单 Agent、追加式会话、5个工具、带版本校验的 Patch，
以及“修改后必须通过测试才能完成”的验证门槛。运行时没有第三方 Python
依赖；开发和测试使用 pytest。

> 当前为 Alpha：离线 Agent 闭环已通过测试，但尚未宣称任何对比评测结果。

## 快速开始

需要 Python 3.11+、Git 和 DeepSeek API Key。

```bash
cd Yada
python3 -m venv .venv
.venv/bin/python -m pip install -e .
export DEEPSEEK_API_KEY="sk-..."

yada "修复 parser 的边界问题，并运行相关测试" \
  --workspace /path/to/repository
```

Yada 默认会在运行仓库命令前请求确认。在一次性沙箱中可以启用自动执行：

```bash
yada --task-file issue.md --workspace /workspace --yes
```

默认使用 `deepseek-v4-pro`、开启思考模式，并把推理强度设为 `max`。

## 最小闭环

```text
稳定 Prompt + Tool Schema
          ↓
DeepSeek 工具调用
          ↓
校验 → 审批 → 执行
          ↓
结构化、限长的 Observation
          ↓
追加会话并继续
          ↓
最新 Patch 通过验证后才允许 finish
```

工具只有5个：

- `search_code`：优先使用 ripgrep，缺失时使用 Python 回退。
- `read_file`：分段读取带行号内容，并返回 SHA-256。
- `apply_patch`：使用 Git Unified Diff，并检查全部目标文件 Hash。
- `run_command`：无 Shell 的 argv 执行、命令白名单和用户审批。
- `finish`：最新修改后没有成功测试或构建时直接拒绝。

DeepSeek 思考模式要求工具轮次继续回传 `reasoning_content`。Yada 会在内存中
保留并正确回传，但 JSONL 轨迹默认只记录长度和 Hash；只有显式使用
`--trace-reasoning` 才会落盘完整推理。

## 安全边界

Yada 提供 Guardrail，但不是完整的操作系统沙箱：

- 文件工具拒绝工作区逃逸、符号链接逃逸、`.git` 和 `.yada`。
- Patch 拒绝二进制、重命名、复制、权限模式和符号链接变更。
- 命令使用 argv 数组，拒绝 Shell `-c` 和修改性 Git 子命令。
- 子进程环境会移除 API Key、Token、Secret 等变量。
- 默认每条命令都需要确认；`--yes` 仅应在隔离环境使用。

仓库测试本身就是任意代码。陌生仓库应放在一次性 VM 或更强的沙箱中运行。
Dockerfile 能限制文件系统暴露，但不会阻断容器网络。

## 测试

测试套件包含一个完全离线的 Fake Model 端到端流程，以及过期 Hash、路径
逃逸、密钥环境变量和验证门槛测试：

```bash
python3 -m pip install -e ".[dev]"
python3 -m pytest
```

pytest 只是开发依赖，不会增加 Yada 的运行时依赖。fixture 复用临时 Git
仓库，`monkeypatch` 显式控制环境变量，普通 `assert` 则提供更清楚的失败信息。

## 项目结构

Yada 采用 `src/` 布局，并把 Agent 编排与具体执行分离：

```text
src/yada/
├── agents/        # 追加式模型/工具循环与 Prompt
├── models/        # 模型协议与 DeepSeek API 适配器
├── environments/  # 工作区边界与命令审批
├── tools/         # 每个工具一个模块，以及小型分发器
├── traces/        # JSONL 轨迹记录
├── run/           # CLI 入口
└── utils/         # 输出截断等通用逻辑
tests/
├── agents/
├── models/
└── tools/
```

边界设计参考了 mini-SWE-agent 的有效结构，但 Yada 保留自己的多工具协议、
基于 SHA 的 Patch、命令策略和验证门槛。

## 借鉴与差异

Yada 借鉴了 [mini-SWE-agent](https://github.com/SWE-agent/mini-swe-agent)
的极简循环、[SWE-agent](https://github.com/SWE-agent/SWE-agent) 的可复现轨迹，
并直接遵循 DeepSeek 官方的[思考模式](https://api-docs.deepseek.com/guides/thinking_mode)
和[工具调用](https://api-docs.deepseek.com/guides/tool_calls)契约。代码为独立实现，
目标是成为一个便于实验和消融的 DeepSeek-native Harness。

当前不做 TUI、IDE、MCP、Skills、多 Agent、联网、长期记忆、模型路由或自动
提交。详细设计见 [docs/architecture.md](docs/architecture.md)。
