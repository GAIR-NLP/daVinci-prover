"""Workspace file reads and validated writes with grouped automatic Lean checks."""

from __future__ import annotations

import os
import stat
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from dvp.runtime.submission import SUBMISSION_PATH
from dvp.toolkits import lean

if TYPE_CHECKING:
    from dvp.toolkits.registry import ToolRegistry


class SavedFileVerificationError(RuntimeError):
    """A verifier exception after a completed save, retaining mutation metadata."""

    def __init__(self, result: dict[str, Any]) -> None:
        super().__init__(result["error"])
        self.result = result


_VERIFICATION_FIELDS = (
    "verification_profile", "draft_typechecked", "strict_verified",
    "holes", "source_placeholders", "_harness_infrastructure_error",
)


def _workspace_name(registry: ToolRegistry, path: Path) -> str:
    return path.relative_to(registry.workspace.workspaces_dir.resolve()).as_posix()


def _batch_verification_result(
    registry: ToolRegistry, result: dict[str, Any], verifications: list[dict[str, Any]],
    checked_sources: dict[str, str],
) -> dict[str, Any]:
    """Keep one primary check's identity and evidence, plus all check feedback."""
    executions = [item["verification"].get("lean_executed") for item in verifications]
    result = {
        **result,
        "file_saved": True,
        "success": all(bool(item["verification"].get("success")) for item in verifications),
        "verification_count": len(verifications),
        "verifications": verifications,
        "lean_executed": (
            True if any(value is True for value in executions)
            else None if any(value is None for value in executions)
            else False
        ),
    }
    if not verifications:
        return result
    primary = next((item for item in verifications
                    if item["verification"].get("_harness_infrastructure_error")), None)
    if primary is None:
        primary = next((item for item in verifications
                        if not item["verification"].get("success")), None)
    if primary is None:
        submission = _workspace_name(registry, registry.workspace.submission_file.resolve())
        primary = next((item for item in verifications if item["file_path"] == submission),
                       verifications[0])
    verification = primary["verification"]
    # Do not combine a helper's holes or source bundle with the entrypoint's
    # strict result: downstream acceptance must see a single coherent check.
    return {
        **result,
        "file_path": primary["file_path"],
        # The last saving call can concern a different file, or earlier bytes.
        # Bind hashes and progress facts to this check's exact primary source.
        "proof_code": checked_sources.get(primary["file_path"]),
        "verification": verification,
        **{key: verification[key] for key in _VERIFICATION_FIELDS if key in verification},
    }


