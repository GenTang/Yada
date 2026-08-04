# Yada

**Yet Another DeepSeek Agent** 是一个为 DeepSeek V4 构建的小型、可审计
Coding Agent Harness。给它一个任务和一个 Git 仓库，Yada 会检查代码、应用经过
校验的 Patch、运行验证，并记录完整执行轨迹。

[English README](README.md)

> 只想使用 Yada？直接阅读下面的**快速开始**。想贡献代码？请从
> [CONTRIBUTING.md](CONTRIBUTING.md) 和[开发者文档](docs/dev/architecture.md)
> 开始。

Yada 目前处于 Alpha 阶段。仓库已经测试本地 Agent 闭环，但尚未宣称任何对比
评测结果。

## 运行条件

- Python 3.11+
- Git
- [uv](https://docs.astral.sh/uv/)（推荐）
- DeepSeek API Key

直接运行 `yada` 和执行 `yada eval --case` 不需要 Docker；官方
`yada eval --swebench` 评测要求安装并启动仍受维护的 Docker Desktop 或
Docker Engine。安装、验证、版本策略与资源要求见
[Docker requirements](docs/configuration.md#docker-requirements)。

## 快速开始

```bash
git clone https://github.com/GenTang/Yada.git
cd Yada
uv sync --locked --dev

export DEEPSEEK_API_KEY="sk-..."

uv run yada "修复 parser 的边界问题，并运行相关测试" \
  --workspace /path/to/repository
```

Yada 默认会在运行仓库命令前请求确认。只有在可信、一次性的隔离环境中才应使用
`--yes`：

```bash
uv run yada --task-file issue.md --workspace /workspace --yes
```

## 运行时会发生什么

Yada 会打印每轮 DeepSeek 调用和工具执行，最后报告任务是否通过验证门槛。默认
Trace 保存在目标仓库的 `.yada/runs/` 目录下。

仓库测试可以执行任意代码。Yada 提供 Guardrail，但不是完整的操作系统沙箱；
处理陌生项目时请使用一次性 VM 或容器。

## 检查每一个步骤

将任意 JSONL Trace 转换成完全离线、自包含的可视化页面，无需服务器、CDN 或
额外运行时依赖：

```bash
uv run yada-trace TRACE.jsonl --html
```

[![Yada 离线 Trace Viewer](docs/assets/yada-trace-viewer.jpg)](docs/assets/yada-trace-viewer.jpg)

## 更多文档

- [配置](docs/configuration.md)：其他安装方式、API Key、模型参数、命令策略和
  Trace Level。
- [CLI 参考](docs/cli-reference.md)：`yada`、`yada eval` 和 `yada-trace`。
- [评测生命周期](docs/evaluation.md)：`--case` 与 `--swebench` 会加载、修改、
  缓存、评分和写入哪些内容。
- [贡献指南](CONTRIBUTING.md)：开发环境、验证命令和基于 Rebase 的 PR 流程。
- [架构](docs/dev/architecture.md)：Agent 循环、工具、Patch 事务、评测边界与
  安全不变量。
- [调试](docs/dev/debugging.md)：测试、Trace 检查与可复现 Issue。

Yada 使用 [MIT License](LICENSE)。
