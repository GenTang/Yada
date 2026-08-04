"""Run-level editing strategy shared by prompts, tools, and evaluations."""

from __future__ import annotations

from enum import Enum


class EditingStrategy(str, Enum):
    """Stable editing policy selected once for an Agent run."""

    PATCH_ONLY = "patch-only"
    REPLACE_FIRST = "replace-first"


DEFAULT_EDITING_STRATEGY = EditingStrategy.REPLACE_FIRST
EDITING_STRATEGY_CHOICES = tuple(strategy.value for strategy in EditingStrategy)


def parse_editing_strategy(
    value: EditingStrategy | str,
) -> EditingStrategy:
    """Normalize a public strategy value or raise a concise validation error."""

    if isinstance(value, EditingStrategy):
        return value
    try:
        return EditingStrategy(value)
    except ValueError as exc:
        choices = ", ".join(EDITING_STRATEGY_CHOICES)
        raise ValueError(f"editing_strategy must be one of: {choices}") from exc


__all__ = [
    "DEFAULT_EDITING_STRATEGY",
    "EDITING_STRATEGY_CHOICES",
    "EditingStrategy",
    "parse_editing_strategy",
]