async def _verify_saved_batch(
    registry: ToolRegistry, paths: tuple[Path, ...], result: dict[str, Any],
    *, on_check: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Check final destinations only after the whole save has completed."""
    pending = getattr(registry, "_deferred_auto_checks", None)
    if pending is not None:
        for path in paths:
            if path.suffix == ".lean":
                pending[path.resolve()] = None
        return {
            **result, "file_saved": True, "lean_executed": False,
            "verification_count": 0,
            "automatic_check_deferred": any(path.suffix == ".lean" for path in paths),
        }
    verifications: list[dict[str, Any]] = []
    checked_sources: dict[str, str] = {}
    first_exception: Exception | None = None
    for path in paths:
        if path.suffix != ".lean":
            continue
        name = _workspace_name(registry, path)
        try:
            checked_sources[name] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            # The normal verifier path reports a missing/unreadable file.
            pass
        if on_check is not None:
            on_check()
        try:
            verification = await lean.lean_verify(registry, {"file_path": name})
            if not isinstance(verification, dict):
                raise TypeError("Lean verifier returned a non-object result")
        except Exception as exc:
            if first_exception is None:
                first_exception = exc
            error = f"{type(exc).__name__}: {exc}"
            verification = {
                "success": False,
                "file_path": name,
                "error": error,
                "lean_executed": None,
                "_harness_infrastructure_error": True,
            }
        verifications.append({"file_path": name, "verification": verification})
    result = _batch_verification_result(registry, result, verifications, checked_sources)
    if first_exception is not None:
        error = result["verification"].get("error") or (
            f"{type(first_exception).__name__}: {first_exception}"
        )
        raise SavedFileVerificationError({**result, "error": error}) from first_exception
    return result


def _line_slice(
    lines: list[str],
    line_range: list[int | None] | None,
) -> tuple[int, int]:
    if not line_range:
        return 0, min(len(lines), 200)
    start, end = ([*line_range, None, None])[:2]
    start_idx = (
        0 if start is None
        else (start - 1 if start > 0 else len(lines) + start)
    )
    end_idx = (
        len(lines) if end is None
        else (end - 1 if end > 0 else len(lines) + end)
    )
    start_idx = min(len(lines), max(0, start_idx))
    end_idx = min(len(lines), max(start_idx, end_idx))
    return start_idx, end_idx


def _missing_arg(
    args: dict[str, Any],
    *names: str,
    allow_empty: tuple[str, ...] = (),
) -> str | None:
    missing = [
        name for name in names
        if args.get(name) is None
        or (args.get(name) == "" and name not in allow_empty)
    ]
    return (
        "Missing required argument(s): " + ", ".join(missing)
        if missing
        else None
    )


def _file_write_argument_error(args: dict[str, Any]) -> str | None:
    if error := _missing_arg(args, "file_path", "content", allow_empty=("content",)):
        return error
    for name in ("file_path", "content"):
        if not isinstance(args.get(name), str):
            return f"TypeError: {name} must be a string"
    return None


def _file_edit_argument_error(args: dict[str, Any]) -> str | None:
    # An empty replacement deletes the selected text; it is still required.
    if error := _missing_arg(args, "file_path", "new_str", allow_empty=("new_str",)):
        return error
    for name in ("file_path", "new_str"):
        if not isinstance(args.get(name), str):
            return f"TypeError: {name} must be a string"
    target_path = args.get("target_path")
    if target_path is not None and not isinstance(target_path, str):
        return "TypeError: target_path must be a string or null"
    old_str = args.get("old_str")
    if old_str is not None and not isinstance(old_str, str):
        return "TypeError: old_str must be a string or null"
    old_line_range = args.get("old_line_range")
    if old_line_range is not None:
        if not isinstance(old_line_range, list):
            return "TypeError: old_line_range must be an array or null"
        if any(
            item is not None
            and (
                not isinstance(item, int)
                or isinstance(item, bool)
            )
            for item in old_line_range
        ):
            return (
                "TypeError: old_line_range items must be integers or null"
            )
    replace_all = args.get("replace_all")
    if replace_all is not None and not isinstance(replace_all, bool):
        return "TypeError: replace_all must be a boolean"
    if old_line_range is None and not old_str:
        return "Provide old_str or old_line_range"
    return None


def preview_file_edit_content(
    original: str,
    args: dict[str, Any],
) -> tuple[str | None, str | None]:
    """Apply DVP's validated edit semantics without touching the workspace."""

    if error := _file_edit_argument_error(args):
        return None, error
    old_line_range = args.get("old_line_range")
    old_str = args.get("old_str")
    new_str = args["new_str"]
    if old_line_range is not None:
        lines = original.splitlines(keepends=True)
        start, end = _line_slice_compat(
            [line.rstrip("\n") for line in lines],
            old_line_range,
        )
        edited = (
            "".join(lines[:start])
            + new_str
            + (
                "\n"
                if new_str and not new_str.endswith("\n")
                else ""
            )
            + "".join(lines[end:])
        )
        return edited, None
    assert isinstance(old_str, str) and old_str
    if old_str not in original:
        return None, "old_str not found"
    count = -1 if args.get("replace_all", False) else 1
    return original.replace(old_str, new_str, count), None


def _line_slice_compat(
    lines: list[str],
    line_range: list[int | None] | None,
) -> tuple[int, int]:
    """Mirror the upstream file_edit range convention."""

    if not line_range:
        return 0, min(len(lines), 200)
    start, end = ([*line_range, None, None])[:2]
    start_idx = (
        0
        if start is None
        else (start - 1 if start > 0 else len(lines) + start)
    )
    end_idx = (
        len(lines)
        if end is None
        else (end - 1 if end > 0 else len(lines) + end)
    )
    return max(0, start_idx), min(len(lines), end_idx)


async def file_read(registry: ToolRegistry, args: dict[str, Any]) -> dict[str, Any]:
    """Return the complete selected range and explicit continuation metadata.

    Character budgets belong to the model-facing result projection. Keeping
    the full selected range here lets that layer paginate at line boundaries
    instead of silently shortening the file's contents. ``start_column`` is a
    zero-based Unicode character offset in the first selected source line,
    allowing the projection to resume even a line longer than its budget.
    """

    if error := _missing_arg(args, "file_path"):
        return {"success": False, "error": error}
    start_column = args.get("start_column", 0)
    if not isinstance(start_column, int) or isinstance(start_column, bool):
        return {
            "success": False,
            "error": "TypeError: start_column must be a non-negative integer",
        }
    if start_column < 0:
        return {
            "success": False,
            "error": "ValueError: start_column must be a non-negative integer",
        }
    path = registry.workspace.resolve(args["file_path"])
    if not path.exists() or not path.is_file():
        return {
            "success": False,
            "error": f"File not found: {args['file_path']}",
        }
    lines = path.read_text(
        encoding="utf-8",
        errors="replace",
    ).splitlines()
    requested_line_range = args.get("line_range")
    start, end = _line_slice(lines, requested_line_range)
    first_line_length = len(lines[start]) if start < end else 0
    if start_column > first_line_length:
        return {
            "success": False,
            "error": (
                "ValueError: start_column exceeds the selected first line length "
                f"({first_line_length})"
            ),
        }
    rendered = "\n".join(
        f"{idx + 1:6d}|{line[start_column:] if idx == start else line}"
        for idx, line in enumerate(lines[start:end], start=start)
    )
    return {
        "success": True,
        "file_path": args["file_path"],
        "total_lines": len(lines),
        "requested_line_range": requested_line_range,
        "line_range": [start + 1, end + 1],
        "returned_line_range": [start + 1, end + 1],
        "content": rendered,
        **({"start_column": start_column} if start_column else {}),
        "truncated": False,
        "remaining_lines": len(lines) - end,
        "next_line_range": (
            [end + 1, min(end + 201, len(lines) + 1)]
            if end < len(lines)
            else None
        ),
    }


async def file_write(registry: ToolRegistry, args: dict[str, Any]) -> dict[str, Any]:
    """Save one file and check its final Lean contents after grouped writes."""
    args = {"file_path": SUBMISSION_PATH, **args}
    if error := _file_write_argument_error(args):
        return {"success": False, "error": error}
    path = registry.workspace.resolve(args["file_path"], for_write=True)
    content = args["content"].encode("utf-8")
    original = path.read_bytes() if path.exists() else None
    outcome = _commit_files({path: content}, {path: original}) or {}
    if outcome.get("success") is False:
        return {**outcome, "file_path": args["file_path"]}
    result = {
        "success": True,
        **outcome,
        "file_path": args["file_path"],
        "bytes": len(content),
        "lean_executed": False,
    }
    result["file_path"] = _workspace_name(registry, path)
    result["files"] = [{"file_path": result["file_path"], "bytes": len(content)}]
    return await _verify_saved_batch(registry, (path,), result)


_EDIT_FIELDS = frozenset({
    "file_path", "new_str", "old_str", "old_line_range", "replace_all", "target_path",
})


def automatic_check_paths(
    registry: ToolRegistry, backend_name: str, args: dict[str, Any],
) -> tuple[Path, ...]:
    """Plan distinct Lean output destinations, without reading or modifying files.

    The caller can reserve the check budget before any mutation. Matching edits
    against source text remains the handler's job; invalid structure or paths
    have no planned checks. Order matches the staged destinations' first use.
    """
    if not isinstance(args, dict):
        return ()
    try:
        if backend_name == "file_write":
            arguments = {"file_path": SUBMISSION_PATH, **args}
            if _file_write_argument_error(arguments):
                return ()
            path = registry.workspace.resolve(arguments["file_path"], for_write=True)
            return (path,) if path.suffix == ".lean" else ()
        if backend_name != "file_edit":
            return ()
        batch = "edits" in args
        default_path = args.get("file_path", SUBMISSION_PATH)
        if batch:
            if (set(args) - {"edits", "file_path"}
                    or not isinstance(default_path, str) or not default_path):
                return ()
            edits = args["edits"]
            if not isinstance(edits, list) or not edits:
                return ()
        else:
            edits = [args]
        destinations: dict[Path, None] = {}
        for edit in edits:
            if not isinstance(edit, dict) or (batch and set(edit) - _EDIT_FIELDS):
                return ()
            edit = {"file_path": default_path, **edit}
            if _file_edit_argument_error(edit):
                return ()
            registry.workspace.resolve(edit["file_path"])
            path = registry.workspace.resolve(
                edit.get("target_path") or edit["file_path"], for_write=True,
            )
            if path.suffix == ".lean":
                destinations[path] = None
        return tuple(destinations)
    except (OSError, TypeError, ValueError):
        return ()


def _source_text(content: bytes) -> str:
    """Match text-mode file reads while retaining exact bytes for rollback."""
    return content.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")


async def file_edit(registry: ToolRegistry, args: dict[str, Any]) -> dict[str, Any]:
    """Validate every edit against staged text before saving any result.

    An edits array is sequential: later edits see earlier changes, including a
    newly created target_path. Line ranges therefore refer to the current staged
    text. A shared file_path supplies the default for items without their own
    file_path. A bad edit changes no files. The synchronous commit has no await point,
    stages all output first, and rolls back already replaced files on an I/O error.
    """
    batch = "edits" in args
    default_path = args.get("file_path", SUBMISSION_PATH)
    if batch:
        if any(key in args for key in _EDIT_FIELDS - {"file_path"}):
            return {"success": False, "error": "Use edits or single-edit arguments, not both"}
        if unknown := set(args) - {"edits", "file_path"}:
            return {"success": False, "error": "Unknown argument(s): " + ", ".join(sorted(unknown))}
        if default_path is None or default_path == "":
            return {"success": False, "error": "Missing required argument(s): file_path"}
        if not isinstance(default_path, str):
            return {"success": False, "error": "TypeError: file_path must be a string"}
        edits = args["edits"]
        if not isinstance(edits, list) or not edits:
            return {"success": False, "error": "edits must be a non-empty array"}
    else:
        edits = [args]

    staged: dict[Path, bytes] = {}
    originals: dict[Path, bytes | None] = {}
    names: dict[Path, str] = {}
    for index, edit in enumerate(edits, 1):
        error = None
        try:
            if not isinstance(edit, dict):
                error = "Each edit must be an object"
            elif batch and (unknown := set(edit) - _EDIT_FIELDS):
                error = "Unknown edit argument(s): " + ", ".join(sorted(unknown))
            else:
                edit = {"file_path": default_path, **edit}
                error = _file_edit_argument_error(edit)
            if not error:
                source = registry.workspace.resolve(edit["file_path"])
                target_name = edit.get("target_path") or edit["file_path"]
                target = registry.workspace.resolve(target_name, for_write=True)
                if source in staged:
                    original_text = _source_text(staged[source])
                elif source.is_file():
                    originals.setdefault(source, source.read_bytes())
                    original_text = _source_text(originals[source])
                else:
                    error = f"File not found: {edit['file_path']}"
                if not error:
                    edited, error = preview_file_edit_content(original_text, edit)
                    if not error:
                        if target not in originals:
                            originals[target] = target.read_bytes() if target.exists() else None
                        assert edited is not None
                        staged[target] = edited.encode("utf-8")
                        names[target] = target_name
        except (OSError, UnicodeError, TypeError, ValueError) as exc:
            error = f"{type(exc).__name__}: {exc}"
        if error:
            return {"success": False, "error": f"Edit {index}: {error}" if batch else error}

    files = [{"file_path": names[path], "bytes": len(content)}
             for path, content in staged.items()]
    outcome = _commit_files(staged, originals) or {}
    if outcome.get("success") is False:
        return {**outcome, "files": [{"file_path": item["file_path"]} for item in files]}
    files = [{"file_path": _workspace_name(registry, path), "bytes": len(content)}
             for path, content in staged.items()]
    result = {"success": True, **outcome, "files": files, "lean_executed": False}
    if batch:
        result["edits_applied"] = len(edits)
    else:
        result.update(files[0])
    return await _verify_saved_batch(registry, tuple(staged), result)


def _discard_temporary(path: Path) -> str | None:
    """Cleanup must never hide a completed save or a failed rollback."""
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        return f"Temporary-file cleanup failed for {path.name}: {exc}"
    return None


def _stage_file(path: Path, content: bytes) -> Path:
    """Stage beside the destination so replacement stays on one filesystem."""
    temporary = path.parent / f".dvp-write-{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as handle:
            handle.write(content)
        if path.exists():
            temporary.chmod(stat.S_IMODE(path.stat().st_mode))
        return temporary
    except BaseException as exc:
        if warning := _discard_temporary(temporary):
            exc.add_note(warning)
        raise


def _commit_files(
    contents: dict[Path, bytes], originals: dict[Path, bytes | None],
) -> dict[str, Any] | None:
    """Prepare every output, then replace; report any rollback failure explicitly.

    Individual file replacement is atomic. This is not a filesystem transaction
    across crashes or unrelated external writers; normal write errors are rolled
    back and no model call can observe an intermediate state during dispatch.
    """
    prepared: dict[Path, Path] = {}
    committed: list[Path] = []
    created_directories: list[Path] = []
    result: dict[str, Any] | None = None
    cleanup_warnings: list[str] = []
    try:
        # Detect a stale snapshot before staging. A parent directory may not yet
        # exist when an edit writes a copy to a new helper-module directory.
        for path, original in originals.items():
            current = path.read_bytes() if path.exists() else None
            if current != original:
                raise OSError(f"File changed while preparing edits: {path.name}")
        for path, content in contents.items():
            missing = []
            parent = path.parent
            while not parent.exists():
                missing.append(parent)
                parent = parent.parent
            for directory in reversed(missing):
                directory.mkdir()
                created_directories.append(directory)
            prepared[path] = _stage_file(path, content)
        for path, temporary in prepared.items():
            os.replace(temporary, path)
            committed.append(path)
    except OSError as exc:
        cleanup_warnings.extend(getattr(exc, "__notes__", ()))
        rollback_errors = []
        for path in reversed(committed):
            temporary = None
            try:
                original = originals[path]
                if original is None:
                    path.unlink(missing_ok=True)
                else:
                    temporary = _stage_file(path, original)
                    os.replace(temporary, path)
            except OSError as rollback_exc:
                rollback_errors.append(f"{path.name}: {rollback_exc}")
                cleanup_warnings.extend(getattr(rollback_exc, "__notes__", ()))
            finally:
                if temporary is not None:
                    if warning := _discard_temporary(temporary):
                        cleanup_warnings.append(warning)
        detail = (
            "Rollback failed; some files may have changed: " + "; ".join(rollback_errors)
            if rollback_errors else "No file contents were changed."
        )
        result = {"success": False, "error": f"Could not save files: {exc}. {detail}"}
        if rollback_errors:
            result["rollback_failed"] = True
    finally:
        for temporary in prepared.values():
            if warning := _discard_temporary(temporary):
                cleanup_warnings.append(warning)
        for directory in reversed(created_directories):
            try:
                directory.rmdir()
            except OSError:
                pass  # Successful writes, or an external writer, may fill it.
    if cleanup_warnings:
        if result is None:
            result = {"success": True}
        result["warnings"] = cleanup_warnings
    return result
