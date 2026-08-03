# Yada

**Yet Another DeepSeek Agent**：一个专为 DeepSeek V4 构建的小型、可审计
Coding Agent Harness。

[English README](README.md)

Yada 有意保持克制：单 Agent 循环、独立的规划/执行边界、追加式会话、5个工具、
带版本校验的 Patch，以及“修改后必须通过测试才能完成”的验证门槛。运行时没有
第三方 Python 依赖；开发验证使用 Ruff 和 pytest。

> 当前为 Alpha：离线 Agent 闭环已通过测试，但尚未宣称任何对比评测结果。

## 通用评测

Yada 内置了 Benchmark-neutral 的评测层。`EvalRunner` 将任意
`BenchmarkAdapter` 与任意 `AgentAdapter` 组合起来；当前提供本地 JSON Manifest、
SWE-bench、原生 Yada 和外部命令四个适配器。

仓库内置了一个可移植的 SWE-bench Verified 开发用例。首次运行会拉取 pytest 的
精确 commit，并创建锁定的 Python 3.9 任务环境；后续运行复用缓存，但每个 Agent
仍获得全新的工作区：

```bash
uv run yada eval \
  --case benchmarks/swebench_verified/pytest-10051 \
  --agent yada \
  --yes
```

该命令会运行 1 个 FAIL_TO_PASS 和 15 个 PASS_TO_PASS 测试，产生真实的本地
resolved/unresolved 判定，但不等同于官方 Docker 成绩。源码缓存在
`.yada/cache/evals/`，仓库只提交任务配方和任务自己的 `uv.lock`。

运行 SWE-bench 时，Yada 只生成 Patch 和官方 `predictions.jsonl`，评分仍委托给
`swebench.harness.run_evaluation` 的 Docker Harness。使用 `--grade-mode none` 可以
只检查任务准备和预测文件，不会产生虚假的 resolved 结果。Manifest Schema、外部
Agent 命令模板和公平比较约束见 [docs/evaluation.md](docs/evaluation.md)。

## 快速开始

需要 Python 3.11+、Git 和 DeepSeek API Key。

推荐使用 `uv`：

```bash
cd Yada
uv sync --locked --dev
export DEEPSEEK_API_KEY="sk-..."

uv run yada "修复 parser 的边界问题，并运行相关测试" \
  --workspace /path/to/repository
```

也可以继续使用标准 venv 与 pip：

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
保留并正确回传。默认的 `--trace-level summary` 只记录紧凑的上下文指标；
`--trace-level debug` 还会保存每轮模型请求的完整脱敏 provider payload。
JSONL 默认只保留 reasoning 的长度和 Hash，并脱敏常见 API key、
Authorization、token、password 和 secret。只有显式使用
`--trace-reasoning` 才会落盘完整推理。

在评测中生成可还原的 debug trace：

```bash
uv run yada eval \
  --case benchmarks/swebench_verified/pytest-10051 \
  --agent yada \
  --yes \
  --trace-level debug
```

无需手工翻阅 JSONL，可以直接生成关联后的诊断时间线：

```bash
uv run yada-trace .yada/runs/20260801T120000.000000Z.jsonl
uv run yada-trace eval-results/<run>.artifacts/yada-trace.jsonl --step 8
uv run yada-trace eval-results/<run>.artifacts/yada-trace.jsonl --verbose
uv run yada-trace TRACE.jsonl --events
```

默认报告会按 Agent step 归组模型请求、响应、规划决定和有序工具执行，并为
step、模型调用、工具执行和协议事件显示真实 JSONL 行号。`--step` 和
`--verbose` 会在分组内展开脱敏后的模型消息、工具参数、Patch、stdout 和
stderr；`--events` 可切回带物理行号的平铺时间线。Debug trace 脱敏后仍可能
包含源码和测试输出，应当作敏感 artifact 处理。

默认 trace 和评测路径使用精确到分钟的系统本地时间，不包含秒和小数秒。
若名称已存在，Yada 会在文件或 `.artifacts` 后缀前依次添加 `(1)`、`(2)`；
同一次评测的结果 JSON 与 artifacts 目录始终使用相同编号。

## 安全边界

Yada 提供 Guardrail，但不是完整的操作系统沙箱：

- 文件工具拒绝工作区逃逸、符号链接逃逸、`.git` 和 `.yada`。
- Patch 拒绝二进制、重命名、复制、权限模式和符号链接变更。
- 命令使用 argv 数组，拒绝 Shell `-c` 和修改性 Git 子命令。
- 子进程环境会移除 API Key、Token、Secret 等变量。
- 默认每条命令都需要确认；`--yes` 仅应在隔离环境使用。

仓库测试本身就是任意代码。陌生仓库应放在一次性 VM 或更强的沙箱中运行。
Dockerfile 能限制文件系统暴露，但不会阻断容器网络。

## 开发验证

测试套件包含一个完全离线的 Fake Model 端到端流程，以及过期 Hash、路径
逃逸、密钥环境变量和验证门槛测试；Ruff 负责 Lint 和格式检查：

```bash
uv sync --locked --dev
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen pytest tests/ -v
```

CI 会在 Python 3.11 和 3.12 上运行同样的检查。没有 uv 时，可使用
`python3 -m pip install -e .` 安装运行时项目，再单独安装 `pytest` 和 `ruff`
并执行对应命令。它们只是开发依赖，不会增加 Yada 的运行时依赖。

## 项目结构

Yada 采用 `src/` 布局，并把 Agent 编排与具体执行分离：

```text
src/yada/
├── agents/        # 薄编排循环、无副作用 Planner 与工具 Executor
├── models/        # 模型协议与 DeepSeek API 适配器
├── environments/  # 工作区边界与命令审批
├── tools/         # 每个工具一个模块，以及小型分发器
├── traces/        # JSONL 轨迹记录与人类可读诊断报告
├── evals/         # 通用 Runner、Benchmark 与 Agent 适配器
├── run/           # CLI 入口
└── utils/         # 输出截断等通用逻辑
benchmarks/        # 可移植任务配方；运行时 checkout 放在 .yada/cache
tests/
├── agents/
├── evals/
├── models/
├── tools/
└── traces/
```

`Planner` 只负责会话策略和下一步动作校验，不做 I/O；`Executor` 负责参数解析、
工作区副作用和带关联 ID 的工具事件；`Agent` 只负责编排二者。它目前不是一次
额外的模型调用，但已经为未来的独立规划模型留下替换点，避免主循环演变成上帝类。

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
