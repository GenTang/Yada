"""Text helpers shared by tools."""


def truncate_text(text: str, limit: int) -> tuple[str, bool]:
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
    if value is None:
        return ""
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value

