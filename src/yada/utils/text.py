"""Text helpers shared by tools."""


def truncate_text(text: str, limit: int) -> tuple[str, bool]:
    """Bound text while retaining both setup context and trailing error frames.

    Args:
        text: Potentially large command or diff output.
        limit: Maximum number of original characters to retain.

    Returns:
        The bounded text and whether content was omitted.
    """

    if len(text) <= limit:
        return text, False
    head = limit * 2 // 3
    tail = limit - head
    omitted = len(text) - limit
    return (
        text[:head]
        + f"\n... <{omitted} characters omitted by Yada> ...\n"
        + text[-tail:],
        True,
    )


def timeout_text(value: str | bytes | None) -> str:
    """Normalize optional timeout output from ``subprocess`` to text."""

    if value is None:
        return ""
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value
