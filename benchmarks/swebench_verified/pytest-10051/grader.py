"""Local development grader for SWE-bench Verified pytest-10051."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BASE_COMMIT = "aa55975c7d3f6c9f6d7f68accc41bb7cadf0eb9a"
FAIL_TO_PASS = "testing/logging/test_fixture.py::test_clear_for_call_stage"
PASS_TO_PASS = (
    "testing/logging/test_fixture.py::test_change_level",
    "testing/logging/test_fixture.py::test_with_statement",
    "testing/logging/test_fixture.py::test_log_access",
    "testing/logging/test_fixture.py::test_messages",
    "testing/logging/test_fixture.py::test_record_tuples",
    "testing/logging/test_fixture.py::test_unicode",
    "testing/logging/test_fixture.py::test_clear",
    "testing/logging/test_fixture.py::test_caplog_captures_for_all_stages",
    "testing/logging/test_fixture.py::test_fixture_help",
    "testing/logging/test_fixture.py::test_change_level_undo",
    "testing/logging/test_fixture.py::test_change_level_undos_handler_level",
    "testing/logging/test_fixture.py::test_ini_controls_global_log_level",
    "testing/logging/test_fixture.py::test_caplog_can_override_global_log_level",
    "testing/logging/test_fixture.py::test_caplog_captures_despite_exception",
    "testing/logging/test_fixture.py::test_log_report_captures_according_to_config_option_upon_failure",
)


def run(
    argv: list[str],
    cwd: Path,
    *,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )


def grade(workspace: Path) -> int:
    if not (workspace / ".git").exists():
        print(f"SETUP FAIL: not a Git repository: {workspace}")
        return 2

    head = run(["git", "rev-parse", "HEAD"], workspace)
    if head.returncode or head.stdout.strip() != BASE_COMMIT:
        print(f"SETUP FAIL: expected HEAD {BASE_COMMIT}, got {head.stdout.strip()!r}")
        return 2

    test_patch = Path(__file__).with_name("test.patch")
    with tempfile.TemporaryDirectory(prefix="yada-pytest-10051-") as temp_dir:
        grading_workspace = Path(temp_dir) / "pytest"
        shutil.copytree(workspace, grading_workspace, symlinks=True)
        applied = run(["git", "apply", str(test_patch)], grading_workspace)
        if applied.returncode:
            print("SETUP FAIL: could not apply the official test patch")
            print(applied.stdout)
            return 2

        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(grading_workspace / "src")
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        result = run(
            [sys.executable, "-m", "pytest", "-q", FAIL_TO_PASS, *PASS_TO_PASS],
            grading_workspace,
            environment=environment,
        )
        print(result.stdout)
        if result.returncode == 0:
            print(
                "RESOLVED: 1 FAIL_TO_PASS and "
                f"{len(PASS_TO_PASS)} PASS_TO_PASS tests passed"
            )
        else:
            print("UNRESOLVED: one or more selected SWE-bench tests failed")
        return result.returncode


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("workspace", type=Path)
    args = parser.parse_args()
    raise SystemExit(grade(args.workspace.expanduser().resolve()))


if __name__ == "__main__":
    main()
