"""Immutable benchmark metadata for Comparator targets and permitted axioms.

The sidecar identifies the objective; Lean's parser reads only the original
task. Candidate source is compiled and compared as saved, without assembly.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dvp.verification.lean import contains_placeholder, mask_lean_comments_and_strings


POLICY_VERSION = "fixed-source-context-v1"


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def preparation_fingerprint() -> str:
    """Identify how original task metadata becomes the Comparator specification."""
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


class FixedContextError(ValueError):
    def __init__(self, kind: str, message: str):
        super().__init__(f"{kind}: {message}")
        self.kind = kind


def load_contract_sidecar(dataset: Path, rows: list[dict[str, Any]], *, require_active: bool = False) -> dict[str, Any] | None:
    """Attach checked metadata without altering the input dataset or objective."""
    sidecar = dataset.with_suffix(".contract.json")
    activation = dataset.with_suffix(".contract.ready.json")
    if require_active and not activation.exists():
        # New metadata can be staged and validated without changing the policy
        # of a shell loop that starts another evaluation during preparation.
        return None
    if not sidecar.exists():
        if activation.exists():
            raise ValueError(f"Missing fixed-context sidecar: {sidecar}")
        return None
    raw = sidecar.read_bytes()
    if require_active:
        from dvp.verification.command_parser import parser_fingerprint
        ready = json.loads(activation.read_text(encoding="utf-8"))
        # Activation binds immutable dataset boundaries and parser identity.
        # Older records also identified an in-process guard; that guard no
        # longer participates in assembly or proof acceptance. Every new
        # final acceptance must independently pass comparator.
        if (ready.get("policy") != POLICY_VERSION
                or ready.get("sidecar_sha256") != hashlib.sha256(raw).hexdigest()
                or ready.get("parser_sha256") != parser_fingerprint()
                or ("dataset_sha256" in ready and ready["dataset_sha256"] != hashlib.sha256(dataset.read_bytes()).hexdigest())
                or ("preparation_sha256" in ready and ready["preparation_sha256"] != preparation_fingerprint())):
            raise ValueError(f"Fixed-context activation does not match the validated metadata/parser: {activation}")
    metadata = json.loads(raw)
    if metadata.get("schema_version") != "davinci.benchmark_contract.v1":
        raise ValueError(f"Unsupported fixed-context sidecar schema: {sidecar}")
    if metadata.get("dataset_sha256") != hashlib.sha256(dataset.read_bytes()).hexdigest():
        raise ValueError(f"Fixed-context dataset hash mismatch: {sidecar}")
    if metadata.get("offset_unit") != "python_unicode_codepoints":
        raise ValueError(f"Unsupported fixed-context offset unit: {sidecar}")
    tasks = metadata.get("tasks", [])
    by_id = {task["task_id"]: task for task in tasks}
    if len(by_id) != len(tasks) or len(rows) != len(tasks):
        raise ValueError(f"Fixed-context task coverage mismatch: {sidecar}")
    identity = {"policy": POLICY_VERSION, "path": str(sidecar), "sha256": hashlib.sha256(raw).hexdigest()}
    for row in rows:
        task = by_id.get(row.get("task_id"))
        if task is None:
            raise ValueError(f"Missing fixed-context task {row.get('task_id')}")
        statement = row["theorem_statement"]
        if sha256(statement) != task["statement_sha256"] or sha256(statement.strip()) != task["effective_statement_sha256"]:
            raise ValueError(f"Fixed-context objective hash mismatch: {row.get('task_id')}")
        row["_fixed_task_contract"] = task
        row["_fixed_contract_identity"] = identity
    return identity


@dataclass
class FixedTaskContract:
    statement: str
    metadata: dict[str, Any]
    source_identity: dict[str, Any] | None = None
    _commands: Any = field(default=None, init=False, repr=False)
    _trusted_syntax: tuple[str, ...] = field(default=(), init=False, repr=False)
    _commands_cache_identity: str | None = field(default=None, init=False, repr=False)
    source_cache_hit: bool = field(default=False, init=False)

    @classmethod
    def from_metadata(cls, statement: str, metadata: dict[str, Any], *, source_identity: dict[str, Any] | None = None) -> "FixedTaskContract":
        statement = statement.strip()
        if sha256(statement) != metadata["effective_statement_sha256"]:
            raise FixedContextError("contract_hash_mismatch", "The task text differs from its fixed source.")
        for key in ("trusted_header_span", "formal_statement_span", "footer_span", "target_signature_span", "target_proof_span"):
            start, end = _span(metadata[key])
            if not 0 <= start <= end <= len(statement):
                raise FixedContextError("invalid_contract_metadata", f"Invalid {key}.")
        header = _span(metadata["trusted_header_span"])
        formal = _span(metadata["formal_statement_span"])
        footer = _span(metadata["footer_span"])
        if not (header[0] == 0 and header[1] == formal[0] == _span(metadata["target_signature_span"])[0] and formal[1] == footer[0] and footer[1] == len(statement)):
            raise FixedContextError("invalid_contract_metadata", "The fixed regions must partition the complete objective.")
        for name in ("trusted_header", "formal_statement", "footer"):
            if name + "_sha256" in metadata and sha256(statement[slice(*_span(metadata[name + "_span"]))]) != metadata[name + "_sha256"]:
                raise FixedContextError("contract_hash_mismatch", f"The {name} slice differs from its recorded source.")
        if "contract_hash" in metadata:
            payload = {key: value for key, value in metadata.items() if key != "contract_hash"}
            if sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))) != metadata["contract_hash"]:
                raise FixedContextError("contract_hash_mismatch", "The task boundary metadata was changed.")
        return cls(statement, metadata, source_identity)

    @property
    def contract_hash(self) -> str:
        return sha256(POLICY_VERSION + "\n" + json.dumps(self.metadata, sort_keys=True, ensure_ascii=False, separators=(",", ":")))

    @property
    def target_name(self) -> str:
        return self.metadata["target_name"]

    @property
    def guidance(self) -> str:
        helpers = [slot["name"] for slot in self.metadata["proof_slots"] if slot["role"] == "helper"]
        extra = (" Also complete the original helper proofs: " + ", ".join(helpers) + ". Keep helper proof arguments local to their proof.") if helpers else ""
        return (
            f"The final objective is `{self.target_name}`. The benchmark's existing imports, definitions, axioms, "
            "instances, assumptions, and completed helper lemmas are fixed. Keep that context in the saved "
            "submission and fill the original proof holes. Comparator checks the saved source against the original task."
            + extra
        )

    @staticmethod
    def trusted_syntax_from_commands(commands: list[Any]) -> tuple[str, ...]:
        return tuple(
            c.source for c in commands
            if c.kind.endswith(".open") or re.match(r"\s*(?:(?:local|scoped)\s+)?(?:notation|infixl?|infixr|prefix|postfix)\b", mask_lean_comments_and_strings(c.source))
        )

    async def source_commands(self, target_workspace: Path, timeout: float = 3600) -> list[Any]:
        from dvp.verification.context_cache import cache_identity, digest, load_context, save_context
        from dvp.verification.parser_service import parse_commands
        identity = digest(cache_identity(self, target_workspace))
        if self._commands is None or self._commands_cache_identity != identity:
            cached = load_context(self, target_workspace)
            if cached is not None:
                self._commands, self._trusted_syntax = cached
                self._commands_cache_identity = identity
                self.source_cache_hit = True
                return self._commands
            initial = await parse_commands(self.statement, target_workspace, timeout=timeout, trusted_source=True)
            trusted_syntax = self.trusted_syntax_from_commands(initial)
            commands = (
                await parse_commands(self.statement, target_workspace, timeout=timeout, trusted_syntax=trusted_syntax)
                if trusted_syntax else initial
            )
            self._source_parts(commands)
            save_context(self, target_workspace, commands, trusted_syntax)
            self._commands, self._trusted_syntax = commands, trusted_syntax
            self._commands_cache_identity = identity
            self.source_cache_hit = False
        return self._commands

    async def validate(self, target_workspace: Path, timeout: float = 3600) -> None:
        commands = await self.source_commands(target_workspace, timeout)
        self._source_parts(commands)

    async def comparator_spec(self, target_workspace: Path, timeout: float = 3600) -> dict[str, list[str]]:
        """Obligations and allowed axioms derived only from the original source.

        All original proof holes are obligations, including helper theorems
        unused by the final target. Definition-hole tasks need a separate
        semantic policy and are deliberately unsupported here.
        """
        commands = await self.source_commands(target_workspace, timeout)
        target, helpers, fixed = self._source_parts(commands)
        obligations = [target, *helpers.values()]
        if any(command.decl_kind not in {"theorem", "lemma"} for command in obligations):
            raise FixedContextError("unsupported_definition_obligation", "Comparator acceptance requires theorem or lemma proof obligations.")
        axioms = ["propext", "Quot.sound", "Classical.choice"]
        axioms.extend(command.full_name for command in fixed if command.decl_kind == "axiom" and command.full_name)
        return {
            "theorem_names": list(dict.fromkeys(command.full_name for command in obligations)),
            "definition_names": [],
            "permitted_axioms": list(dict.fromkeys(axioms)),
        }

    def _source_parts(self, commands: list[Any]) -> tuple[Any, dict[str, Any], list[Any]]:
        target_start, _ = _span(self.metadata["target_signature_span"])
        targets = [c for c in commands if c.start <= target_start < c.end and c.decl_name]
        if len(targets) != 1:
            raise FixedContextError("unsupported_source_boundary", f"Cannot identify the explicit target {self.target_name}.")
        target = targets[0]
        if target.full_name != self.target_name or target.body_start is None:
            raise FixedContextError("unsupported_source_boundary", f"Parsed target disagrees with {self.target_name}.")
        helpers = {}
        for slot in self.metadata["proof_slots"]:
            if slot["role"] == "target":
                continue
            start, _ = _span(slot["signature_span"])
            matches = [c for c in commands if c.start <= start < c.end and c.decl_name]
            if len(matches) != 1 or matches[0].body_start is None:
                raise FixedContextError("unsupported_source_boundary", "Cannot identify an original helper proof.")
            helpers[matches[0].decl_name] = matches[0]
        after = [c for c in commands if c.start >= target.end]
        if any(_command_word(c.source) != "end" for c in after):
            raise FixedContextError("unsupported_source_boundary", "Non-context commands follow the final target.")
        obligations = {target.start, *(c.start for c in helpers.values())}
        if any(contains_placeholder(mask_lean_comments_and_strings(c.source)) and c.start not in obligations for c in commands):
            raise FixedContextError("unsupported_source_boundary", "A source placeholder is outside the recorded proof obligations.")
        return target, helpers, [c for c in commands if c.start < target.start or c in after]


def _span(value: Any) -> tuple[int, int]:
    if isinstance(value, dict):
        return int(value["start"]), int(value["end"])
    return int(value[0]), int(value[1])


def _command_word(source: str) -> str:
    words = mask_lean_comments_and_strings(source).strip().split()
    return words[0] if words else ""
