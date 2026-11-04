"""Conservative lexical context for a declaration in its pinned source file."""

from __future__ import annotations

import re
from typing import Any

from dvp.verification.lean import mask_lean_comments_and_strings


_PAIRS = {"(": ")", "[": "]", "{": "}", "⦃": "⦄", "⟨": "⟩"}
_CLOSERS = set(_PAIRS.values())
_IDENTIFIER = r"(?:«[^»]*»|[\w']+)(?:\.(?:«[^»]*»|[\w']+))*"
_IDENTIFIER_PART = re.compile(r"«[^»]*»|[\w']+")
_DECLARATION = re.compile(
    r"(theorem|lemma|def|abbrev|instance|axiom|constant|opaque|structure|class|inductive)"
    r"(?![\w'.])"
)
_MODIFIER = re.compile(
    r"(?:private|protected|public|noncomputable|unsafe|partial|nonrec|scoped|local)"
    r"(?![\w'.])"
)
_SCOPE = re.compile(
    r"^((?:@\[[^\]]*\]\s*)*(?:(?:noncomputable|public|private)\s+)*)"
    r"(namespace|section|end)\b"
)
_CONTEXT = re.compile(
    r"^(?:variables?|universes?|open|include|omit|set_option)\b"
    r"|^local\s+(?:notation|infixl?|infixr|prefix|postfix|attribute)\b"
)
_COMMAND = re.compile(
    r"^(?:@\[[^\]]*\]\s*)*"
    r"(?:(?:private|protected|public|noncomputable|unsafe|partial|scoped|local)\s+)*"
    r"(?:namespace|section|end|variables?|universes?|open|include|omit|set_option|"
    r"theorem|lemma|def|abbrev|instance|axiom|constant|opaque|structure|class|"
    r"inductive|example|mutual|attribute|notation|infixl?|infixr|prefix|postfix|"
    r"syntax|macro|elab|initialize|builtin_initialize|import|export)\b"
)
_NOTE = (
    "Lexical source context only, not an elaborated Lean type or a complete Lean "
    "parse. Custom commands, macros, and command-local `in` scopes may add context."
)


def _masked(source: str) -> str:
    """Mask comments, strings, and quoted identifiers without moving offsets."""
    preliminary = mask_lean_comments_and_strings(source)
    chars = list(source)
    # Mask identifiers before lexing again: a quote/comment marker inside «…»
    # belongs to the identifier and must not swallow the remaining declaration.
    for match in re.finditer(r"«[^»]*»", source):
        if preliminary[match.start()] != "«":
            continue
        for index in range(match.start(), match.end()):
            if chars[index] not in "\r\n":
                chars[index] = " "
    return mask_lean_comments_and_strings("".join(chars))


def _advance_depth(text: str, stack: list[str]) -> bool:
    valid = True
    for char in text:
        if char in _PAIRS:
            stack.append(_PAIRS[char])
        elif char in _CLOSERS:
            if stack and stack[-1] == char:
                stack.pop()
            else:
                valid = False
    return valid


def declaration_signature(source_text: str) -> str:
    """Keep the declaration header, stopping only at a lexical outer body.

    Binder defaults, comments, strings and quoted names cannot introduce body
    markers. This deliberately does not attempt to elaborate implicit binders.
    """
    masked = _masked(source_text)
    first = re.search(r"\S", masked)
    if first is None:
        return ""
    start = first.start()
    stack: list[str] = []
    line_start = start
    index = start
    while index < len(masked):
        char = masked[index]
        if not stack:
            if masked.startswith(":=", index):
                return source_text[start:index].strip()
            if masked.startswith("where", index):
                before = masked[index - 1] if index else ""
                after = masked[index + 5:index + 6]
                if not re.match(r"[\w'.]", before) and not re.match(r"[\w'.]", after):
                    return source_text[start:index].strip()
            if char == "|" and not masked[line_start:index].strip():
                return source_text[start:index].strip()
        _advance_depth(char, stack)
        if char == "\n":
            line_start = index + 1
        index += 1
    return source_text[start:].strip()


def _skip_trivia(source: str, index: int) -> int:
    """Skip whitespace and nested comments, preserving quoted identifier text."""
    while index < len(source):
        if source[index].isspace():
            index += 1
        elif source.startswith("--", index):
            newline = source.find("\n", index + 2)
            index = newline if newline >= 0 else len(source)
        elif source.startswith("/-", index):
            depth = 1
            index += 2
            while index < len(source) and depth:
                if source.startswith("/-", index):
                    depth += 1
                    index += 2
                elif source.startswith("-/", index):
                    depth -= 1
                    index += 2
                else:
                    index += 1
        else:
            break
    return index


def _balanced_end(masked: str, start: int) -> int | None:
    """Return the end of one balanced delimiter group, or fail conservatively."""
    stack: list[str] = []
    for index in range(start, len(masked)):
        if not _advance_depth(masked[index], stack):
            return None
        if not stack:
            return index + 1
    return None


