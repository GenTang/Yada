from __future__ import annotations

from typing import Any

from yada.tools.schemas import TOOL_SCHEMAS


def _functions_by_name() -> dict[str, dict[str, Any]]:
    return {schema["function"]["name"]: schema["function"] for schema in TOOL_SCHEMAS}


def _assert_property_descriptions(schema: dict[str, Any]) -> None:
    for property_schema in schema.get("properties", {}).values():
        assert property_schema.get("description")
        items = property_schema.get("items")
        if isinstance(items, dict):
            _assert_property_descriptions(items)


def test_all_tool_parameters_describe_their_model_visible_semantics() -> None:
    for function in _functions_by_name().values():
        _assert_property_descriptions(function["parameters"])


def test_high_risk_tool_descriptions_expose_recovery_relevant_contracts() -> None:
    functions = _functions_by_name()

    search = functions["search_code"]
    assert "regular expression" in search["description"]
    assert (
        "escape regex metacharacters"
        in search["parameters"]["properties"]["query"]["description"]
    )

    patch = functions["apply_patch"]
    assert "counts are recalculated automatically" in patch["description"]
    assert "exactly every patch target" in patch["description"]
    assert "no extra or missing paths" in patch["description"]

    replace = functions["replace_text"]
    assert "Cannot create or delete files" in replace["description"]
    old_text = replace["parameters"]["properties"]["edits"]["items"]["properties"][
        "old_text"
    ]["description"]
    assert "whitespace and line breaks" in old_text
    assert "match exactly once" in old_text

    command = functions["run_command"]["parameters"]["properties"]
    assert "no shell parsing" in command["argv"]["description"]
    assert "verification gate" in command["purpose"]["description"]
    assert "Workspace-relative" in command["cwd"]["description"]
