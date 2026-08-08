"""Host-owned structured observation for Red pytest commands."""

from __future__ import annotations

import json
import os
import shutil
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

from yada.environments.commands import WORKSPACE_PATH_PLACEHOLDER

_MAX_REPORT_BYTES = 64_000
_MAX_EVENTS = 64

_PYTEST_PLUGIN_SOURCE = r"""\
import json
import os

import pytest


_NONCE = os.environ.get("YADA_RED_OBSERVER_NONCE", "")
_REPORT = os.environ.get("YADA_RED_OBSERVER_REPORT", "")
_TARGET = os.environ.get("YADA_RED_OBSERVER_TARGET", "")
_WORKSPACE = os.path.realpath(os.environ.get("YADA_WORKSPACE", ""))
_EXPECTED_CWD = os.path.realpath(
    os.environ.get("YADA_RED_OBSERVER_CWD", os.getcwd())
)
_ACTIVE_CONFIGS = set()


def _config_directory(config):
    invocation = getattr(config, "invocation_params", None)
    directory = getattr(invocation, "dir", None)
    if directory is not None:
        return os.path.realpath(str(directory))
    return os.path.realpath(os.getcwd())


def _active_config(config):
    return id(config) in _ACTIVE_CONFIGS


def _normalize_nodeid(value):
    value = str(value or "").replace("\\", "/")
    return value[2:] if value.startswith("./") else value


def _is_target(nodeid):
    return _normalize_nodeid(nodeid) == _normalize_nodeid(_TARGET)


def _bounded(value, limit=2000):
    text = str(value or "")
    return text if len(text) <= limit else text[:limit] + "..."


def _workspace_path(value):
    try:
        candidate = os.path.realpath(str(value))
        if not _WORKSPACE:
            return None
        relative = os.path.relpath(candidate, _WORKSPACE)
        if relative == os.pardir or relative.startswith(os.pardir + os.sep):
            return None
        return relative.replace("\\", "/")
    except Exception:
        return None


def _write(event):
    if not _REPORT or not _NONCE:
        return
    try:
        payload = dict(event)
        payload["schema_version"] = 1
        payload["nonce"] = _NONCE
        encoded = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
        descriptor = os.open(_REPORT, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(descriptor, encoded)
        finally:
            os.close(descriptor)
    except Exception:
        # Observation must never alter the test result it is observing.
        pass


def pytest_configure(config):
    if _config_directory(config) == _EXPECTED_CWD:
        _ACTIVE_CONFIGS.add(id(config))


def pytest_collection_finish(session):
    if not _active_config(session.config):
        return
    for item in session.items:
        if _is_target(item.nodeid):
            _write({"event": "target_collected", "nodeid": item.nodeid})
            return


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_make_collect_report(collector):
    outcome = yield
    report = outcome.get_result()
    if _active_config(collector.config) and report.failed:
        _write(
            {
                "event": "collection_error",
                "nodeid": getattr(report, "nodeid", ""),
                "message": _bounded(getattr(report, "longrepr", "")),
            }
        )


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if not _active_config(item.config) or not _is_target(report.nodeid):
        return
    event = {
        "event": "target_report",
        "nodeid": report.nodeid,
        "when": report.when,
        "outcome": report.outcome,
        "wasxfail": bool(getattr(report, "wasxfail", False)),
    }
    excinfo = call.excinfo
    if excinfo is not None:
        exception_type = getattr(excinfo, "type", None)
        if exception_type is not None:
            event["exception_type"] = "%s.%s" % (
                getattr(exception_type, "__module__", ""),
                getattr(exception_type, "__name__", str(exception_type)),
            )
        event["message"] = _bounded(getattr(excinfo, "value", ""))
        paths = []
        try:
            for entry in excinfo.traceback:
                path = _workspace_path(entry.path)
                if path is not None and path not in paths:
                    paths.append(path)
                if len(paths) >= 32:
                    break
        except Exception:
            pass
        event["traceback_paths"] = paths
    _write(event)


def pytest_sessionfinish(session, exitstatus):
    if _active_config(session.config):
        _write({"event": "session_finish", "exitstatus": int(exitstatus)})


def pytest_unconfigure(config):
    _ACTIVE_CONFIGS.discard(id(config))
"""