def _name_parts(name: str) -> tuple[str, ...] | None:
    if not re.fullmatch(_IDENTIFIER, name):
        return None
    return tuple(
        part[1:-1] if part.startswith("«") else part
        for part in _IDENTIFIER_PART.findall(name)
    )


def declaration_evidence(
    source_text: str | None, fullname: str, *, namespace: str | None = None,
) -> dict[str, Any]:
    """Associate a source header with its reported Lean declaration identity.

    Generated declarations can share the source of their generator (for
    example, ``to_additive``). A matching lexical name is required before
    exposing a signature; a source header is still not an elaborated type.
    Namespace suffix matching permits ``Prime.dvd_mul`` for
    ``Nat.Prime.dvd_mul``. Supplying ``namespace`` additionally checks the
    complete lexical name; an empty string denotes the root namespace, while
    ``None`` means the namespace is unknown. No binders from that context are
    invented here.
    """
    result: dict[str, Any] = {
        "kind": "declaration",
        "signature": None,
        "signature_status": "unavailable",
        "source_declaration": None,
        "signature_error": None,
    }

    def unavailable(reason: str) -> dict[str, Any]:
        result["signature_error"] = (
            f"{reason} Use lean_verify with `#check {fullname}` to obtain the "
            "declaration's elaborated type."
        )
        return result

    if not isinstance(source_text, str) or not source_text.strip():
        return unavailable("Source text is missing; no declaration header is available.")

    masked = _masked(source_text)
    index = _skip_trivia(source_text, 0)
    # Attributes may contain nested brackets, defaults, comments, and strings.
    # They describe elaboration behavior rather than the declaration's type.
    while index < len(source_text):
        if source_text.startswith("@[", index):
            end = _balanced_end(masked, index + 1)
            if end is None:
                return unavailable("The source has an unfinished declaration attribute.")
            index = _skip_trivia(source_text, end)
            continue
        modifier = _MODIFIER.match(source_text, index)
        if modifier:
            index = _skip_trivia(source_text, modifier.end())
            continue
        break

    declaration = _DECLARATION.match(source_text, index)
    if declaration is None:
        return unavailable("The source does not start with a supported named declaration.")
    result["kind"] = declaration.group(1)
    header_start = index
    index = _skip_trivia(source_text, declaration.end())
    if result["kind"] == "class" and re.match(r"inductive(?![\w'.])", source_text[index:]):
        index = _skip_trivia(source_text, index + len("inductive"))
    if result["kind"] == "instance" and re.match(r"\(\s*priority\s*:=", masked[index:]):
        end = _balanced_end(masked, index)
        if end is None:
            return unavailable("The source instance has an unfinished priority annotation.")
        index = _skip_trivia(source_text, end)

    name_match = re.match(_IDENTIFIER, source_text[index:])
    if name_match is None:
        return unavailable("The source declaration has no explicit name to verify its identity.")
    source_name = name_match.group()
    name_end = index + name_match.end()
    following = source_text[name_end:]
    if source_name == "_":
        return unavailable("The source declaration has no explicit name to verify its identity.")
    if following and not (
        following[0].isspace() or following[0] in ":([{⦃"
        or following.startswith(("/-", "--", ".{"))
    ):
        return unavailable("The source declaration name cannot be read completely.")
    result["source_declaration"] = source_name
    source_parts = _name_parts(source_name)
    target_parts = _name_parts(fullname) if isinstance(fullname, str) else None
    if not target_parts:
        return unavailable("The reported declaration name cannot be verified lexically.")
    rooted = source_parts[0] == "_root_"
    if rooted:
        source_parts = source_parts[1:]
    if target_parts[0] == "_root_":
        target_parts = target_parts[1:]
    qualified_parts = source_parts
    if namespace is not None and not rooted:
        namespace_parts = _name_parts(namespace) if namespace else ()
        if namespace_parts is None:
            return unavailable("The lexical source namespace cannot be verified.")
        if namespace_parts and namespace_parts[0] == "_root_":
            namespace_parts = namespace_parts[1:]
        qualified_parts = namespace_parts + source_parts
    matches = bool(source_parts) and (
        target_parts == qualified_parts if rooted or namespace is not None
        else target_parts[-len(source_parts):] == source_parts
    )
    if not matches:
        result["signature_status"] = "name_mismatch"
        if namespace is not None and not rooted:
            return unavailable(
                f"Lexical source context places `{source_name}` in namespace "
                f"`{namespace or '_root_'}`, which does not match `{fullname}`; "
                "the source may belong to a generator or a different declaration. "
                "Its header is not a verified signature for the requested name."
            )
        return unavailable(
            f"Source declares `{source_name}`, not `{fullname}`; it may belong to a "
            "generator of the requested declaration. Its header is not the requested signature."
        )

    signature = declaration_signature(source_text[header_start:])
    signature_mask = _masked(signature)
    stack: list[str] = []
    for line_number, line in enumerate(signature_mask.splitlines()):
        stripped = line.lstrip()
        if line_number and not stack and (_COMMAND.match(stripped) or stripped.startswith("#")):
            return unavailable("The source header contains more than one Lean command.")
        if not _advance_depth(line, stack):
            return unavailable("The source declaration header has unbalanced delimiters.")
    if stack:
        return unavailable("The source declaration header has unbalanced delimiters.")
    result["signature"] = signature
    result["signature_status"] = "source_header"
    return result


