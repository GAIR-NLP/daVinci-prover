"""Stable model-visible prompts for the proving policy."""

from __future__ import annotations

from typing import Any

_SYSTEM_PROMPT = """\
You are Davinci Prover. Prove the supplied theorem in Lean4 using the available tools.

Workspace:
- Submission.lean: preloaded verbatim with the supplied objective
- Submission/: writable helper modules and proof-development experiments
- target/: read-only Lean project used for verification
- Import local modules by path: Submission/Count.lean is `import Submission.Count`."""

_PROOF_REQUIREMENTS = """\
Proof and submission:
- Preserve the supplied imports, context, and exact target theorem declaration
  verbatim, including its name, parameters, hypotheses, and conclusion. Edit only
  the target proof body, even if a rewritten declaration would be equivalent.
  You may add auxiliary definitions, lemmas, and imported helper modules.
- Save the completed proof in Submission.lean and ensure its Lean check succeeds.
  Explicitly import any helper modules used. The submitted source includes those
  local modules and must contain no sorry/admit, added axioms, or unsafe declaration tricks.
- Do not use `native_decide`, `decide +native`, a
  `native := true` decision configuration, or compiler-trusted proof
  primitives. Kernel-checked `decide` and `bv_decide` remain allowed.
- Lean checks provide development feedback; they do not submit the proof or end the task.
- After finishing and checking the proof, save the files and end the task.
  The saved source files are checked independently afterward. The final chat reply is not graded."""

_WRITE_WORKFLOW = (
    "Consecutive file_write/file_edit calls in one response save in order; "
    "each distinct changed Lean destination is checked once before another tool runs "
    "or the response ends. Non-Lean writes are not checked. "
    "Use lean_verify to recheck a target after editing its imported helper modules."
)

# Custom system policies still receive the mandatory submission contract.
_ENVIRONMENT_CONTRACT = """\
Submission requirements:
Save the completed target proof in Submission.lean and ensure its Lean check succeeds.
Explicitly import any helper modules used. Submission.lean and its imported local
source files must be free of sorry/admit.
Lean checks provide development feedback; they do not submit the proof or end the task.
After finishing and checking the proof, save the files and end the task.
The saved source files are checked independently afterward. The final chat reply is not graded."""

_SELF_CHECK_GUIDANCE = """\
Before finishing, use lean_verify with `import Submission` followed by
`#print axioms <fully-qualified target theorem name>` to inspect dependencies.
Only `propext`, `Quot.sound`, `Classical.choice`, and axioms explicitly supplied
in the original task are permitted. Compilation success alone does not establish
a valid proof. Independent final checks cover statement equality, axiom
dependencies, and Lean kernel replay."""


def _build_user_prompt(theorem_statement: str) -> str:
    return f"""\
<objective>
Prove the following theorem in Lean4:

```lean
{theorem_statement}
```
</objective>"""


def initial_messages(
    theorem_statement: str,
    *,
    enable_lean_retrieve: bool = True,
    enable_python_execute: bool = False,
    system_prompt: str | None = None,
    user_prompt: str | None = None,
    task_contract: Any | None = None,
) -> list[dict[str, str]]:
    """Render the immutable policy contract and the supplied task context."""
    system = system_prompt or _SYSTEM_PROMPT
    if enable_lean_retrieve and not system_prompt:
        system += "\nlean_retrieve searches Lean declarations."
    if enable_python_execute:
        system = system.rstrip() + (
            "\n\npython_execute uses Submission/experiments/; files persist within this attempt."
        )
    requirements = _ENVIRONMENT_CONTRACT if system_prompt else _PROOF_REQUIREMENTS
    system = (
        f"{system.rstrip()}\n\n{requirements}\n\n{_SELF_CHECK_GUIDANCE}\n\n"
        f"Checking workflow:\n{_WRITE_WORKFLOW}"
    )
    user = user_prompt or _build_user_prompt(theorem_statement)
    if task_contract is not None:
        user += "\n\n" + task_contract.guidance
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
