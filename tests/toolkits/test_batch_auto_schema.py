"""Batch automatic checks keep the existing edit argument contract."""

from types import SimpleNamespace

import pytest

from dvp.toolkits.schemas import TOOL_NAMES, build_tools, visible_schemas


def handlers(*, python=False):
    optional = {}
    if python:
        optional = {
            "python_executor": SimpleNamespace(timeout=60, allowed_packages=("numpy",)),
            "python_execute": object(),
        }
    return SimpleNamespace(
        **{name: object() for name in TOOL_NAMES}, **optional,
    )


def test_file_tools_describe_grouped_checks_and_use_registered_handlers():
    automatic_handlers = handlers()
    automatic = build_tools(automatic_handlers)
    for name in ("file_edit", "file_write"):
        assert automatic[name].handler is getattr(automatic_handlers, name)
    description = automatic["file_edit"].description
    for fact in (
        "one edit or an edits array", "Each call validates all its edits before saving",
        "Consecutive file_edit/file_write calls in one response save in order",
        "each distinct changed Lean destination once", "imported local modules",
        "before another tool runs or the response ends", "Non-Lean files are not checked",
    ):
        assert fact in description
    assert "share final automatic Lean checks" in automatic["file_write"].description


def test_batch_auto_accepts_single_and_coordinated_edits_without_mixed_arguments():
    jsonschema = pytest.importorskip("jsonschema")
    schema = build_tools(handlers())["file_edit"].parameters
    validator = jsonschema.Draft202012Validator(schema)
    for arguments in (
        {"new_str": "by trivial"},
        {"old_str": "sorry", "new_str": "trivial"},
        {"file_path": "Submission/Helper.lean", "edits": [
            {"old_str": "old", "new_str": "new"},
            {"file_path": "Submission.lean", "old_line_range": [4, 6], "new_str": ""},
            {"old_str": "new", "new_str": "copy", "target_path": "Submission/Copy.lean"},
        ]},
    ):
        validator.validate(arguments)
    for arguments in (
        {}, {"edits": []}, {"edits": [{"old_str": "sorry"}]},
        {"new_str": "trivial", "edits": [{"new_str": "trivial"}]},
        {"target_path": "Submission/Copy.lean", "edits": [{"new_str": "trivial"}]},
        {"edits": [{"new_str": "trivial", "file_path": ""}]},
    ):
        assert not validator.is_valid(arguments)


@pytest.mark.parametrize("retrieve", [False, True])
@pytest.mark.parametrize("python", [False, True])
def test_batch_auto_keeps_tool_order_other_schemas_and_optional_capabilities(retrieve, python):
    automatic = build_tools(handlers(python=python))
    assert tuple(automatic) == (*TOOL_NAMES, *(("python_execute",) if python else ()))
    visible = visible_schemas(
        [tool.schema() for tool in automatic.values()], enable_lean_retrieve=retrieve,
    )
    names = [schema["function"]["name"] for schema in visible]
    assert ("lean_retrieve" in names) is retrieve
    assert ("python_execute" in names) is python


def test_batch_auto_parameter_changes_do_not_leak_to_another_surface():
    first = build_tools(handlers())
    first["file_edit"].parameters["properties"]["edits"]["items"]["required"].append("old_str")
    later = build_tools(handlers())
    assert later["file_edit"].parameters["properties"]["edits"]["items"]["required"] == ["new_str"]
