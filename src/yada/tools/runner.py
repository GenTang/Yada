"""Composition root and dispatcher for Yada's tools."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from yada.editing import (
    DEFAULT_EDITING_STRATEGY,
    EditingStrategy,
    parse_editing_strategy,
)
from yada.environments import (
    CommandApprover,
    CommandExecutor,
    LocalCommandExecutor,
    VerificationWorkspaces,
    Workspace,
)
from yada.exceptions import ToolError
from yada.tools.base import ToolContext, ToolExecution
from yada.tools.command import run_command
from yada.tools.finish import final_state, finish_task
from yada.tools.patch import apply_patch
from yada.tools.read import read_file
from yada.tools.replace import replace_text
from yada.tools.schemas import TOOL_SCHEMAS
from yada.tools.search import search_code
from yada.verification import VerificationPhase, VerificationWorkflow

ToolHandler = Callable[..., dict[str, Any]]


class ToolRunner:
    """Own shared tool state and route model tool calls to small handlers."""

    def __init__(
        self,
        workspace: Path,
        *,
        command_policy: str = "ask",
        command_timeout_seconds: int = 120,
        max_output_chars: int = 12_000,
        command_environment: dict[str, str] | None = None,
        command_executor: CommandExecutor | None = None,
        approver: CommandApprover | None = None,
        editing_strategy: EditingStrategy | str = DEFAULT_EDITING_STRATEGY,
    ) -> None:
        self.editing_strategy = parse_editing_strategy(editing_strategy)
        workspace_boundary = Workspace(workspace)
        self._primary_workspace = workspace_boundary
        self._verification_workspaces = VerificationWorkspaces(workspace_boundary)
        self.context = ToolContext(
            workspace=workspace_boundary,
            workflow=VerificationWorkflow(workspace_boundary.root),
            approver=approver or CommandApprover(command_policy),
            command_timeout_seconds=command_timeout_seconds,
            max_output_chars=max_output_chars,
            command_environment=dict(command_environment or {}),
            command_executor=command_executor or LocalCommandExecutor(),
        )
        self._handlers: dict[str, ToolHandler] = {
            "search_code": search_code,
            "read_file": read_file,
            "apply_patch": apply_patch,
            "run_command": run_command,
        }
        if self.editing_strategy is EditingStrategy.REPLACE_FIRST:
            self._handlers["replace_text"] = replace_text
        self._schemas = [
            schema
            for schema in TOOL_SCHEMAS
            if (
                self.editing_strategy is EditingStrategy.REPLACE_FIRST
                or schema["function"]["name"] != "replace_text"
            )
        ]

    @property
    def workspace(self) -> Workspace:
        """Expose the workspace boundary used by all registered handlers."""

        return self.context.workspace

    @property
    def schemas(self) -> list[dict[str, Any]]:
        """Return the stable tool schemas sent with every model request."""

        return self._schemas

    @property
    def tool_names(self) -> tuple[str, ...]:
        """Return the frozen model-facing tool names for trace metadata."""

        return tuple(schema["function"]["name"] for schema in self._schemas)

    def execute(self, name: str, arguments: dict[str, Any]) -> ToolExecution:
        """Dispatch one tool call and normalize expected validation failures.

        Args:
            name: Tool name from the model response.
            arguments: Decoded JSON object for the selected handler.

        Returns:
            A model-safe result. Expected tool and argument errors become
            ``{"ok": false, ...}`` observations instead of escaping the loop.
        """

        try:
            if name == "select_strategy":
                return self._select_strategy(arguments)
            if name == "submit_red_test":
                return self._submit_red_test(arguments)
            if name == "finish_task":
                execution = finish_task(self.context, **arguments)
                return self._with_events(execution)
            if (
                name in {"apply_patch", "replace_text"}
                and self.context.workflow.phase is VerificationPhase.AWAITING_STRATEGY
            ):
                raise ToolError(
                    "select_strategy must succeed before editing files",
                    error_code="strategy_required",
                )
            if name == "run_command":
                self._authorize_command(arguments)
            handler = self._handlers.get(name)
            if handler is None:
                raise ToolError(f"unknown tool: {name}")
            if name == "run_command" and self.context.workflow.phase in {
                VerificationPhase.FIX,
                VerificationPhase.GREEN,
                VerificationPhase.REGRESSION_VERIFIED,
            }:
                data = self._run_fix_command(arguments)
            else:
                data = handler(self.context, **arguments)
            return ToolExecution(
                {"ok": True, **data}, events=self.context.workflow.drain_events()
            )
        except ToolError as exc:
            observation: dict[str, Any] = {"ok": False, "error": str(exc)}
            if exc.error_code is not None:
                observation["error_code"] = exc.error_code
            if exc.details is not None:
                observation["details"] = exc.details
            return ToolExecution(
                observation, events=self.context.workflow.drain_events()
            )
        except (TypeError, ValueError) as exc:
            return ToolExecution(
                {"ok": False, "error": str(exc)},
                events=self.context.workflow.drain_events(),
            )

    def _select_strategy(self, arguments: dict[str, Any]) -> ToolExecution:
        try:
            data = self.context.workflow.select(**arguments)
            if self.context.workflow.phase is VerificationPhase.RED:
                self._start_red_workspace()
            return ToolExecution(
                {"ok": True, **data}, events=self.context.workflow.drain_events()
            )
        except ToolError as exc:
            observation: dict[str, Any] = {"ok": False, "error": str(exc)}
            if exc.error_code is not None:
                observation["error_code"] = exc.error_code
            if exc.details is not None:
                observation["details"] = exc.details
            stop_reason = (
                "workflow_failed"
                if self.context.workflow.phase is VerificationPhase.UNFINISHED
                else None
            )
            return ToolExecution(
                observation,
                stop_reason=stop_reason,
                events=self.context.workflow.drain_events(),
            )

    def _submit_red_test(self, arguments: dict[str, Any]) -> ToolExecution:
        from yada.tools.command import run_command

        try:
            target = arguments.pop("target")
            argv = arguments.pop("argv")
            cwd = arguments.pop("cwd", ".")
            timeout_seconds = arguments.pop("timeout_seconds", None)
            if arguments:
                raise TypeError(f"unexpected arguments: {sorted(arguments)}")
            pre_command_manifest = self.context.workflow.current_red_manifest()
            result = run_command(
                self.context,
                argv=argv,
                purpose="inspect",
                cwd=cwd,
                timeout_seconds=timeout_seconds,
                _red_observation=True,
            )
            evidence = self.context.workflow.freeze_red_test(
                target=target,
                argv=argv,
                cwd=cwd,
                command_result=result,
                pre_command_manifest=pre_command_manifest,
            )
            self._materialize_frozen_test(evidence.patch)
            return ToolExecution(
                {
                    "ok": True,
                    **result,
                    "status": "test_frozen",
                    "test_patch_sha256": evidence.patch_sha256,
                    "test_files": list(evidence.files),
                    "target": evidence.target,
                    "command_fingerprint": evidence.command_fingerprint,
                },
                stop_reason="test_frozen",
                events=self.context.workflow.drain_events(),
            )
        except (KeyError, ToolError, TypeError, ValueError) as exc:
            if isinstance(exc, KeyError):
                exc = TypeError(f"missing required argument: {exc.args[0]}")
            observation: dict[str, Any] = {"ok": False, "error": str(exc)}
            if isinstance(exc, ToolError):
                if exc.error_code is not None:
                    observation["error_code"] = exc.error_code
                if exc.details is not None:
                    observation["details"] = exc.details
            stop_reason = (
                "workflow_failed"
                if self.context.workflow.phase is VerificationPhase.UNFINISHED
                else None
            )
            return ToolExecution(
                observation,
                stop_reason=stop_reason,
                events=self.context.workflow.drain_events(),
            )

    def _authorize_command(self, arguments: dict[str, Any]) -> None:
        phase = self.context.workflow.phase
        purpose = arguments.get("purpose")
        argv = arguments.get("argv")
        if phase is VerificationPhase.AWAITING_STRATEGY:
            if (
                purpose != "inspect"
                or not isinstance(argv, list)
                or len(argv) < 2
                or argv[0] != "git"
            ):
                raise ToolError(
                    "before strategy selection, run_command permits only read-only Git inspection",
                    error_code="strategy_required",
                )
        if phase is VerificationPhase.RED and purpose in {"test", "build"}:
            raise ToolError(
                "use submit_red_test for Host-observed Red execution",
                error_code="submit_red_test_required",
            )

    def _with_events(self, execution: ToolExecution) -> ToolExecution:
        return ToolExecution(
            execution.data,
            finished=execution.finished,
            stop_reason=execution.stop_reason,
            events=execution.events + self.context.workflow.drain_events(),
        )

    def _start_red_workspace(self) -> None:
        """Move the current session into a disposable baseline Git worktree."""

        baseline = self.context.workflow.baseline_revision
        if baseline is None:
            raise ToolError(
                "cannot create Red workspace without a baseline revision",
                error_code="baseline_unavailable",
            )
        try:
            red_workspace = self._verification_workspaces.start_red(baseline)
        except ToolError:
            self.context.workflow.phase = VerificationPhase.UNFINISHED
            raise
        self.context.command_executor.close()
        self.context.workspace = red_workspace
        self.context.workflow.workspace = red_workspace.root

    def _run_fix_command(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Run Fix commands in a disposable copy of the latest candidate revision."""

        from yada.tools.command import run_command

        baseline = self.context.workflow.baseline_revision
        if baseline is None:
            raise ToolError(
                "Fix command requires a frozen baseline revision",
                error_code="baseline_unavailable",
            )
        with self._verification_workspaces.fix_command(baseline) as (
            command_workspace,
            candidate_patch,
        ):
            self.context.command_executor.close()
            try:
                self.context.workspace = command_workspace
                self.context.workflow.workspace = command_workspace.root
                result = run_command(
                    self.context,
                    **arguments,
                    _observe_workflow=False,
                )
                if (
                    self._verification_workspaces.collect_patch(command_workspace.root)
                    != candidate_patch
                ):
                    raise ToolError(
                        "verification command modified candidate source files",
                        error_code="verification_command_mutated_workspace",
                    )
                self.context.workflow.observe_fix_command(
                    argv=list(result["argv"]),
                    cwd=str(result["cwd"]),
                    purpose=str(result["purpose"]),
                    verification_role=result.get("verification_role"),
                    exit_code=int(result["exit_code"]),
                    timed_out=bool(result["timed_out"]),
                    revision=self.context.state.revision,
                )
                return result
            finally:
                self.context.command_executor.close()
                self.context.workspace = self._primary_workspace
                self.context.workflow.workspace = self._primary_workspace.root

    def _materialize_frozen_test(self, patch: str) -> None:
        """Apply a valid frozen test patch to the canonical Fix workspace."""

        try:
            self._verification_workspaces.materialize_test(patch)
        except ToolError as exc:
            self.context.workflow.phase = VerificationPhase.UNFINISHED
            if exc.error_code == "verification_workspace_failed":
                raise ToolError(
                    "could not materialize the frozen test patch",
                    error_code="test_materialization_failed",
                    details=exc.details,
                ) from exc
            raise
        self.context.command_executor.close()
        self.context.workspace = self._primary_workspace
        self.context.workflow.workspace = self._primary_workspace.root

    def final_state(self) -> dict[str, Any]:
        """Collect the bounded Git status and diff used in the final result."""

        return {
            **final_state(self.context),
            "verification_workflow": self.context.workflow.snapshot(),
        }

    def close(self) -> None:
        """Release resources owned by the configured command backend."""

        self.context.command_executor.close()
        self._verification_workspaces.close()
