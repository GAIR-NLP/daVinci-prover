"""Content-addressed JSON cache of immutable source parsing, never proofs."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from dvp.verification.command_parser import LeanCommand, _ast_fingerprint, parser_fingerprint


SCHEMA = "davinci.fixed_source_cache.v1"
from dvp.runtime.paths import DATA_ROOT

DEFAULT_CACHE_ROOT = DATA_ROOT / "benchmark_context_cache"


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def implementation_fingerprint() -> str:
    files = (Path(__file__), Path(__file__).with_name("fixed_context.py"))
    return digest({p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in files})


def environment_fingerprint(workspace: Path) -> str:
    from dvp.verification.parser_service import environment_identity
    return digest(environment_identity(workspace.expanduser().resolve()))


def cache_identity(contract: Any, workspace: Path) -> dict[str, Any]:
    from dvp.verification.fixed_context import preparation_fingerprint
    identity = contract.source_identity or {}
    return {
        "schema": SCHEMA,
        "source_sha256": hashlib.sha256(contract.statement.encode()).hexdigest(),
        "contract_hash": contract.contract_hash,
        "sidecar_sha256": identity.get("sha256"),
        "parser_sha256": parser_fingerprint(),
        "preparation_sha256": preparation_fingerprint(),
        "adapter_sha256": implementation_fingerprint(),
        "environment_sha256": environment_fingerprint(workspace),
    }


def cache_root() -> Path:
    override = os.environ.get("DAVINCI_FIXED_CONTEXT_CACHE_DIR")
    return Path(override).expanduser().resolve() if override else DEFAULT_CACHE_ROOT


def cache_path(contract: Any, workspace: Path) -> Path:
    identity = cache_identity(contract, workspace)
    return cache_root() / identity["environment_sha256"] / (digest(identity) + ".json")


def _decode(statement: str, records: Any) -> list[LeanCommand]:
    if not isinstance(records, list) or not records:
        raise ValueError("Empty or invalid cached source commands")
    result = []
    previous_end = 0
    for record in records:
        record = dict(record)
        record["namespace"] = tuple(record["namespace"])
        record["decl_name_parts"] = tuple(record["decl_name_parts"])
        command = LeanCommand(**record)
        if not (previous_end <= command.start < command.end <= len(statement)):
            raise ValueError("Invalid cached source command span")
        if command.source != statement[command.start:command.end]:
            raise ValueError("Cached command differs from immutable source")
        if command.fingerprint != _ast_fingerprint(command.ast):
            raise ValueError("Cached command AST checksum mismatch")
        expected_signature = (_ast_fingerprint(command.signature_ast)
                              if command.signature_ast is not None else None)
        if command.signature_fingerprint != expected_signature:
            raise ValueError("Cached declaration signature checksum mismatch")
        for name in ("signature", "value", "body"):
            start, end = getattr(command, name + "_start"), getattr(command, name + "_end")
            # The parser records the beginning of a declaration such as an
            # axiom/structure, but has no := boundary at which to end its
            # signature. Preserve this legitimate partial source position.
            if (name == "signature" and start is not None and end is None
                    and command.value_start is None and command.body_start is None):
                if not command.start <= start <= command.end:
                    raise ValueError("Cached declaration start escapes source command")
                continue
            if (start is None) != (end is None):
                raise ValueError("Incomplete cached declaration span")
            if start is not None and not command.start <= start <= end <= command.end:
                raise ValueError("Cached declaration span escapes source command")
        previous_end = command.end
        result.append(command)
    return result


def load_context(contract: Any, workspace: Path) -> tuple[list[LeanCommand], tuple[str, ...]] | None:
    """A miss/corrupt record requires a real source parse; it never authorizes a proof."""
    identity = cache_identity(contract, workspace)
    path = cache_root() / identity["environment_sha256"] / (digest(identity) + ".json")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload["identity"] != identity or payload["payload_sha256"] != digest(payload["context"]):
            return None
        context = payload["context"]
        commands = _decode(contract.statement, context["commands"])
        syntax = tuple(context["trusted_syntax"])
        if syntax != contract.trusted_syntax_from_commands(commands):
            return None
        contract._source_parts(commands)
        return commands, syntax
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return None


def save_context(contract: Any, workspace: Path, commands: list[LeanCommand],
                 trusted_syntax: tuple[str, ...]) -> Path:
    """Publish only after source boundaries have passed, with atomic replacement."""
    contract._source_parts(commands)
    if tuple(trusted_syntax) != contract.trusted_syntax_from_commands(commands):
        raise ValueError("Trusted syntax must come from the immutable source")
    identity = cache_identity(contract, workspace)
    context = {"commands": [asdict(command) for command in commands],
               "trusted_syntax": list(trusted_syntax)}
    payload = {"identity": identity, "context": context, "payload_sha256": digest(context)}
    path = cache_root() / identity["environment_sha256"] / (digest(identity) + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".context-", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return path
