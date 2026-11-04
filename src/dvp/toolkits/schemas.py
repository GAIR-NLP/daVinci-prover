"""Ordered model-visible tool schemas."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from collections.abc import Awaitable, Callable
from typing import Any


ToolHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass
class Tool:
    """One OpenAI-compatible function tool and its trusted handler."""

    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


# The complete set of tools available to the prover.
TOOL_NAMES = (
    "file_edit",
    "file_write",
    "file_read",
    "file_search",
    "content_search",
    "lean_retrieve",
    "lean_verify",
)

OPTIONAL_TOOL_NAMES = ("python_execute",)


def _object_schema(
    properties: dict[str, Any],
    required: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


def _exactly_one_schema(
    properties: dict[str, Any],
    first: str,
    second: str,
) -> dict[str, Any]:
    schema = _object_schema(properties)
    schema["oneOf"] = [
        {
            "required": [first],
            "properties": {
                first: {"type": "string"},
                second: {"type": "null"},
            },
        },
        {
            "required": [second],
            "properties": {
                first: {"type": "null"},
                second: {"type": "string"},
            },
        },
    ]
    return schema


def normalize_tool_name(name: str | None) -> str:
    """Canonicalize gateway prefixes to a fixed point."""

    normalized = str(name or "").strip()
    while True:
        for prefix in ("to=functions.", "functions."):
            if normalized.startswith(prefix):
                normalized = normalized[len(prefix):]
                break
        else:
            break
    if "<|" in normalized:
        normalized = normalized.split("<|", 1)[0]
    return "content_search" if normalized == "search" else normalized


def visible_schemas(
    sources: list[dict[str, Any]], *, enable_lean_retrieve: bool = True,
) -> list[dict[str, Any]]:
    """Return isolated, ordered schemas for the enabled capabilities."""
    by_name = {
        (source.get("function") or {}).get("name"): source
        for source in sources
    }
    schemas = []
    for name in (*TOOL_NAMES, *OPTIONAL_TOOL_NAMES):
        if name == "lean_retrieve" and not enable_lean_retrieve:
            continue
        if name not in by_name:
            continue
        item = deepcopy(by_name[name])
        if name == "content_search" and not enable_lean_retrieve:
            properties = (item["function"].get("parameters") or {}).get(
                "properties", {}
            )
            content_pattern = properties.get("content_pattern")
            if isinstance(content_pattern, dict):
                content_pattern["description"] = "Literal text or regex."
        schemas.append(item)
    return schemas


def build_tools(handlers: Any) -> dict[str, Tool]:
    tools: dict[str, Tool] = {}

    def add(tool: Tool) -> None:
        tools[tool.name] = tool

    edit_properties = {
        "file_path": {
            "type": "string",
            "minLength": 1,
            "description": "Source path. In a batch item, overrides the shared file_path; otherwise defaults to Submission.lean.",
            "default": "Submission.lean",
        },
        "new_str": {"type": "string", "description": "Replacement text; empty deletes the selection."},
        "old_str": {
            "type": ["string", "null"],
            "description": "Exact text to replace; replaces the first occurrence unless replace_all is true.",
            "default": None,
        },
        "old_line_range": {
            "type": ["array", "null"],
            "items": {"type": ["integer", "null"]},
            "description": "Optional [start, end) line range; takes priority over old_str.",
            "default": None,
        },
        "replace_all": {"type": "boolean", "default": False},
        "target_path": {
            "type": ["string", "null"],
            "description": "Optional output path: Submission.lean or a file under Submission/.",
            "default": None,
        },
    }
    edit_schema = _object_schema({
        **deepcopy(edit_properties),
        "edits": {
            "type": "array",
            "minItems": 1,
            "description": (
                "Ordered edits across one or more files. Later edits see earlier changes, "
                "including line-number changes. Top-level file_path is the default for each "
                "item; an item's file_path overrides it. Put all other edit arguments inside items."
            ),
            "items": _object_schema(deepcopy(edit_properties), ["new_str"]),
        },
    })
    edit_schema["properties"]["file_path"]["description"] = (
        "Source path for a single edit, or the default path for batch items without "
        "their own file_path. Defaults to Submission.lean."
    )
    # Nested items inherit the shared path at execution time, rather than a
    # schema default that could override it during client-side normalization.
    edit_schema["properties"]["edits"]["items"]["properties"]["file_path"].pop("default")
    edit_schema["oneOf"] = [
        {"required": ["new_str"], "not": {"required": ["edits"]}},
        {"required": ["edits"], "not": {
            "anyOf": [{"required": [name]} for name in edit_properties if name != "file_path"],
        }},
    ]
    add(Tool(
        "file_edit",
        (
            "Replace text or line ranges using one edit or an edits array. "
            "Write only to Submission.lean or under Submission/. "
            "Each call validates all its edits before saving. "
            "Consecutive file_edit/file_write calls in one response save in order, then "
            "automatically check each distinct changed Lean destination once with its imported "
            "local modules, before another tool runs or the response ends. Non-Lean files are not checked."
        ),
        edit_schema,
        handlers.file_edit,
    ))
    add(Tool(
        "file_write",
        (
            "Save a complete file to Submission.lean or under Submission/. Consecutive "
            "file_write/file_edit calls in one response share final automatic Lean checks, "
            "before another tool runs or the response ends. Non-Lean files are not checked."
        ),
        _object_schema({
            "file_path": {
                "type": "string",
                "description": "Writable path. Defaults to Submission.lean.",
                "default": "Submission.lean",
            },
            "content": {"type": "string", "description": "Complete file content; empty creates or clears a file."},
        }, ["content"]),
        handlers.file_write,
    ))
    add(Tool(
        "file_read",
        (
            "Read a workspace file with an optional 1-indexed line range. "
            "Results include line numbers and continuation ranges when paginated."
        ),
        _object_schema({
            "file_path": {
                "type": "string",
                "description": "Submission.lean, or a path under Submission/ or target/.",
            },
            "line_range": {
                "type": "array",
                "items": {"type": ["integer", "null"]},
                "description": "[start, end) with 1-indexed lines. Follow next_read when a response is paginated.",
                "default": [1, 200],
            },
            "start_column": {
                "type": "integer",
                "minimum": 0,
                "default": 0,
                "description": "Zero-based character offset in the first requested line, for continuing a long line via next_read.",
            },
        }, ["file_path"]),
        handlers.file_read,
    ))
    add(Tool(
        "file_search",
        "Find files by glob pattern inside a workspace path.",
        _object_schema({
            "directory_path": {
                "type": "string",
                "description": "Submission/, target/, or . for the workspace root.",
            },
            "pattern": {
                "type": "string",
                "description": "Glob such as *.lean or *Nat*.",
            },
            "recursive": {"type": "boolean", "default": True},
            "limit": {"type": "integer", "default": 50},
        }, ["directory_path", "pattern"]),
        handlers.file_search,
    ))
    add(Tool(
        "content_search",
        "Search workspace text or regex, returning multiple matches per file with numbered context ranges.",
        _object_schema({
            "path": {"type": "string", "description": "Workspace path to search."},
            "content_pattern": {
                "type": "string",
                "description": "Literal text or regex; use lean_retrieve for semantic Lean lookup.",
            },
            "use_regex": {"type": "boolean", "default": True},
            "recursive": {"type": "boolean", "default": True},
            "limit": {
                "type": "integer", "default": 20, "minimum": 1,
                "description": "Maximum number of matching files, not matching lines.",
            },
            "max_matches_per_file": {
                "type": "integer", "default": 10, "minimum": 1,
                "description": "Maximum matching lines per file; omitted matches are reported explicitly.",
            },
            "A": {
                "type": "integer", "default": 2, "minimum": 0,
                "description": "Context lines after each match.",
            },
            "B": {
                "type": "integer", "default": 2, "minimum": 0,
                "description": "Context lines before each match.",
            },
            "C": {
                "type": ["integer", "null"], "default": None, "minimum": 0,
                "description": "Context lines on both sides; when set, overrides A and B.",
            },
        }, ["path", "content_pattern"]),
        handlers.content_search,
    ))
    add(Tool(
        "lean_retrieve",
        "Search indexed Lean declarations by language, name, or keywords. Returns complete "
        "source signatures with lexical context, short summaries, and versioned source locations. "
        "Use lean_verify with #check for elaborated types; request proof source when needed.",
        _object_schema({
            "workspace": {
                "type": ["string", "null"],
                "description": "Currently target or omitted.",
                "default": None,
            },
            "natural_language_query": {
                "type": ["string", "null"],
                "description": "Natural-language description of a declaration.",
                "default": None,
            },
            "lean_name": {
                "type": ["string", "null"],
                "description": "Exact or partial Lean declaration name.",
                "default": None,
            },
            "keywords": {
                "type": ["string", "null"],
                "description": "Comma-separated technical keywords.",
                "default": None,
            },
            "limit": {
                "type": "integer", "default": 5,
                "description": "Maximum results; defaults to 5. Increase for a broader search.",
            },
            "include_def_proof": {"type": "boolean", "default": False},
            "include_theorem_proof": {
                "type": "boolean",
                "default": False,
            },
        }),
        handlers.lean_retrieve,
    ))

    # Keep one stationary check action. Profile selection is derived from
    # source content and is never a policy-controlled argument.
    tools["lean_verify"] = Tool(
        "lean_verify",
        (
            "Check one existing Lean file or inline code value and its local module dependencies. "
            "Import Submission/Count.lean as Submission.Count; nested modules are supported. "
            "To inspect the saved target, use inline code with import Submission followed by "
            "#print axioms <fully-qualified theorem name>. Reports compiler diagnostics; "
            "success refers to compilation. Code with sorry/admit is checked as a draft, "
            "including automatic write checks, and may report up to three local hole goals. "
            "Earlier compiler errors can prevent hole goals from being displayed."
        ),
        _exactly_one_schema(
            {
                "code": {
                    "type": ["string", "null"],
                    "description": (
                        "Lean code, optionally importing local modules."
                    ),
                    "default": None,
                },
                "file_path": {
                    "type": ["string", "null"],
                    "description": (
                        "Submission.lean or an existing Lean file under Submission/."
                    ),
                    "default": None,
                },
            },
            "code",
            "file_path",
        ),
        handlers.lean_verify,
    )
    executor = getattr(handlers, "python_executor", None)
    if executor is not None:
        allowed_packages = getattr(executor, "allowed_packages", ())
        if allowed_packages:
            python_capabilities = (
                " The Python standard library and only these third-party packages are available: "
                + ", ".join(allowed_packages)
                + ". Networking, subprocesses, and threads are unavailable."
            )
        elif getattr(executor, "stdlib_only", False):
            python_capabilities = (
                " Only the Python standard library is available (math, itertools, fractions, decimal); "
                "NumPy/SymPy, networking, subprocesses, and threads are unavailable."
            )
        else:
            python_capabilities = ""
        add(Tool(
            "python_execute",
            (
                "Run Python for numerical experiments, enumeration, and conjecture testing. "
                "Files are relative to Submission/experiments/ and persist within this attempt. "
                "Returns stdout, stderr, and exit status."
                + python_capabilities
            ),
            _object_schema({
                "code": {"type": "string", "description": "Python source to execute; at most 65,536 UTF-8 bytes."},
                "timeout": {"type": "number", "exclusiveMinimum": 0,
                            "maximum": executor.timeout,
                            "description": "Optional execution timeout in seconds; defaults to the configured maximum."},
            }, ["code"]),
            handlers.python_execute,
        ))
    return tools
