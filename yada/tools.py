"""Workspace tools and execution policy for Yada."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


PROTECTED_PARTS = {".git", ".yada"}
ALLOWED_EXECUTABLES = {
    "bash",
    "cargo",
    "go",
    "git",
    "make",
    "mypy",
    "node",
    "nox",
    "npm",
    "pnpm",
    "poetry",
    "pyright",
    "pytest",
    "python",
    "python3",
    "ruff",
    "sh",
    "tox",
    "uv",
    "yarn",
}
SAFE_GIT_SUBCOMMANDS = {
    "diff",
    "grep",
    "log",
    "ls-files",
    "rev-parse",
    "show",
    "status",
}


class ToolError(RuntimeError):
    pass


@dataclass(frozen=True)
class ToolExecution:
    data: dict[str, Any]
    finished: bool = False

    @property
    def content(self) -> str:
        return json.dumps(self.data, ensure_ascii=False)


class Workspace:
    def __init__(self, root: Path) -> None:
        resolved = root.expanduser().resolve()
        if not resolved.is_dir():
            raise ValueError(f"Workspace is not a directory: {resolved}")
        self.root = resolved

    def resolve(self, user_path: str, *, allow_missing: bool = False) -> Path:
        if not isinstance(user_path, str) or not user_path.strip():
            raise ToolError("path must be a non-empty string")
        raw = Path(user_path).expanduser()
        candidate = raw if raw.is_absolute() else self.root / raw
        resolved = candidate.resolve(strict=False)
        try:
            relative = resolved.relative_to(self.root)
        except ValueError as exc:
            raise ToolError(f"path escapes workspace: {user_path}") from exc
        if any(part in PROTECTED_PARTS for part in relative.parts):
            raise ToolError(f"access to protected path is denied: {user_path}")
        if not allow_missing and not resolved.exists():
            raise ToolError(f"path does not exist: {user_path}")
        return resolved

    def display(self, path: Path) -> str:
        relative = path.relative_to(self.root)
        return relative.as_posix() if relative.parts else "."

    @staticmethod
    def sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(128 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()


class CommandApprover:
    """Approval gate for commands. This is a guardrail, not an OS sandbox."""

    def __init__(
        self,
        mode: str = "ask",
        *,
        input_fn: Callable[[str], str] = input,
    ) -> None:
        if mode not in {"ask", "allow", "deny"}:
            raise ValueError("command policy must be ask, allow, or deny")
        self.mode = mode
        self.input_fn = input_fn
        self.allow_rest = mode == "allow"

    def approve(self, argv: list[str], cwd: str) -> bool:
        if self.mode == "deny":
            return False
        if self.allow_rest:
            return True
        rendered = shlex.join(argv)
        answer = self.input_fn(
            f"\nYada wants to run in {cwd}:\n  {rendered}\nAllow? [y/N/a=allow rest] "
        ).strip().lower()
        if answer == "a":
            self.allow_rest = True
            return True
        return answer in {"y", "yes"}


class ToolRunner:
    def __init__(
        self,
        workspace: Path,
        *,
        command_policy: str = "ask",
        command_timeout_seconds: int = 120,
        max_output_chars: int = 12_000,
        approver: CommandApprover | None = None,
    ) -> None:
        self.workspace = Workspace(workspace)
        self.approver = approver or CommandApprover(command_policy)
        self.command_timeout_seconds = command_timeout_seconds
        self.max_output_chars = max_output_chars
        self.revision = 0
        self.verified_revision = -1
        self.patch_count = 0
        self.touched_files: set[str] = set()
        self.successful_verifications: list[dict[str, Any]] = []

    @property
    def schemas(self) -> list[dict[str, Any]]:
        return TOOL_SCHEMAS

    def execute(self, name: str, arguments: dict[str, Any]) -> ToolExecution:
        try:
            if name == "search_code":
                data = self.search_code(**arguments)
            elif name == "read_file":
                data = self.read_file(**arguments)
            elif name == "apply_patch":
                data = self.apply_patch(**arguments)
            elif name == "run_command":
                data = self.run_command(**arguments)
            elif name == "finish":
                return self.finish(**arguments)
            else:
                raise ToolError(f"unknown tool: {name}")
            return ToolExecution({"ok": True, **data})
        except (ToolError, TypeError, ValueError) as exc:
            return ToolExecution({"ok": False, "error": str(exc)})

    def search_code(
        self,
        query: str,
        path: str = ".",
        file_glob: str | None = None,
        max_results: int = 80,
    ) -> dict[str, Any]:
        if not isinstance(query, str) or not query:
            raise ToolError("query must be a non-empty string")
        if not isinstance(max_results, int) or not 1 <= max_results <= 200:
            raise ToolError("max_results must be between 1 and 200")
        target = self.workspace.resolve(path)
        relative = self.workspace.display(target)
        rg = shutil.which("rg")
        if rg:
            argv = [
                rg,
                "--line-number",
                "--column",
                "--no-heading",
                "--color",
                "never",
                "--glob",
                "!.git/**",
                "--glob",
                "!.yada/**",
            ]
            if file_glob:
                argv.extend(["--glob", file_glob])
            argv.extend(["--", query, relative])
            try:
                result = subprocess.run(
                    argv,
                    cwd=self.workspace.root,
                    text=True,
                    capture_output=True,
                    timeout=20,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise ToolError("search timed out") from exc
            if result.returncode not in {0, 1}:
                raise ToolError(f"rg failed: {result.stderr.strip()[:1000]}")
            lines = result.stdout.splitlines()[:max_results]
        else:
            lines = self._python_search(query, target, file_glob, max_results)
        text, truncated = _truncate_text("\n".join(lines), self.max_output_chars)
        return {
            "query": query,
            "path": relative,
            "matches": text,
            "match_count_returned": len(lines),
            "truncated": truncated,
        }

    def _python_search(
        self,
        query: str,
        target: Path,
        file_glob: str | None,
        max_results: int,
    ) -> list[str]:
        try:
            pattern = re.compile(query)
        except re.error as exc:
            raise ToolError(f"invalid regular expression: {exc}") from exc
        files = [target] if target.is_file() else target.rglob("*")
        matches: list[str] = []
        for file_path in files:
            if len(matches) >= max_results:
                break
            if not file_path.is_file():
                continue
            relative = file_path.relative_to(self.workspace.root)
            if any(part in PROTECTED_PARTS for part in relative.parts):
                continue
            if file_glob and not fnmatch.fnmatch(relative.as_posix(), file_glob):
                continue
            try:
                raw = file_path.read_bytes()
            except OSError:
                continue
            if b"\0" in raw[:8192] or len(raw) > 2_000_000:
                continue
            for line_number, line in enumerate(
                raw.decode("utf-8", errors="replace").splitlines(), 1
            ):
                match = pattern.search(line)
                if match:
                    matches.append(
                        f"{relative.as_posix()}:{line_number}:{match.start() + 1}:{line}"
                    )
                    if len(matches) >= max_results:
                        break
        return matches

    def read_file(
        self,
        path: str,
        start_line: int = 1,
        end_line: int | None = None,
    ) -> dict[str, Any]:
        file_path = self.workspace.resolve(path)
        if not file_path.is_file():
            raise ToolError(f"not a file: {path}")
        size = file_path.stat().st_size
        if size > 1_000_000:
            raise ToolError("file is larger than the 1 MB read limit")
        if not isinstance(start_line, int) or start_line < 1:
            raise ToolError("start_line must be a positive integer")
        if end_line is None:
            end_line = start_line + 199
        if not isinstance(end_line, int) or end_line < start_line:
            raise ToolError("end_line must be greater than or equal to start_line")
        if end_line - start_line + 1 > 400:
            raise ToolError("a single read is limited to 400 lines")

        text = file_path.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        selected = lines[start_line - 1 : end_line]
        numbered = "\n".join(
            f"{number:>6}|{line}"
            for number, line in enumerate(selected, start=start_line)
        )
        return {
            "path": self.workspace.display(file_path),
            "sha256": self.workspace.sha256(file_path),
            "start_line": start_line,
            "end_line": start_line + len(selected) - 1 if selected else start_line - 1,
            "total_lines": len(lines),
            "content": numbered,
        }

    def apply_patch(
        self,
        patch: str,
        expected_files: list[dict[str, str]],
    ) -> dict[str, Any]:
        if not isinstance(patch, str) or not patch.strip():
            raise ToolError("patch must be a non-empty unified diff")
        if len(patch) > 250_000:
            raise ToolError("patch exceeds the 250 KB limit")
        touched = self._parse_patch_paths(patch)
        expected = self._normalize_expected_files(expected_files)
        if set(expected) != set(touched):
            raise ToolError(
                "expected_files must exactly match patch paths; "
                f"patch={sorted(touched)}, expected={sorted(expected)}"
            )

        for relative_path in sorted(touched):
            file_path = self.workspace.resolve(relative_path, allow_missing=True)
            declared = expected[relative_path]
            if file_path.exists():
                if not file_path.is_file():
                    raise ToolError(f"patch target is not a regular file: {relative_path}")
                actual = self.workspace.sha256(file_path)
                if declared != actual:
                    raise ToolError(
                        f"stale file hash for {relative_path}: expected {declared}, current {actual}"
                    )
            elif declared != "NEW":
                raise ToolError(
                    f"new file {relative_path} must use sha256 value NEW"
                )

        self._git_apply(patch, check_only=True)
        self._git_apply(patch, check_only=False)
        self.revision += 1
        self.patch_count += 1
        self.touched_files.update(touched)
        self.verified_revision = -1

        changed: list[dict[str, str]] = []
        for relative_path in sorted(touched):
            file_path = self.workspace.resolve(relative_path, allow_missing=True)
            changed.append(
                {
                    "path": relative_path,
                    "sha256": (
                        self.workspace.sha256(file_path) if file_path.exists() else "DELETED"
                    ),
                }
            )
        return {
            "revision": self.revision,
            "changed_files": changed,
            "message": "patch applied; run a relevant test before finish",
        }

    def _parse_patch_paths(self, patch: str) -> set[str]:
        forbidden_markers = (
            "GIT binary patch",
            "Binary files ",
            "rename from ",
            "rename to ",
            "copy from ",
            "copy to ",
            "old mode ",
            "new mode ",
            "new file mode 120000",
        )
        touched: set[str] = set()
        for line in patch.splitlines():
            if line.startswith(forbidden_markers):
                raise ToolError("binary, rename, copy, mode, and symlink patches are not supported")
            if not line.startswith("diff --git "):
                continue
            try:
                parts = shlex.split(line)
            except ValueError as exc:
                raise ToolError(f"invalid diff header: {line}") from exc
            if len(parts) != 4 or not parts[2].startswith("a/") or not parts[3].startswith("b/"):
                raise ToolError(f"unsupported diff header: {line}")
            old_path = parts[2][2:]
            new_path = parts[3][2:]
            if old_path != new_path:
                raise ToolError("renames are not supported in the minimal patch tool")
            normalized = self.workspace.display(
                self.workspace.resolve(new_path, allow_missing=True)
            )
            touched.add(normalized)
        if not touched:
            raise ToolError("patch must contain at least one 'diff --git a/... b/...' header")
        return touched

    def _normalize_expected_files(
        self, expected_files: list[dict[str, str]]
    ) -> dict[str, str]:
        if not isinstance(expected_files, list) or not expected_files:
            raise ToolError("expected_files must be a non-empty array")
        normalized: dict[str, str] = {}
        for item in expected_files:
            if not isinstance(item, dict):
                raise ToolError("each expected_files entry must be an object")
            path = item.get("path")
            sha256 = item.get("sha256")
            if not isinstance(path, str) or not isinstance(sha256, str):
                raise ToolError("expected_files entries require string path and sha256")
            relative = self.workspace.display(
                self.workspace.resolve(path, allow_missing=True)
            )
            if relative in normalized:
                raise ToolError(f"duplicate expected_files path: {relative}")
            if sha256 != "NEW" and not re.fullmatch(r"[0-9a-f]{64}", sha256):
                raise ToolError(f"invalid sha256 for {relative}")
            normalized[relative] = sha256
        return normalized

    def _git_apply(self, patch: str, *, check_only: bool) -> None:
        argv = ["git", "apply", "--whitespace=nowarn"]
        if check_only:
            argv.append("--check")
        argv.append("-")
        result = subprocess.run(
            argv,
            cwd=self.workspace.root,
            input=patch,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            phase = "check" if check_only else "apply"
            error = (result.stderr or result.stdout).strip()
            raise ToolError(f"git apply {phase} failed: {error[:2000]}")

    def run_command(
        self,
        argv: list[str],
        purpose: str,
        cwd: str = ".",
        timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        if purpose not in {"inspect", "test", "build"}:
            raise ToolError("purpose must be inspect, test, or build")
        if not isinstance(argv, list) or not argv or len(argv) > 40:
            raise ToolError("argv must be a non-empty array with at most 40 items")
        if not all(isinstance(item, str) and item and "\0" not in item for item in argv):
            raise ToolError("every argv item must be a non-empty string without NUL bytes")
        executable = argv[0]
        if Path(executable).name != executable or executable not in ALLOWED_EXECUTABLES:
            raise ToolError(
                f"executable is not allowed: {executable}; allowed={sorted(ALLOWED_EXECUTABLES)}"
            )
        if executable in {"bash", "sh"} and "-c" in argv[1:]:
            raise ToolError("shell -c is disabled; pass a script path and arguments instead")
        if executable == "git":
            if len(argv) < 2 or argv[1] not in SAFE_GIT_SUBCOMMANDS:
                raise ToolError(
                    f"only read-only git subcommands are allowed: {sorted(SAFE_GIT_SUBCOMMANDS)}"
                )

        command_cwd = self.workspace.resolve(cwd)
        if not command_cwd.is_dir():
            raise ToolError(f"command cwd is not a directory: {cwd}")
        display_cwd = self.workspace.display(command_cwd)
        if not self.approver.approve(argv, display_cwd):
            raise ToolError("command was denied by policy or user")

        effective_timeout = timeout_seconds or self.command_timeout_seconds
        if not isinstance(effective_timeout, int) or not 1 <= effective_timeout <= 1800:
            raise ToolError("timeout_seconds must be between 1 and 1800")
        env = _sanitized_environment()
        env["YADA_WORKSPACE"] = str(self.workspace.root)
        started = time.monotonic()
        try:
            result = subprocess.run(
                argv,
                cwd=command_cwd,
                env=env,
                text=True,
                capture_output=True,
                timeout=effective_timeout,
                check=False,
            )
            timed_out = False
            exit_code = result.returncode
            stdout = result.stdout
            stderr = result.stderr
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            exit_code = 124
            stdout = _timeout_text(exc.stdout)
            stderr = _timeout_text(exc.stderr) + f"\nTimed out after {effective_timeout}s"
        duration_ms = round((time.monotonic() - started) * 1000)
        stdout_text, stdout_truncated = _truncate_text(stdout, self.max_output_chars)
        stderr_text, stderr_truncated = _truncate_text(stderr, self.max_output_chars)

        if purpose in {"test", "build"} and exit_code == 0 and not timed_out:
            self.verified_revision = self.revision
            verification = {
                "argv": argv,
                "purpose": purpose,
                "revision": self.revision,
                "duration_ms": duration_ms,
            }
            self.successful_verifications.append(verification)

        return {
            "argv": argv,
            "cwd": display_cwd,
            "purpose": purpose,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "duration_ms": duration_ms,
            "stdout": stdout_text,
            "stderr": stderr_text,
            "truncated": stdout_truncated or stderr_truncated,
            "verified_revision": (
                self.verified_revision if self.verified_revision >= 0 else None
            ),
        }

    def finish(self, summary: str) -> ToolExecution:
        if not isinstance(summary, str) or not summary.strip():
            raise ToolError("summary must be a non-empty string")
        if self.patch_count == 0:
            raise ToolError("finish rejected: no patch has been applied")
        if self.verified_revision != self.revision:
            raise ToolError(
                "finish rejected: run a successful test or build after the latest patch"
            )
        diff_check = self._git_diff_check()
        if diff_check:
            raise ToolError(f"finish rejected by git diff --check: {diff_check}")
        state = self.final_state()
        return ToolExecution(
            {
                "ok": True,
                "status": "finished",
                "summary": summary.strip(),
                "revision": self.revision,
                "successful_verifications": self.successful_verifications,
                **state,
            },
            finished=True,
        )

    def _git_diff_check(self) -> str:
        if not (self.workspace.root / ".git").exists():
            return ""
        result = subprocess.run(
            ["git", "diff", "--check", "--"],
            cwd=self.workspace.root,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        return "" if result.returncode == 0 else (result.stdout + result.stderr).strip()

    def final_state(self) -> dict[str, Any]:
        if not (self.workspace.root / ".git").exists():
            return {"git_status": None, "diff_stat": None, "diff": None}
        commands = {
            "git_status": [
                "git",
                "status",
                "--short",
                "--",
                ".",
                ":(exclude).yada/**",
            ],
            "diff_stat": ["git", "diff", "--stat", "--"],
            "diff": ["git", "diff", "--", "."],
        }
        output: dict[str, Any] = {}
        for key, argv in commands.items():
            result = subprocess.run(
                argv,
                cwd=self.workspace.root,
                text=True,
                capture_output=True,
                timeout=20,
                check=False,
            )
            text, truncated = _truncate_text(result.stdout + result.stderr, 30_000)
            output[key] = text
            if truncated:
                output[f"{key}_truncated"] = True
        untracked_diffs: list[str] = []
        for relative_path in sorted(self.touched_files):
            file_path = self.workspace.resolve(relative_path, allow_missing=True)
            if not file_path.exists():
                continue
            tracked = subprocess.run(
                ["git", "ls-files", "--error-unmatch", "--", relative_path],
                cwd=self.workspace.root,
                text=True,
                capture_output=True,
                timeout=20,
                check=False,
            )
            if tracked.returncode == 0:
                continue
            diff = subprocess.run(
                ["git", "diff", "--no-index", "--", "/dev/null", relative_path],
                cwd=self.workspace.root,
                text=True,
                capture_output=True,
                timeout=20,
                check=False,
            )
            untracked_diffs.append(diff.stdout)
        if untracked_diffs:
            combined = (output.get("diff") or "") + "".join(untracked_diffs)
            output["diff"], output["diff_truncated"] = _truncate_text(combined, 30_000)
        return output


def _sanitized_environment() -> dict[str, str]:
    secret_markers = ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")
    return {
        key: value
        for key, value in os.environ.items()
        if not any(marker in key.upper() for marker in secret_markers)
    }


def _timeout_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value


def _truncate_text(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    head = limit * 2 // 3
    tail = limit - head
    omitted = len(text) - limit
    return (
        text[:head]
        + f"\n... <{omitted} characters omitted by Yada> ...\n"
        + text[-tail:],
        True,
    )


TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_code",
            "description": "Search text or regex in workspace files. Use this to locate symbols before reading.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Regex search pattern."},
                    "path": {"type": "string", "description": "Workspace-relative path; default '.'."},
                    "file_glob": {"type": "string", "description": "Optional glob such as '*.py'."},
                    "max_results": {"type": "integer", "description": "Maximum matches, 1-200."},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read up to 400 numbered lines and return the current SHA-256 for safe editing.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "apply_patch",
            "description": "Apply a git-style unified diff transactionally. Every touched existing file needs its read_file SHA-256; new files use NEW.",
            "parameters": {
                "type": "object",
                "properties": {
                    "patch": {"type": "string", "description": "Unified diff with diff --git headers."},
                    "expected_files": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string"},
                                "sha256": {"type": "string"},
                            },
                            "required": ["path", "sha256"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["patch", "expected_files"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run an argv array in the workspace without a shell. Label it inspect, test, or build. Commands require policy approval unless Yada runs with --yes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "argv": {"type": "array", "items": {"type": "string"}},
                    "purpose": {"type": "string", "enum": ["inspect", "test", "build"]},
                    "cwd": {"type": "string"},
                    "timeout_seconds": {"type": "integer"},
                },
                "required": ["argv", "purpose"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": "Submit the completed task. Rejected unless a patch exists and a relevant test/build passed after the latest patch.",
            "parameters": {
                "type": "object",
                "properties": {"summary": {"type": "string"}},
                "required": ["summary"],
                "additionalProperties": False,
            },
        },
    },
]
