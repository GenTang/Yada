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

## Editing strategy

Editing strategy is frozen for the complete run:

| Strategy | Editing tools shown to the model | Policy |
| --- | --- | --- |
| `patch-only` | `apply_patch` | Express every edit as a checked unified diff. |
| `replace-first` | `replace_text`, `apply_patch` | Prefer exact replacement for localized edits and use patch for unsuitable operations. |

`replace-first` is the default. Select `patch-only` explicitly when every edit must
use a checked unified diff:

```bash
uv run yada "Fix the localized parser bug" \
  --workspace /path/to/repository \
  --editing-strategy patch-only
```

Both strategies allow at most one editing tool call per Assistant turn. This
prevents a replacement and a precomputed patch from acting as an opaque
same-turn fallback. See the
[editing strategy design](dev/editing-strategy.md) for routing and recovery
rules.

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

## Docker requirements

Docker is optional for direct `yada` runs and `yada eval --case`. It is required
only for official `yada eval --swebench` evaluation, where Yada uses Docker for
both the public Agent command environment and the independent Harness grader.

### Supported installation and version policy

Use a currently maintained release of one of:

- [Docker Desktop](https://docs.docker.com/desktop/) on macOS or Windows; or
- [Docker Engine](https://docs.docker.com/engine/install/) on Linux.

Legacy Docker Toolbox installations and obsolete standalone clients are not
supported. SWE-bench 4.1.0 does not publish an exact minimum Docker
Desktop/Engine version, so Yada does not claim or enforce an arbitrary semantic
version floor. Compatibility is functional: the `docker` CLI must execute
natively on the host, connect to a running daemon without `sudo`, pull or build
Linux images, bind-mount a workspace, and run containers for the required
platform. Upgrade to the latest release supported by the host OS when any of
these checks fail.

Yada pins the Python Harness separately at `swebench==4.1.0` through uv's
temporary dependency overlay. Installing Docker does not install the Harness,
and installing the Harness does not install or start Docker.

### Install and verify

On macOS, install the correct Docker Desktop build for the machine's CPU from
the [official Mac installation guide](https://docs.docker.com/desktop/setup/install/mac-install/).
With Homebrew, the equivalent installation is:

```bash
brew install --cask docker
open -a Docker
```

Wait until Docker Desktop reports that its engine is running. On Linux, follow
Docker's distribution-specific Engine installation and
[post-installation guide](https://docs.docker.com/engine/install/linux-postinstall/);
the current user must be able to run Docker without `sudo`.

Verify the complete client/daemon path before starting an evaluation:

```bash
command -v docker
file "$(command -v docker)"
docker --version
docker info --format '{{.ServerVersion}}'
docker run --rm hello-world
```

On an Apple Silicon Mac, also verify the Linux `amd64` path used by many
SWE-bench images:

```bash
docker run --rm --platform linux/amd64 hello-world
```

`docker --version` must exit normally. A crash, architecture error, or legacy
client is an installation problem even if a file named `docker` exists on
`PATH`. `docker info` must print a server version; a client-only installation is
not sufficient.

### SWE-bench resources and architecture

The upstream SWE-bench guidance recommends an `x86_64` machine with 8 CPU
cores, 16 GB RAM, and roughly 120 GB of free Docker storage. These are capacity
recommendations rather than Yada parser requirements, but image preparation can
fail or exhaust disk below them. Configure Docker Desktop's virtual machine
resources accordingly.

Apple Silicon and other `arm64` hosts are experimental in SWE-bench. Yada
automatically disables the prebuilt `swebench` image namespace on Apple Silicon
and builds images locally, which is substantially slower and still requires
Docker to support the requested Linux platform.

See the [SWE-bench Docker setup guide](https://www.swebench.com/SWE-bench/guides/docker_setup/)
and [Evaluation lifecycle](evaluation.md#official-swe-bench---swebench-instance_id)
for the resource and container boundaries.

### Run Yada itself in Docker

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
