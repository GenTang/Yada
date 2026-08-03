# Configuration

This guide covers installation choices, DeepSeek credentials, model settings,
execution policy, and trace capture. See the [CLI reference](cli-reference.md)
for every command-line option.

## Requirements

Yada requires Python 3.11+ and Git. The recommended setup uses
[uv](https://docs.astral.sh/uv/):

```bash
git clone https://github.com/GenTang/Yada.git
cd Yada
uv sync --locked --dev
```

The runtime itself has no third-party Python dependencies. To use a standard
virtual environment instead:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
```

After editable installation, use `.venv/bin/yada`; after `uv sync`, use
`uv run yada`.

## DeepSeek credentials

`DEEPSEEK_API_KEY` is required when the native Yada agent calls DeepSeek:

```bash
export DEEPSEEK_API_KEY="sk-..."
```

PowerShell:

```powershell
$env:DEEPSEEK_API_KEY = "sk-..."
```

Do not put the key in a task file, trace, issue, or commit. Yada sends it only in
the DeepSeek authorization header and removes secret-looking environment
variables from repository subprocesses.

## Model and endpoint

The main `yada` command and the native `yada eval` agent share these settings:

| Setting | Default | CLI | Environment |
| --- | --- | --- | --- |
| Model | `deepseek-v4-pro` | `--model` | `DEEPSEEK_MODEL` |
| API base URL | `https://api.deepseek.com` | `--base-url` | `DEEPSEEK_BASE_URL` |
| Thinking | enabled | `--thinking` / `--no-thinking` | — |
| Reasoning effort | `max` | `--reasoning-effort high\|max` | — |
| Maximum output tokens | `16384` | `--max-output-tokens` | — |
| API timeout | 300 seconds | `--api-timeout` | — |

CLI values override environment-backed defaults. For example:

```bash
uv run yada "Fix the failing test" \
  --workspace /path/to/repository \
  --model deepseek-v4-pro \
  --reasoning-effort high
```

When thinking is enabled, Yada retains `reasoning_content` across tool-calling
turns as required by DeepSeek. In non-thinking mode, Yada uses ordinary automatic
tool selection.

## Command execution policy

Repository commands are independently validated and then handled by one of
three policies:

| Policy | Behavior |
| --- | --- |
| `ask` | Prompt before each command; this is the default. |
| `allow` | Run approved command shapes without prompting. |
| `deny` | Reject every repository command. |

Choose a policy with `--command-policy`. `--yes` is an alias for
`--command-policy allow`:

```bash
uv run yada --task-file issue.md \
  --workspace /workspace \
  --command-policy ask
```

Yada accepts argv arrays rather than shell command strings, disables `sh -c` and
`bash -c`, restricts Git to read-only subcommands, and uses an executable
allowlist. These are guardrails, not an OS sandbox. Tests and build scripts can
still execute arbitrary code; use `allow` only in a trusted disposable VM or
container.

`--command-timeout` sets the default subprocess timeout in seconds. A model may
request a per-command timeout between 1 and 1800 seconds.

## Trace capture

Choose one of two trace levels:

| Level | Captured data |
| --- | --- |
| `summary` | Timing, responses, tools, compact request metrics; reasoning is replaced by length and SHA-256. |
| `debug` | Everything above plus sanitized provider request payloads and reasoning text. |

```bash
uv run yada "Fix the parser" \
  --workspace /path/to/repository \
  --trace-level debug
```

Both levels redact common API-key, bearer-token, password, credential, and
secret patterns. Debug traces can still contain reasoning, source code, prompts,
patches, paths, and test output. Treat them as sensitive artifacts.

By default, direct runs write to:

```text
WORKSPACE/.yada/runs/<task>__<system-local-time>.jsonl
```

Use `--trace PATH` to choose an exact location. Default names use local time at
minute precision and append `(1)`, `(2)`, and so on when a name already exists.

## Docker

The included image isolates the mounted filesystem but does not block container
network access:

```bash
docker build -t yada .
docker run --rm -it \
  -e DEEPSEEK_API_KEY \
  -v "/path/to/repository:/workspace" \
  yada "Fix the failing test" --workspace /workspace --yes
```

Use a stronger sandbox when the repository or its dependencies are untrusted.
