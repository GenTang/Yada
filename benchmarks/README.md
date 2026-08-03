# Reproducible benchmark cases

This directory contains small, checked-in benchmark recipes. Source checkouts,
virtual environments, model traces, and results are generated at runtime and
must not be committed.

Run the first local development case with:

```bash
export DEEPSEEK_API_KEY="sk-..."
uv run yada eval \
  --case benchmarks/swebench_verified/pytest-10051 \
  --agent yada \
  --yes
```

The first run downloads the exact repository commit into
`.yada/cache/evals/`, creates the case-specific uv environment, and may take a
few minutes. Later runs reuse both caches but always give the agent a fresh
workspace.

Cases under `swebench_verified/` use the canonical public problem statement and
base commit. Their local grader is intended for fast development feedback. A
published SWE-bench score must still come from the official Docker Harness.

This local path is intentional rather than a duplicate of `--swebench`: it
keeps prompt and tool regression tests fast, inspectable, and usable without
Docker. Use `--swebench INSTANCE_ID` when environment parity and an
official-compatible verdict matter; Yada then requires Docker before model
inference and separates the Agent command container from the final grader.
