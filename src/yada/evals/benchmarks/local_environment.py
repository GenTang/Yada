"""Locked task-environment preparation for portable local cases."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def prepare_environment(
    value: object,
    *,
    base_dir: Path,
    source: Path,
) -> dict[str, str]:
    """Sync a case-specific uv project and prepare its source installation."""

    if value is None:
        return {}
    if not isinstance(value, dict) or value.get("type") != "uv":
        raise ValueError("manifest environment must have type='uv'")
    project = _resolve_path(base_dir, str(value.get("project", ".")))
    if not (project / "pyproject.toml").is_file():
        raise ValueError(f"uv project has no pyproject.toml: {project}")
    uv = str(value.get("executable", "uv"))
    sync_argv = [uv, "sync", "--project", str(project), "--locked"]
    requested_python = value.get("python")
    if requested_python is not None:
        if not isinstance(requested_python, str) or not requested_python.strip():
            raise ValueError("environment python must be a non-empty string")
        sync_argv.extend(["--python", requested_python])
    timeout = int(value.get("timeout_seconds", 900))
    sync = _run(sync_argv, base_dir, timeout=timeout)
    if sync.returncode:
        raise RuntimeError(sync.stderr.strip() or "uv sync failed")

    python = _venv_python(project / ".venv")
    setup_environment = os.environ.copy()
    configured_environment = value.get("setup_environment", {})
    if not isinstance(configured_environment, dict) or not all(
        isinstance(key, str) and isinstance(item, str)
        for key, item in configured_environment.items()
    ):
        raise ValueError("environment setup_environment must contain strings")
    setup_environment.update(configured_environment)

    install_mode = value.get("install_workspace", False)
    if install_mode is True or install_mode == "editable":
        install = _run(
            [
                uv,
                "pip",
                "install",
                "--python",
                str(python),
                "--no-deps",
                "--no-build-isolation",
                "-e",
                str(source),
            ],
            base_dir,
            timeout=timeout,
            environment=setup_environment,
        )
        error = "uv workspace install failed"
    elif install_mode == "legacy-editable":
        install = _run(
            [str(python), "setup.py", "develop", "--no-deps"],
            source,
            timeout=timeout,
            environment=setup_environment,
        )
        error = "legacy workspace install failed"
    elif install_mode in {False, None}:
        install = None
        error = ""
    else:
        raise ValueError(
            "environment install_workspace must be false, editable, "
            "or legacy-editable"
        )
    if install is not None and install.returncode:
        raise RuntimeError(install.stderr.strip() or error)
    if not python.is_file():
        raise RuntimeError(f"uv did not create the expected Python: {python}")
    return {
        "PATH": str(python.parent) + os.pathsep + os.environ.get("PATH", ""),
        "VIRTUAL_ENV": str(project / ".venv"),
    }


def workspace_environment(value: object, workspace: Path) -> dict[str, str]:
    """Resolve configured Python import roots inside the fresh workspace."""

    if not isinstance(value, dict):
        return {}
    pythonpath = value.get("pythonpath", [])
    if not isinstance(pythonpath, list) or not all(
        isinstance(item, str) and item for item in pythonpath
    ):
        raise ValueError("environment pythonpath must be an array of paths")
    paths: list[str] = []
    for item in pythonpath:
        relative = Path(item)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("environment pythonpath entries must stay in workspace")
        paths.append(str((workspace / relative).resolve()))
    if not paths:
        return {}
    inherited = os.environ.get("PYTHONPATH", "")
    if inherited:
        paths.append(inherited)
    return {"PYTHONPATH": os.pathsep.join(paths)}


def _resolve_path(base_dir: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return (path if path.is_absolute() else base_dir / path).resolve()


def _venv_python(venv: Path) -> Path:
    posix = venv / "bin" / "python"
    return posix if posix.exists() else venv / "Scripts" / "python.exe"


def _run(
    argv: list[str],
    cwd: Path,
    *,
    timeout: int,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )

