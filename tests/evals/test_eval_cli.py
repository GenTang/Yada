from __future__ import annotations

import pytest

from yada.evals.cli import _default_swebench_namespace, build_parser


def test_eval_cli_has_two_task_selectors() -> None:
    parser = build_parser()

    case = parser.parse_args(["--case", "case-dir"])
    swebench = parser.parse_args(["--swebench", "owner__repo-1"])

    assert str(case.case) == "case-dir"
    assert case.swebench is None
    assert swebench.case is None
    assert swebench.swebench == "owner__repo-1"


def test_eval_cli_rejects_multiple_task_selectors() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--case", "case-dir", "--swebench", "owner__repo-1"])


def test_swebench_namespace_is_automatic_on_apple_silicon(monkeypatch) -> None:
    monkeypatch.setattr("yada.evals.cli.sys.platform", "darwin")
    monkeypatch.setattr("yada.evals.cli.platform.machine", lambda: "arm64")

    assert _default_swebench_namespace() is None


def test_swebench_namespace_uses_prebuilt_images_elsewhere(monkeypatch) -> None:
    monkeypatch.setattr("yada.evals.cli.sys.platform", "linux")
    monkeypatch.setattr("yada.evals.cli.platform.machine", lambda: "x86_64")

    assert _default_swebench_namespace() == "swebench"
