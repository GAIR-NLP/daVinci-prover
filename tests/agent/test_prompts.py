"""Factual prompt contracts without freezing editorial wording."""

import json
from types import SimpleNamespace

import pytest

from dvp.agent.prompts import initial_messages
from dvp.toolkits.schemas import TOOL_NAMES, build_tools, visible_schemas


THEOREM = "import Mathlib\n\ntheorem objective : True := by sorry\n"
INTERNAL_EVALUATION_TERMS = (
    "reward", "comparator", "strict_verified", "source_placeholders",
    "holes.items", "target_checked", "objective_complete", "_harness",
    "once per attempt", "recovery message", "ends the episode",
    "ends with", "partial proof score",
)


@pytest.mark.parametrize("retrieve", [True, False])
@pytest.mark.parametrize("python", [True, False])
def test_default_prompt_exposes_workspace_tools_and_proof_requirements(retrieve, python):
    messages = initial_messages(THEOREM, enable_lean_retrieve=retrieve, enable_python_execute=python)
    assert [message["role"] for message in messages] == ["system", "user"]
    system = " ".join(messages[0]["content"].split())
    for fact in (
        "Submission.lean", "Submission/", "target/", "import Submission.Count",
        "Consecutive file_write/file_edit calls in one response save in order",
        "each distinct changed Lean destination is checked once before another tool runs",
        "or the response ends", "Non-Lean writes are not checked",
        "recheck a target after editing its imported helper modules",
        "exact target theorem declaration verbatim", "name, parameters, hypotheses, and conclusion",
        "Edit only the target proof body", "equivalent", "auxiliary definitions, lemmas",
        "added axioms", "unsafe declaration tricks", "native_decide", "decide +native",
        "native := true", "`decide` and `bv_decide` remain allowed",
        "Save the completed proof", "ensure its Lean check succeeds",
        "Explicitly import any helper modules", "must contain no sorry/admit",
        "Lean checks provide development feedback",
        "they do not submit the proof or end the task",
        "After finishing and checking the proof, save the files and end the task",
        "saved source files are checked independently afterward",
        "final chat reply is not graded",
        "#print axioms <fully-qualified target theorem name>", "`import Submission`",
        "`propext`, `Quot.sound`, `Classical.choice`", "axioms explicitly supplied",
        "Compilation success alone does not establish a valid proof",
        "statement equality, axiom dependencies, and Lean kernel replay",
    ):
        assert fact in system
    assert ("lean_retrieve" in system) is retrieve
    assert ("python_execute" in system) is python
    assert f"```lean\n{THEOREM}\n```" in messages[1]["content"]
    for term in INTERNAL_EVALUATION_TERMS:
        assert term not in system.lower()


def test_default_prompt_does_not_prescribe_tool_order_or_budget_reasoning():
    prompt = " ".join(message["content"] for message in initial_messages(THEOREM)).lower()
    prompt = " ".join(prompt.split())
    for removed in (
        "first turn", "initial read", "do not call file_read", "never search",
        "exact-target lean check early", "one focused", "smaller probe", "lookup chain",
        "target_checked=false", "before unrelated", "before an unrelated",
        "first source range", "time budget", "turn budget", "output-token",
        "remaining time", "remaining tokens", "task limits",
        "only end your turn when you are sure", "keep going until",
    ):
        assert removed not in prompt


@pytest.mark.parametrize("retrieve", [True, False])
def test_tool_descriptions_explain_work_without_internal_evaluation_terms(retrieve):
    handlers = SimpleNamespace(
        **{name: object() for name in TOOL_NAMES},
    )
    schemas = visible_schemas(
        [tool.schema() for tool in build_tools(handlers).values()],
        enable_lean_retrieve=retrieve,
    )
    descriptions = json.dumps(schemas).lower()
    for term in INTERNAL_EVALUATION_TERMS:
        assert term not in descriptions
    by_name = {schema["function"]["name"]: schema["function"] for schema in schemas}
    assert ("lean_retrieve" in by_name) is retrieve
    for name in ("file_write", "file_edit"):
        assert "automatic" in by_name[name]["description"]
        assert "before another tool runs or the response ends" in by_name[name]["description"]
    assert "each distinct changed Lean destination once" in by_name["file_edit"]["description"]
    verify = by_name["lean_verify"]["description"]
    for fact in ("local module dependencies", "Submission.Count", "nested modules",
                 "compiler diagnostics", "local hole goals", "up to three", "Earlier compiler errors",
                 "import Submission followed by #print axioms", "success refers to compilation"):
        assert fact in verify


@pytest.mark.parametrize("custom", [False, True])
def test_fixed_context_guidance_is_appended_verbatim(custom):
    guidance = "Fixed imports, assumptions and helper proofs must be preserved."
    contract = SimpleNamespace(guidance=guidance)
    user_prompt = "Custom task context." if custom else None
    messages = initial_messages(THEOREM, user_prompt=user_prompt, task_contract=contract)
    expected = user_prompt or initial_messages(THEOREM)[1]["content"]
    assert messages[1]["content"] == expected + "\n\n" + guidance


def test_custom_prompt_injection_keeps_submission_requirements():
    messages = initial_messages(
        THEOREM, system_prompt="Custom system policy.\n", user_prompt="Custom objective.",
        enable_lean_retrieve=False,
    )
    system = messages[0]["content"]
    assert system.startswith("Custom system policy.\n\nSubmission requirements:")
    assert "Save the completed target proof in Submission.lean" in system
    assert "Use lean_verify to recheck a target" in system
    assert "source files must be free of sorry/admit" in system
    assert "Lean checks provide development feedback" in system
    assert "final chat reply is not graded" in system
    assert "#print axioms <fully-qualified target theorem name>" in system
    assert "axioms explicitly supplied" in system
    assert "You are Davinci" not in system
    assert messages[1] == {"role": "user", "content": "Custom objective."}
    for term in INTERNAL_EVALUATION_TERMS:
        assert term not in system.lower()
