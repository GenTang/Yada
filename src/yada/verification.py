"""Host-enforced verification strategy state for one Yada run.

This module deliberately contains no model orchestration.  It is the deterministic
state machine shared by tools and the agent coordinator.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any

from yada.exceptions import ToolError


class VerificationStrategy(str, Enum):
    """The model-selected verification workflow for one run."""

    RED_GREEN = "red_green"
    DIRECT_EXECUTE = "direct_execute"


class VerificationPhase(str, Enum):
    """Host-owned phases.  Transitions are intentionally one-way except reverify."""

    AWAITING_STRATEGY = "awaiting_strategy"
    DIRECT = "direct"
    RED = "red"
    TEST_FROZEN = "test_frozen"
    FIX = "fix"
    GREEN = "green"
    REGRESSION_VERIFIED = "regression_verified"
    FINISHED = "finished"
    UNFINISHED = "unfinished"


@dataclass(frozen=True)
class WorkflowEvent:
    """A trace event emitted by a successful state transition."""

    name: str
    data: dict[str, Any]


@dataclass(frozen=True)
class FrozenTestEvidence:
    """Content-addressed test and Red evidence passed to the fresh Fix session."""

    baseline_revision: str
    patch: str
    patch_sha256: str
    files: tuple[str, ...]
    manifest: dict[str, str]
    target: str
    argv: tuple[str, ...]
    cwd: str
    command_fingerprint: str
    red_exit_code: int
    red_stdout: str
    red_stderr: str
    red_failure: dict[str, Any]

    def fix_context(self) -> dict[str, Any]:
        """Return only explicit artifacts that may enter the Fix conversation."""

        return {
            "baseline_revision": self.baseline_revision,
            "test_patch": self.patch,
            "test_patch_sha256": self.patch_sha256,
            "test_files": list(self.files),
            "target_test": self.target,
            "red_command": {"argv": list(self.argv), "cwd": self.cwd},
            "red_command_fingerprint": self.command_fingerprint,
            "red_exit_code": self.red_exit_code,
            "red_stdout": self.red_stdout,
            "red_stderr": self.red_stderr,
            "red_failure": self.red_failure,
        }


@dataclass
class VerificationWorkflow:
    """Mutable workflow state with fail-closed transition methods."""

    workspace: Path
    strategy: VerificationStrategy | None = None
    reason: str | None = None
    phase: VerificationPhase = VerificationPhase.AWAITING_STRATEGY
    baseline_revision: str | None = None
    red_test_paths: set[str] = field(default_factory=set)
    frozen_test: FrozenTestEvidence | None = None
    green_revision: int = -1
    regression_revision: int = -1
    production_patch_count: int = 0
    _events: list[WorkflowEvent] = field(default_factory=list, repr=False)

    def select(self, strategy: str, reason: str) -> dict[str, Any]:
        """Freeze the model-selected strategy before the first mutation."""

        if self.strategy is not None:
            raise ToolError(
                "verification strategy has already been selected and is irreversible",
                error_code="strategy_already_selected",
                details={"selected_strategy": self.strategy.value},
            )
        if not isinstance(reason, str) or not reason.strip():
            raise ToolError(
                "strategy reason must be a non-empty string",
                error_code="invalid_strategy_reason",
            )
        try:
            selected = VerificationStrategy(strategy)
        except (TypeError, ValueError) as exc:
            raise ToolError(
                "strategy must be red_green or direct_execute",
                error_code="invalid_strategy",
            ) from exc

        self.strategy = selected
        self.reason = reason.strip()
        self._emit(
            "strategy_selected",
            {"strategy": selected.value, "reason": self.reason},
        )
        if selected is VerificationStrategy.DIRECT_EXECUTE:
            self.phase = VerificationPhase.DIRECT
            return {"strategy": selected.value, "reason": self.reason}

        baseline = _git_head(self.workspace)
        dirty = _workspace_changes(self.workspace)
        if baseline is None or dirty:
            self.phase = VerificationPhase.UNFINISHED
            details = {
                "baseline_revision": baseline,
                "dirty_paths": dirty[:20],
            }
            self._emit("red_started", {**details, "status": "rejected"})
            raise ToolError(
                "red_green requires a clean Git workspace with a valid HEAD",
                error_code="baseline_unavailable",
                details=details,
            )
        self.baseline_revision = baseline
        self.phase = VerificationPhase.RED
        self._emit(
            "red_started",
            {"baseline_revision": baseline, "status": "started"},
        )
        return {
            "strategy": selected.value,
            "reason": self.reason,
            "baseline_revision": baseline,
        }

    def authorize_mutation(self, paths: set[str]) -> None:
        """Reject an edit transaction unless every path is allowed in this phase."""

        if self.phase is VerificationPhase.AWAITING_STRATEGY:
            raise ToolError(
                "select_strategy must succeed before editing files",
                error_code="strategy_required",
            )
        if self.phase is VerificationPhase.DIRECT:
            return
        if self.phase is VerificationPhase.RED:
            rejected = sorted(path for path in paths if not is_test_path(path))
            if rejected:
                raise ToolError(
                    "Red phase may modify only files classified as tests",
                    error_code="red_production_edit_rejected",
                    details={"paths": rejected[:20]},
                )
            return
        if self.phase in {
            VerificationPhase.FIX,
            VerificationPhase.GREEN,
            VerificationPhase.REGRESSION_VERIFIED,
        }:
            frozen = set(self.frozen_test.files if self.frozen_test else ())
            rejected = sorted(paths & frozen)
            if rejected:
                raise ToolError(
                    "Fix phase cannot modify frozen test files",
                    error_code="frozen_test_edit_rejected",
                    details={"paths": rejected[:20]},
                )
            return
        raise ToolError(
            f"file mutation is not allowed during phase {self.phase.value}",
            error_code="mutation_not_allowed",
        )

    def record_mutation(self, paths: set[str], revision: int) -> None:
        """Update phase evidence after one authorized patch transaction."""

        if self.phase is VerificationPhase.RED:
            self.red_test_paths.update(paths)
            return
        if self.phase in {
            VerificationPhase.FIX,
            VerificationPhase.GREEN,
            VerificationPhase.REGRESSION_VERIFIED,
        }:
            self.production_patch_count += 1
            self.green_revision = -1
            self.regression_revision = -1
            self.phase = VerificationPhase.FIX
            self._emit(
                "verification_invalidated",
                {"revision": revision, "paths": sorted(paths)},
            )

    def freeze_red_test(
        self,
        *,
        target: str,
        argv: list[str],
        cwd: str,
        command_result: dict[str, Any],
        pre_command_manifest: dict[str, str],
    ) -> FrozenTestEvidence:
        """Validate Red evidence, freeze the cumulative test patch, and end Red."""

        if self.phase is not VerificationPhase.RED:
            raise ToolError(
                "submit_red_test is available only during the Red phase",
                error_code="wrong_verification_phase",
                details={"phase": self.phase.value},
            )
        if not self.red_test_paths:
            raise ToolError(
                "submit_red_test requires a test patch",
                error_code="missing_test_patch",
            )
        changed = _workspace_changes(self.workspace)
        non_tests = sorted(path for path in changed if not is_test_path(path))
        if non_tests:
            self.phase = VerificationPhase.UNFINISHED
            observed = {
                "status": "red_production_change_detected",
                "target": target,
                "paths": non_tests[:20],
            }
            self._emit("red_observed", observed)
            raise ToolError(
                "Red command changed production files",
                error_code="red_production_change_detected",
                details=observed,
            )
        current_manifest = self.current_red_manifest()
        if current_manifest != pre_command_manifest:
            self.phase = VerificationPhase.UNFINISHED
            observed = {
                "status": "red_test_mutated_by_command",
                "target": target,
            }
            self._emit("red_observed", observed)
            raise ToolError(
                "Red command changed the proposed test patch",
                error_code="red_test_mutated_by_command",
                details=observed,
            )

        test_paths = tuple(sorted(set(changed) | self.red_test_paths))
        status, explanation = classify_red_observation(
            target=target,
            argv=argv,
            result=command_result,
            test_paths=test_paths,
        )
        red_failure = red_observation_details(
            result=command_result,
            test_paths=test_paths,
        )
        observed = {
            "status": status,
            "target": target,
            "exit_code": command_result.get("exit_code"),
            "explanation": explanation,
            **red_failure,
        }
        self._emit("red_observed", observed)
        if status != "valid":
            raise ToolError(
                f"invalid Red observation: {explanation}",
                error_code=status,
                details=observed,
            )

        patch = _collect_patch(self.workspace, test_paths)
        if not patch:
            raise ToolError(
                "Red test patch is empty",
                error_code="missing_test_patch",
            )
        manifest = _file_manifest(self.workspace, test_paths)
        fingerprint = command_fingerprint(argv, cwd)
        evidence = FrozenTestEvidence(
            baseline_revision=str(self.baseline_revision),
            patch=patch,
            patch_sha256=hashlib.sha256(patch.encode("utf-8")).hexdigest(),
            files=test_paths,
            manifest=manifest,
            target=target,
            argv=tuple(argv),
            cwd=normalize_cwd(cwd),
            command_fingerprint=fingerprint,
            red_exit_code=int(command_result["exit_code"]),
            red_stdout=str(command_result.get("stdout") or ""),
            red_stderr=str(command_result.get("stderr") or ""),
            red_failure=red_failure,
        )
        self.frozen_test = evidence
        self.phase = VerificationPhase.TEST_FROZEN
        self._emit(
            "test_frozen",
            {
                "baseline_revision": evidence.baseline_revision,
                "test_patch_sha256": evidence.patch_sha256,
                "test_files": list(evidence.files),
                "target": evidence.target,
                "command_fingerprint": evidence.command_fingerprint,
            },
        )
        return evidence

    def current_red_manifest(self) -> dict[str, str]:
        """Capture proposed test contents around an untrusted Red command."""

        return _file_manifest(self.workspace, tuple(sorted(self.red_test_paths)))

    def start_fix(self) -> dict[str, Any]:
        """Start the isolated Fix session from frozen explicit evidence."""

        if self.phase is not VerificationPhase.TEST_FROZEN or self.frozen_test is None:
            raise RuntimeError("Fix session requires frozen Red evidence")
        self.phase = VerificationPhase.FIX
        data = {
            "baseline_revision": self.frozen_test.baseline_revision,
            "test_patch_sha256": self.frozen_test.patch_sha256,
            "target": self.frozen_test.target,
        }
        self._emit("fix_started", data)
        return data

    def observe_fix_command(
        self,
        *,
        argv: list[str],
        cwd: str,
        purpose: str,
        verification_role: str | None,
        exit_code: int,
        timed_out: bool,
        revision: int,
    ) -> None:
        """Bind Green and regression evidence to the latest production revision."""

        if self.strategy is not VerificationStrategy.RED_GREEN:
            return
        if self.phase not in {
            VerificationPhase.FIX,
            VerificationPhase.GREEN,
            VerificationPhase.REGRESSION_VERIFIED,
        }:
            return
        if verification_role not in {None, "target", "regression"}:
            raise ToolError(
                "verification_role must be target or regression",
                error_code="invalid_verification_role",
            )
        if verification_role is None or exit_code != 0 or timed_out:
            return
        if purpose not in {"test", "build"}:
            raise ToolError(
                "verification roles require purpose test or build",
                error_code="invalid_verification_role",
            )
        if self.frozen_test is None:
            raise RuntimeError("Fix verification requires frozen test evidence")
        fingerprint = command_fingerprint(argv, cwd)
        if verification_role == "target":
            if fingerprint != self.frozen_test.command_fingerprint:
                raise ToolError(
                    "Green must use the exact frozen Red command",
                    error_code="green_command_mismatch",
                    details={
                        "expected_fingerprint": self.frozen_test.command_fingerprint,
                        "actual_fingerprint": fingerprint,
                    },
                )
            self._assert_frozen_tests()
            self.green_revision = revision
            self.regression_revision = -1
            self.phase = VerificationPhase.GREEN
            self._emit(
                "green_observed",
                {
                    "revision": revision,
                    "target": self.frozen_test.target,
                    "command_fingerprint": fingerprint,
                },
            )
            return
        if self.green_revision != revision:
            raise ToolError(
                "run the frozen target test successfully before regression verification",
                error_code="green_required",
            )
        if fingerprint == self.frozen_test.command_fingerprint:
            raise ToolError(
                "the frozen target command cannot also count as regression verification",
                error_code="distinct_regression_required",
            )
        self._assert_frozen_tests()
        self.regression_revision = revision
        self.phase = VerificationPhase.REGRESSION_VERIFIED
        self._emit(
            "regression_verified",
            {"revision": revision, "command_fingerprint": fingerprint},
        )

    def finish_error(self, revision: int) -> str | None:
        """Return the Red-Green completion rejection reason, if any."""

        if self.strategy is not VerificationStrategy.RED_GREEN:
            return None
        if self.phase is VerificationPhase.RED:
            return "Red phase cannot finish the overall task; submit a valid Red test"
        if self.frozen_test is None:
            return "valid frozen Red evidence is missing"
        self._assert_frozen_tests()
        if self.production_patch_count == 0:
            return "Fix phase has not applied a production patch"
        if self.green_revision != revision:
            return "the frozen target test has not passed on the latest revision"
        if self.regression_revision != revision:
            return "regression verification has not passed on the latest revision"
        if self.phase is not VerificationPhase.REGRESSION_VERIFIED:
            return "Red-Green workflow has not reached regression_verified"
        return None

    def accept_finish(self, revision: int) -> None:
        """Record the terminal transition after every finish gate passes."""

        self.phase = VerificationPhase.FINISHED
        self._emit(
            "finish_accepted",
            {
                "strategy": self.strategy.value if self.strategy else None,
                "revision": revision,
            },
        )

    def drain_events(self) -> tuple[WorkflowEvent, ...]:
        """Return and clear transition events for the trace adapter."""

        events = tuple(self._events)
        self._events.clear()
        return events

    def snapshot(self) -> dict[str, Any]:
        """Return bounded workflow state for run results and traces."""

        return {
            "strategy": self.strategy.value if self.strategy else None,
            "reason": self.reason,
            "phase": self.phase.value,
            "baseline_revision": self.baseline_revision,
            "test_patch_sha256": (
                self.frozen_test.patch_sha256 if self.frozen_test else None
            ),
            "target_test": self.frozen_test.target if self.frozen_test else None,
            "green_revision": self.green_revision if self.green_revision >= 0 else None,
            "regression_revision": (
                self.regression_revision if self.regression_revision >= 0 else None
            ),
        }

    def _assert_frozen_tests(self) -> None:
        if self.frozen_test is None:
            raise ToolError(
                "frozen test evidence is missing",
                error_code="frozen_test_missing",
            )
        current = _file_manifest(self.workspace, self.frozen_test.files)
        if current != self.frozen_test.manifest:
            raise ToolError(
                "frozen test files changed after the valid Red observation",
                error_code="frozen_test_changed",
                details={"expected": self.frozen_test.manifest, "actual": current},
            )

    def _emit(self, name: str, data: dict[str, Any]) -> None:
        self._events.append(WorkflowEvent(name, data))


def is_test_path(path: str) -> bool:
    """Conservatively classify common test paths without trusting the model."""

    pure = PurePosixPath(path)
    lowered_parts = tuple(part.casefold() for part in pure.parts)
    test_directories = {"test", "tests", "spec", "specs", "__tests__"}
    if any(part in test_directories for part in lowered_parts[:-1]):
        return True
    name = pure.name.casefold()
    if name in {"conftest.py"}:
        return True
    return bool(
        re.match(r"^test_.+", name)
        or re.match(r"^.+_test\.[^.]+$", name)
        or re.search(r"\.(?:test|spec)\.[^.]+$", name)
    )


def command_fingerprint(argv: list[str], cwd: str) -> str:
    """Hash a canonical exact command identity shared by Red and Green."""

    payload = json.dumps(
        {"argv": argv, "cwd": normalize_cwd(cwd)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def normalize_cwd(cwd: str) -> str:
    value = PurePosixPath(cwd or ".").as_posix()
    return "." if value in {"", "."} else value.rstrip("/")


def classify_red_observation(
    *,
    target: str,
    argv: list[str],
    result: dict[str, Any],
    test_paths: tuple[str, ...] = (),
) -> tuple[str, str]:
    """Accept only a structured, behavioral failure of the exact pytest target."""

    if not isinstance(target, str) or not target.strip():
        return "invalid_target_test", "target test identity is empty"
    if not _is_pytest_command(argv):
        return (
            "unsupported_red_command",
            "the initial Red observer supports direct pytest commands only",
        )
    if target not in argv:
        return "target_not_executed", "exact target identity is absent from argv"
    if result.get("timed_out"):
        return "red_timeout", "test command timed out"
    exit_code = result.get("exit_code")
    if exit_code is None:
        return "red_infrastructure_error", "command produced no exit code"
    if exit_code in {3, 4}:
        return (
            "red_infrastructure_error",
            f"pytest exited with infrastructure status {exit_code}",
        )

    observation = result.get("red_observation")
    if not isinstance(observation, dict) or observation.get("status") != "ok":
        observer_status = (
            observation.get("status") if isinstance(observation, dict) else "missing"
        )
        return (
            "red_observer_error",
            f"structured pytest observer report is {observer_status}",
        )
    events = observation.get("events")
    if not isinstance(events, list):
        return "red_observer_error", "structured pytest observer events are invalid"
    typed_events = [event for event in events if isinstance(event, dict)]
    if _normalize_nodeid(str(observation.get("target") or "")) != _normalize_nodeid(
        target
    ):
        return "red_observer_error", "structured pytest observer target is inconsistent"
    target_events = [
        event
        for event in typed_events
        if event.get("event") in {"target_collected", "target_report"}
    ]
    if any(
        _normalize_nodeid(str(event.get("nodeid") or "")) != _normalize_nodeid(target)
        for event in target_events
    ):
        return (
            "red_observer_error",
            "structured pytest observer node id is inconsistent",
        )
    if any(event.get("event") == "internal_error" for event in typed_events):
        return "red_infrastructure_error", "pytest reported an internal error"
    session_finishes = [
        event for event in typed_events if event.get("event") == "session_finish"
    ]
    if not session_finishes:
        return (
            "red_observer_error",
            "structured pytest observer report is incomplete",
        )
    if exit_code not in {event.get("exitstatus") for event in session_finishes}:
        return (
            "red_observer_error",
            "pytest process and structured observer exit statuses disagree",
        )

    reports = [event for event in typed_events if event.get("event") == "target_report"]
    collected = any(
        event.get("event") == "target_collected" for event in typed_events
    ) or bool(reports)
    collection_errors = [
        event for event in typed_events if event.get("event") == "collection_error"
    ]
    if not collected:
        if collection_errors:
            return _classify_collection_error(collection_errors)
        if exit_code == 2:
            return "red_infrastructure_error", "pytest was interrupted"
        return "target_not_collected", "the outer pytest session did not collect target"
    if not reports:
        return "target_not_executed", "target was collected but did not execute"

    phase_counts: dict[str, int] = {}
    for report in reports:
        when = str(report.get("when") or "unknown")
        phase_counts[when] = phase_counts.get(when, 0) + 1
    if any(count > 1 for count in phase_counts.values()):
        return (
            "red_ambiguous_result",
            "target produced repeated outcomes and is not a stable Red observation",
        )
    if any(report.get("wasxfail") for report in reports):
        return "red_skipped", "target was xfailed instead of failing normally"

    failed_reports = [report for report in reports if report.get("outcome") == "failed"]
    if len(failed_reports) > 1:
        return (
            "red_ambiguous_result",
            "target failed in multiple pytest phases",
        )
    if failed_reports:
        if exit_code != 1:
            return (
                "red_infrastructure_error",
                f"exit code {exit_code} is not a normal pytest failure status",
            )
        metadata = _failure_metadata(failed_reports[0], test_paths)
        if metadata["failure_kind"] in {
            "behavioral_assertion",
            "production_exception",
        }:
            return "valid", "target executed and failed behaviorally"
        return (
            "red_test_error",
            "target failed because of an uncaught error in test code",
        )

    if any(report.get("outcome") == "skipped" for report in reports):
        return "red_skipped", "target was skipped instead of failing"
    if any(report.get("outcome") == "passed" for report in reports):
        if exit_code == 0:
            return "red_not_failed", "target passed on baseline production code"
        return (
            "target_not_failed",
            "another test or pytest phase failed, not the submitted target",
        )
    return "target_not_executed", "target produced no recognized pytest outcome"


def _is_pytest_command(argv: list[str]) -> bool:
    if not argv:
        return False
    if argv[0] == "pytest":
        return True
    if len(argv) >= 3 and argv[0] in {"python", "python3"}:
        return argv[1:3] == ["-m", "pytest"]
    return len(argv) >= 3 and argv[:3] == ["uv", "run", "pytest"]


def _normalize_nodeid(value: str) -> str:
    normalized = value.replace("\\", "/")
    return normalized[2:] if normalized.startswith("./") else normalized


def red_observation_details(
    *, result: dict[str, Any], test_paths: tuple[str, ...]
) -> dict[str, Any]:
    """Return bounded failure metadata for traces and the fresh Fix context."""

    observation = result.get("red_observation")
    if not isinstance(observation, dict):
        return {}
    events = observation.get("events")
    if not isinstance(events, list):
        return {}
    failed = [
        event
        for event in events
        if isinstance(event, dict)
        and event.get("event") == "target_report"
        and event.get("outcome") == "failed"
    ]
    return _failure_metadata(failed[0], test_paths) if len(failed) == 1 else {}


def _failure_metadata(
    report: dict[str, Any], test_paths: tuple[str, ...]
) -> dict[str, Any]:
    exception_type = str(report.get("exception_type") or "")
    exception_name = exception_type.rsplit(".", 1)[-1]
    raw_paths = report.get("traceback_paths")
    traceback_paths = (
        [str(path).replace("\\", "/") for path in raw_paths if isinstance(path, str)]
        if isinstance(raw_paths, list)
        else []
    )
    normalized_tests = {path.removeprefix("./") for path in test_paths}
    production_paths = [
        path
        for path in traceback_paths
        if path.removeprefix("./") not in normalized_tests and not is_test_path(path)
    ]
    if exception_name in {"AssertionError", "Failed"}:
        failure_kind = "behavioral_assertion"
        failure_origin = "test_assertion"
    elif production_paths:
        failure_kind = "production_exception"
        failure_origin = production_paths[-1]
    else:
        failure_kind = "test_error"
        failure_origin = traceback_paths[-1] if traceback_paths else "unknown"
    return {
        "failure_kind": failure_kind,
        "failure_origin": failure_origin,
        "exception_type": exception_type or None,
        "failure_phase": report.get("when"),
    }


def _classify_collection_error(
    errors: list[dict[str, Any]],
) -> tuple[str, str]:
    combined = "\n".join(str(error.get("message") or "") for error in errors)
    lowered = combined.casefold()
    if "syntaxerror" in lowered:
        return "red_syntax_error", "target could not be collected because of syntax"
    if "modulenotfounderror" in lowered or "no module named" in lowered:
        return "red_dependency_error", "target collection is missing a dependency"
    if "importerror" in lowered:
        return "red_import_error", "target collection failed with an import error"
    return "red_collection_error", "target collection failed"


def _git_head(workspace: Path) -> str | None:
    result = _run(["git", "rev-parse", "HEAD"], workspace)
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def _workspace_changes(workspace: Path) -> list[str]:
    result = _run(
        [
            "git",
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--",
            ".",
            ":(exclude).yada/**",
        ],
        workspace,
    )
    if result.returncode:
        return ["<git-status-failed>"]
    paths: list[str] = []
    for line in result.stdout.splitlines():
        if len(line) < 4:
            continue
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        paths.append(path.strip('"'))
    return sorted(set(paths))


def _collect_patch(workspace: Path, paths: tuple[str, ...]) -> str:
    tracked = _run(["git", "diff", "--binary", "HEAD", "--", *paths], workspace)
    if tracked.returncode:
        raise ToolError(
            "could not collect frozen test patch",
            error_code="test_patch_collection_failed",
            details={"stderr": tracked.stderr[-1000:]},
        )
    patches = [tracked.stdout]
    for path in paths:
        candidate = workspace / path
        listed = _run(["git", "ls-files", "--error-unmatch", "--", path], workspace)
        if listed.returncode == 0 or not candidate.is_file():
            continue
        diff = _run(
            ["git", "diff", "--no-index", "--binary", "--", "/dev/null", path],
            workspace,
        )
        if diff.returncode not in {0, 1}:
            raise ToolError(
                f"could not diff untracked test file: {path}",
                error_code="test_patch_collection_failed",
            )
        patches.append(diff.stdout)
    return "".join(patches)


def _file_manifest(workspace: Path, paths: tuple[str, ...]) -> dict[str, str]:
    manifest: dict[str, str] = {}
    for path in paths:
        candidate = (workspace / path).resolve()
        try:
            candidate.relative_to(workspace.resolve())
        except ValueError as exc:
            raise ToolError(
                f"test path escapes workspace: {path}",
                error_code="invalid_test_path",
            ) from exc
        if not candidate.exists():
            manifest[path] = "DELETED"
            continue
        if not candidate.is_file():
            raise ToolError(
                f"test path is not a regular file: {path}",
                error_code="invalid_test_path",
            )
        manifest[path] = hashlib.sha256(candidate.read_bytes()).hexdigest()
    return manifest


def _run(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv,
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(argv, 1, "", str(exc))


__all__ = [
    "FrozenTestEvidence",
    "VerificationPhase",
    "VerificationStrategy",
    "VerificationWorkflow",
    "WorkflowEvent",
    "classify_red_observation",
    "command_fingerprint",
    "is_test_path",
]
