from __future__ import annotations

import os
from pathlib import Path

import pytest

from yada.secrets import SecretConfigError, load_deepseek_api_key


def test_explicit_private_key_file_takes_precedence_over_environment(
    tmp_path: Path,
) -> None:
    path = tmp_path / "deepseek_api_key"
    path.write_text("file-secret\n", encoding="utf-8")
    path.chmod(0o600)

    value = load_deepseek_api_key(
        path,
        environment={"DEEPSEEK_API_KEY": "environment-secret"},
    )

    assert value == "file-secret"


def test_default_config_file_precedes_legacy_environment(tmp_path: Path) -> None:
    path = tmp_path / "yada" / "deepseek_api_key"
    path.parent.mkdir()
    path.write_text("config-secret\n", encoding="utf-8")
    path.chmod(0o600)

    value = load_deepseek_api_key(
        environment={
            "XDG_CONFIG_HOME": str(tmp_path),
            "DEEPSEEK_API_KEY": "legacy-secret",
        }
    )

    assert value == "config-secret"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are unavailable")
def test_key_file_rejects_group_or_other_access(tmp_path: Path) -> None:
    path = tmp_path / "deepseek_api_key"
    secret = "sk-must-not-appear-in-errors"
    path.write_text(secret + "\n", encoding="utf-8")
    path.chmod(0o644)

    with pytest.raises(SecretConfigError, match="chmod 600") as captured:
        load_deepseek_api_key(path, environment={})

    assert secret not in str(captured.value)


def test_configured_file_does_not_fall_back_when_missing(tmp_path: Path) -> None:
    missing = tmp_path / "missing"

    with pytest.raises(SecretConfigError, match="cannot read"):
        load_deepseek_api_key(
            missing,
            environment={"DEEPSEEK_API_KEY": "must-not-mask-file-errors"},
        )


def test_legacy_environment_remains_a_compatibility_fallback(tmp_path: Path) -> None:
    assert (
        load_deepseek_api_key(
            environment={
                "XDG_CONFIG_HOME": str(tmp_path),
                "DEEPSEEK_API_KEY": "legacy-secret",
            }
        )
        == "legacy-secret"
    )


def test_key_source_must_contain_exactly_one_nonempty_line(tmp_path: Path) -> None:
    path = tmp_path / "deepseek_api_key"
    path.write_text("first\nsecond\n", encoding="utf-8")
    path.chmod(0o600)

    with pytest.raises(SecretConfigError, match="exactly one line"):
        load_deepseek_api_key(path, environment={})
