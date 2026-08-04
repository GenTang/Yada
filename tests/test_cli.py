from __future__ import annotations

from yada.run.cli import build_parser


def test_direct_cli_exposes_editing_strategy() -> None:
    parser = build_parser()

    default = parser.parse_args(["Fix it"])
    replace_first = parser.parse_args(
        ["Fix it", "--editing-strategy", "replace-first"]
    )

    assert default.editing_strategy == "patch-only"
    assert replace_first.editing_strategy == "replace-first"
