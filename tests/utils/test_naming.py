from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from yada.evals.cli import _default_output_path
from yada.run.cli import _default_trace_path
from yada.utils.naming import next_available_run_name, readable_run_name, task_slug


def test_task_slug_keeps_meaningful_unicode_and_removes_unsafe_characters() -> None:
    assert task_slug("修复 parser 的边界问题，并运行测试") == (
        "修复-parser-的边界问题-并运行测试"
    )
    assert task_slug("  Fix path/to: parser?  ") == "fix-path-to-parser"
    assert task_slug("pytest-dev__pytest-10051") == "pytest-dev__pytest-10051"
    assert task_slug("CON") == "task-con"
    assert task_slug("***") == "task"


def test_task_slug_is_bounded() -> None:
    assert task_slug("one two three", max_length=7) == "one-two"
    with pytest.raises(ValueError, match="positive"):
        task_slug("task", max_length=0)


def test_readable_run_name_uses_local_wall_time_without_seconds() -> None:
    china_time = datetime(
        2026,
        8,
        2,
        20,
        26,
        26,
        123456,
        tzinfo=timezone(timedelta(hours=8)),
    )

    assert readable_run_name("Pytest 10051", now=china_time) == (
        "pytest-10051__2026-08-02_20-26"
    )


def test_next_available_run_name_numbers_related_output_collisions(
    tmp_path,
) -> None:
    base_name = "pytest-dev__pytest-10051__2026-08-03_10-09"
    (tmp_path / f"{base_name}.artifacts").mkdir()
    (tmp_path / f"{base_name}(1).json").write_text("{}", encoding="utf-8")

    assert (
        next_available_run_name(
            tmp_path,
            base_name,
            suffixes=(".json", ".artifacts"),
        )
        == f"{base_name}(2)"
    )


def test_next_available_run_name_requires_an_output_suffix(tmp_path) -> None:
    with pytest.raises(ValueError, match="at least one"):
        next_available_run_name(tmp_path, "run", suffixes=())


def test_default_eval_paths_number_an_existing_artifacts_directory(
    tmp_path, monkeypatch
) -> None:
    run_name = "pytest-dev__pytest-10051__2026-08-03_10-09"
    results = tmp_path / "eval-results"
    results.mkdir()
    (results / f"{run_name}.artifacts").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("yada.evals.cli.readable_run_name", lambda _: run_name)

    assert _default_output_path("case") == (
        Path("eval-results") / f"{run_name}(1).json"
    )


def test_default_trace_path_numbers_an_existing_trace(tmp_path, monkeypatch) -> None:
    run_name = "fix-parser__2026-08-03_10-09"
    directory = tmp_path / ".yada" / "runs"
    directory.mkdir(parents=True)
    (directory / f"{run_name}.jsonl").write_text("", encoding="utf-8")
    monkeypatch.setattr("yada.run.cli.readable_run_name", lambda _: run_name)

    assert _default_trace_path(tmp_path, "task") == (directory / f"{run_name}(1).jsonl")