@dataclass(frozen=True)
class RedObserverSession:
    """Materialized observer environment and its bounded report."""

    environment: dict[str, str]
    report_path: Path
    nonce: str
    target: str

    def read(self) -> dict[str, Any]:
        """Read and validate the observer JSONL without trusting command output."""

        if not self.report_path.is_file():
            return {
                "schema_version": 1,
                "status": "missing",
                "target": self.target,
                "events": [],
            }
        try:
            if self.report_path.stat().st_size > _MAX_REPORT_BYTES:
                return {
                    "schema_version": 1,
                    "status": "oversized",
                    "target": self.target,
                    "events": [],
                }
            events: list[dict[str, Any]] = []
            invalid = False
            for line in self.report_path.read_text(encoding="utf-8").splitlines():
                try:
                    event = json.loads(line)
                except (TypeError, ValueError):
                    invalid = True
                    continue
                if (
                    not isinstance(event, dict)
                    or event.get("schema_version") != 1
                    or event.get("nonce") != self.nonce
                ):
                    invalid = True
                    continue
                event.pop("nonce", None)
                events.append(event)
                if len(events) > _MAX_EVENTS:
                    invalid = True
                    break
        except OSError:
            return {
                "schema_version": 1,
                "status": "unreadable",
                "target": self.target,
                "events": [],
            }
        return {
            "schema_version": 1,
            "status": "invalid" if invalid or not events else "ok",
            "target": self.target,
            "events": events[:_MAX_EVENTS],
        }


@contextmanager
def observe_red_pytest(
    *,
    workspace: Path,
    command_cwd: Path,
    target: str,
    environment: Mapping[str, str],
) -> Iterator[RedObserverSession]:
    """Inject a temporary pytest plugin without changing the submitted command."""

    nonce = uuid.uuid4().hex
    module_name = f"_yada_red_observer_{nonce}"
    relative_root = Path(".yada") / "red-observer" / nonce
    observer_root = workspace / relative_root
    observer_root.mkdir(parents=True, mode=0o700)
    module_path = observer_root / f"{module_name}.py"
    module_path.write_text(_PYTEST_PLUGIN_SOURCE, encoding="utf-8")
    module_path.chmod(0o600)
    report_path = observer_root / "report.jsonl"

    workspace_value = WORKSPACE_PATH_PLACEHOLDER
    plugin_value = f"{workspace_value}/{relative_root.as_posix()}"
    cwd_relative = command_cwd.resolve().relative_to(workspace.resolve()).as_posix()
    cwd_value = (
        workspace_value if cwd_relative == "." else f"{workspace_value}/{cwd_relative}"
    )
    effective_environment = dict(environment)
    existing_pythonpath = effective_environment.get("PYTHONPATH")
    effective_environment["PYTHONPATH"] = os.pathsep.join(
        value for value in (plugin_value, existing_pythonpath) if value
    )
    existing_plugins = effective_environment.get("PYTEST_PLUGINS")
    effective_environment["PYTEST_PLUGINS"] = ",".join(
        value for value in (existing_plugins, module_name) if value
    )
    effective_environment.update(
        {
            "YADA_RED_OBSERVER_CWD": cwd_value,
            "YADA_RED_OBSERVER_NONCE": nonce,
            "YADA_RED_OBSERVER_REPORT": (
                f"{workspace_value}/{relative_root.as_posix()}/report.jsonl"
            ),
            "YADA_RED_OBSERVER_TARGET": target,
        }
    )
    session = RedObserverSession(
        environment=effective_environment,
        report_path=report_path,
        nonce=nonce,
        target=target,
    )
    try:
        yield session
    finally:
        shutil.rmtree(observer_root, ignore_errors=True)
        for parent in (observer_root.parent, observer_root.parent.parent):
            try:
                parent.rmdir()
            except OSError:
                break
