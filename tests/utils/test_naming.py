from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from yada.utils.naming import readable_run_name, task_slug


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


def test_readable_run_name_uses_utc_and_microseconds() -> None:
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
        "pytest-10051__2026-08-02_12-26-26.123456Z"
    )