def _scope_name(command: str, keyword_end: int) -> str:
    remainder = command[keyword_end:].lstrip()
    match = re.match(_IDENTIFIER, remainder)
    return match.group() if match else ""


def _namespace(scopes: list[dict[str, Any]]) -> str:
    namespace = ""
    for scope in scopes:
        if scope["kind"] != "namespace":
            continue
        name = scope["name"]
        if name.startswith("_root_."):
            namespace = name[len("_root_."):]
        else:
            namespace = ".".join(part for part in (namespace, name) if part)
    return namespace


def declaration_context(source_text: str, line_start: int) -> dict[str, Any]:
    """Report active lexical commands before a 1-based declaration line.

    Scope frames prevent variables and opens in closed sections from leaking.
    Indentation excludes tactic-body commands; balanced delimiters preserve
    multiline variable commands and exclude syntax quotations. Results always
    declare their incompleteness instead of claiming elaborated context.
    """
    lines = source_text.splitlines(keepends=True)
    if isinstance(line_start, bool) or not isinstance(line_start, int):
        raise ValueError("line_start must be a 1-based integer")
    if not 1 <= line_start <= len(lines) + 1:
        raise ValueError("line_start is outside the source file")
    prefix = "".join(lines[:line_start - 1])
    source_lines = prefix.splitlines(keepends=True)
    masked_lines = _masked(prefix).splitlines(keepends=True)
    scopes: list[dict[str, Any]] = []
    root_commands: list[str] = []
    warnings: list[str] = []
    delimiter_stack: list[str] = []
    body_indent: int | None = None
    index = 0
    while index < len(masked_lines):
        line = masked_lines[index]
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())
        if not stripped:
            index += 1
            continue
        eligible = not delimiter_stack and (body_indent is None or indent <= body_indent)
        scope_match = _SCOPE.match(stripped) if eligible else None
        context_match = _CONTEXT.match(stripped) if eligible else None
        if scope_match or context_match:
            body_indent = None
            end = index + 1
            command_stack: list[str] = []
            balanced = _advance_depth(line, command_stack)
            while end < len(masked_lines):
                following = masked_lines[end]
                next_text = following.strip()
                next_indent = len(following) - len(following.lstrip())
                if not command_stack and next_text:
                    binder_continuation = bool(context_match) and next_text[0] in _PAIRS
                    if _COMMAND.match(next_text) or (next_indent <= indent and not binder_continuation):
                        break
                balanced = _advance_depth(following, command_stack) and balanced
                end += 1
            # Preserve exact command text, excluding surrounding whitespace and
            # trailing comment-only lines that belong to the next declaration.
            content_end = end
            while content_end > index + 1 and not masked_lines[content_end - 1].strip():
                content_end -= 1
            command = "".join(source_lines[index:content_end]).strip()
            command_mask = "".join(masked_lines[index:content_end]).strip()
            if command_stack or not balanced:
                warnings.append(f"Unbalanced context command before line {index + 1}; omitted.")
            elif re.search(r"\bin\s*$", command_mask):
                warnings.append(f"Command-local `in` context at line {index + 1} was not reconstructed.")
            elif scope_match:
                kind = scope_match.group(2)
                original = source_lines[index].lstrip()
                name = _scope_name(original, scope_match.end())
                if kind == "end":
                    if scopes and (not name or name == scopes[-1]["name"]):
                        scopes.pop()
                    else:
                        warnings.append(f"Unmatched end at line {index + 1}; context may be incomplete.")
                else:
                    scopes.append({"kind": kind, "name": name, "commands": []})
                    if scope_match.group(1):
                        scopes[-1]["commands"].append(command)
            else:
                target = scopes[-1]["commands"] if scopes else root_commands
                target.append(command)
            index = end
            continue
        if eligible:
            # Later indented lines belong to this command's body. A following
            # command at the same/outer indentation starts a new command.
            body_indent = indent
        if not _advance_depth(line, delimiter_stack):
            warnings.append(f"Unbalanced delimiter at line {index + 1}; context may be incomplete.")
        index += 1
    if delimiter_stack:
        warnings.append("The declaration starts inside an unfinished delimiter; context may be incomplete.")
    return {
        "namespace": _namespace(scopes),
        "commands": root_commands + [command for scope in scopes for command in scope["commands"]],
        "scopes": [{"kind": scope["kind"], "name": scope["name"]} for scope in scopes],
        "method": "source_scan",
        "complete": False,
        "note": _NOTE,
        "warnings": list(dict.fromkeys(warnings)),
    }
