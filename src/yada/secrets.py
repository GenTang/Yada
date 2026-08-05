"""Resolve provider credentials without placing secret values on argv or disk logs."""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from pathlib import Path

_MAX_SECRET_BYTES = 4_096
_KEY_FILE_ENVIRONMENT = "DEEPSEEK_API_KEY_FILE"
_LEGACY_KEY_ENVIRONMENT = "DEEPSEEK_API_KEY"


class SecretConfigError(ValueError):
    """Raised when a configured secret source is missing or unsafe."""


def default_deepseek_api_key_path(
    environment: Mapping[str, str] | None = None,
) -> Path:
    """Return the platform-appropriate default credential-file path."""

    values = os.environ if environment is None else environment
    if os.name == "nt" and values.get("APPDATA"):
        return Path(values["APPDATA"]) / "Yada" / "deepseek_api_key"
    config_home = values.get("XDG_CONFIG_HOME")
    root = Path(config_home).expanduser() if config_home else Path.home() / ".config"
    return root / "yada" / "deepseek_api_key"


def load_deepseek_api_key(
    api_key_file: Path | None = None,
    *,
    environment: Mapping[str, str] | None = None,
) -> str:
    """Load a DeepSeek key from a private file, with an environment fallback.

    Precedence is an explicit ``--api-key-file``, ``DEEPSEEK_API_KEY_FILE``,
    the default user config file, then the legacy ``DEEPSEEK_API_KEY`` value.
    The value itself is never included in an exception.
    """

    values = os.environ if environment is None else environment
    configured_path: Path | None = None
    required_file = False
    if api_key_file is not None:
        configured_path = api_key_file
        required_file = True
    elif values.get(_KEY_FILE_ENVIRONMENT):
        configured_path = Path(values[_KEY_FILE_ENVIRONMENT])
        required_file = True
    else:
        default_path = default_deepseek_api_key_path(values)
        if default_path.is_file():
            configured_path = default_path

    if configured_path is not None:
        return _read_private_secret(configured_path, required=required_file)

    legacy_value = values.get(_LEGACY_KEY_ENVIRONMENT, "")
    if legacy_value:
        return _validate_secret_value(legacy_value, source=_LEGACY_KEY_ENVIRONMENT)

    default_path = default_deepseek_api_key_path(values)
    raise SecretConfigError(
        "DeepSeek API key not found; create the private file "
        f"{default_path}, pass --api-key-file, or set {_KEY_FILE_ENVIRONMENT}"
    )


def _read_private_secret(path: Path, *, required: bool) -> str:
    resolved = path.expanduser().resolve()
    try:
        metadata = resolved.stat()
    except OSError as exc:
        if required:
            raise SecretConfigError(
                f"cannot read DeepSeek API key file {resolved}: {exc}"
            ) from exc
        raise SecretConfigError(
            f"DeepSeek API key file is unavailable: {resolved}"
        ) from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise SecretConfigError(f"DeepSeek API key path is not a file: {resolved}")
    if os.name != "nt" and stat.S_IMODE(metadata.st_mode) & 0o077:
        raise SecretConfigError(
            f"DeepSeek API key file must not be accessible by group or others: "
            f"{resolved}; run chmod 600 {resolved}"
        )
    if metadata.st_size > _MAX_SECRET_BYTES:
        raise SecretConfigError(
            f"DeepSeek API key file is unexpectedly large: {resolved}"
        )
    try:
        value = resolved.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise SecretConfigError(
            f"cannot read DeepSeek API key file {resolved}: {exc}"
        ) from exc
    return _validate_secret_value(value, source=str(resolved))


def _validate_secret_value(value: str, *, source: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise SecretConfigError(f"DeepSeek API key source is empty: {source}")
    if "\n" in normalized or "\r" in normalized:
        raise SecretConfigError(
            f"DeepSeek API key source must contain exactly one line: {source}"
        )
    if "\x00" in normalized:
        raise SecretConfigError(
            f"DeepSeek API key source contains invalid data: {source}"
        )
    return normalized
