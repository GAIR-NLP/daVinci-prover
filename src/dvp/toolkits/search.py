"""Bounded workspace file and content search via rg or Python."""

from __future__ import annotations

import asyncio
import base64
import fnmatch
import json
import os
from pathlib import Path
from contextlib import aclosing
import re
import shutil
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from dvp.toolkits.registry import ToolRegistry


_CONTENT_SEARCH_EXCLUDED_DIRS = {
    ".git", ".lake", ".mypy_cache", ".pytest_cache", "__pycache__",
    "build", "dist", "node_modules", "venv", ".venv",
}
_CONTENT_SEARCH_SKIP_SUFFIXES = {
    ".a", ".bin", ".bmp", ".cache", ".gif", ".idx", ".ilean",
    ".jpg", ".jpeg", ".o", ".olean", ".olean.server", ".png",
    ".pyc", ".so", ".trace", ".zip",
}


def _workspace_display_path(
    requested_path: str,
    base: Path,
    path: Path,
) -> str:
    requested = Path(requested_path)
    if path == base:
        return requested.as_posix()
    if base.is_file():
        rel = path.relative_to(base.parent)
        return (requested.parent / rel).as_posix()
    rel = path.relative_to(base)
    return (requested / rel).as_posix()


def _content_search_limit(args: dict[str, Any]) -> int:
    return max(1, int(args.get("limit", 20)))


def _content_search_max_bytes() -> int:
    explicit_bytes = os.getenv("DAVINCI_CONTENT_SEARCH_MAX_FILE_BYTES")
    if explicit_bytes is not None:
        return int(explicit_bytes)
    # Preserve the older rg-only setting while applying it to both backends.
    size = os.getenv("DAVINCI_CONTENT_SEARCH_MAX_FILESIZE", "2M").upper()
    match = re.fullmatch(r"(\d+)([KMG]?)", size)
    if match is None:
        raise ValueError("DAVINCI_CONTENT_SEARCH_MAX_FILESIZE must be bytes or use K/M/G")
    return int(match[1]) * (1024 ** {"": 0, "K": 1, "M": 2, "G": 3}[match[2]])


def _content_search_max_files() -> int:
    return int(os.getenv("DAVINCI_CONTENT_SEARCH_MAX_FILES", "20000"))


def _content_search_timeout() -> float:
    return float(os.getenv("DAVINCI_CONTENT_SEARCH_TIMEOUT", "30"))


def _is_searchable_text_file(path: Path, max_bytes: int) -> bool:
    if any(part in _CONTENT_SEARCH_EXCLUDED_DIRS for part in path.parts):
        return False
    if any(path.name.endswith(suffix) for suffix in _CONTENT_SEARCH_SKIP_SUFFIXES):
        return False
    try:
        return path.stat().st_size <= max_bytes
    except OSError:
        return False


def _trusted_search_mounts(registry: ToolRegistry, base: Path) -> tuple[Path, ...]:
    """Only the configured target is an external mount at the workspace root."""
    workspace = registry.workspace
    if (base == workspace.workspaces_dir.resolve() and workspace.target is not None
            and workspace.target.is_symlink()):
        return (workspace.workspaces_dir.resolve() / "target",)
    return ()


def _walk_search_files(
    base: Path, *, max_depth: int | None = None,
    trusted_mounts: tuple[Path, ...] = (), include_hidden: bool = True,
    excluded_dirs: set[str] | None = None, deadline: float | None = None,
):
    def check_time():
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError

    def walk(directory: Path, depth: int):
        check_time()
        # Match rg's component ordering, mixing files and directories.
        with os.scandir(directory) as stream:
            entries = sorted(stream, key=lambda entry: entry.name)
        for entry in entries:
            check_time()
            if not include_hidden and entry.name.startswith("."):
                continue
            path = Path(entry.path)
            if entry.is_symlink() and path not in trusted_mounts:
                continue
            if entry.is_dir():
                if (max_depth is None or depth < max_depth) and entry.name not in (excluded_dirs or ()):
                    yield from walk(path, depth + 1)
            elif entry.is_file(follow_symlinks=False):
                yield path

    if base.is_file():
        yield base
    elif base.is_dir():
        yield from walk(base, 1)


def _iter_content_search_files(
    base: Path, *, recursive: bool, max_bytes: int,
    trusted_mounts: tuple[Path, ...] = (), deadline: float | None = None,
):
    for path in _walk_search_files(
        base, max_depth=None if recursive else 1, trusted_mounts=trusted_mounts,
        excluded_dirs=_CONTENT_SEARCH_EXCLUDED_DIRS, deadline=deadline,
    ):
        if _is_searchable_text_file(path, max_bytes):
            yield path


def _matches_glob(parts: tuple[str, ...], pattern: tuple[str, ...]) -> bool:
    if not pattern:
        return not parts
    if pattern[0] == "**":
        return _matches_glob(parts, pattern[1:]) or bool(
            parts and not parts[0].startswith(".") and _matches_glob(parts[1:], pattern)
        )
    if not parts or (parts[0].startswith(".") and not pattern[0].startswith(".")):
        return False
    return fnmatch.fnmatchcase(parts[0], pattern[0]) and _matches_glob(parts[1:], pattern[1:])


async def file_search(registry: ToolRegistry, args: dict[str, Any]) -> dict[str, Any]:
    directory_path = args.get("directory_path") or args.get("path")
    search_pattern = args.get("pattern") or args.get("file_pattern")
    if directory_path is None or search_pattern is None:
        return {
            "success": False,
            "error": "Missing required argument(s): directory_path, pattern",
        }
    base = registry.workspace.resolve(directory_path)
    pattern = Path(search_pattern)
    if pattern.is_absolute() or ".." in pattern.parts:
        raise ValueError("Search pattern must stay within the requested workspace path")
    if not pattern.parts or str(search_pattern).endswith("/"):
        return {"success": True, "matches": [], "count": 0}
    recursive = bool(args.get("recursive", True))
    parts = (("**",) + pattern.parts) if recursive else tuple(
        "*" if part == "**" else part for part in pattern.parts
    )
    return await asyncio.to_thread(
        _file_search_sync, base, directory_path, parts,
        max(1, int(args.get("limit", 50))), _trusted_search_mounts(registry, base),
    )


def _file_search_sync(
    base: Path, directory_path: str, pattern: tuple[str, ...], limit: int,
    trusted_mounts: tuple[Path, ...] = (),
) -> dict[str, Any]:
    matches: list[str] = []
    for path in _walk_search_files(
        base, max_depth=None if "**" in pattern else len(pattern),
        trusted_mounts=trusted_mounts,
        include_hidden=any(part.startswith(".") for part in pattern),
    ):
        if _matches_glob(path.relative_to(base).parts, pattern):
            matches.append(_workspace_display_path(directory_path, base, path))
            if len(matches) >= limit:
                break
    return {"success": True, "matches": matches, "count": len(matches)}


def _context_counts(args: dict[str, Any]) -> tuple[int, int]:
    common = args.get("C")
    before = common if common is not None else args.get("B", 2)
    after = common if common is not None else args.get("A", 2)
    before = 2 if before is None else int(before)
    after = 2 if after is None else int(after)
    if before < 0 or after < 0:
        raise ValueError("Content search context counts must be nonnegative")
    return before, after


async def content_search(registry: ToolRegistry, args: dict[str, Any]) -> dict[str, Any]:
    requested_path = (
        args.get("path")
        or args.get("directory_path")
        or args.get("file_path")
    )
    pattern = (
        args.get("content_pattern")
        or args.get("pattern")
        or args.get("query")
    )
    if requested_path is None or pattern is None:
        return {
            "success": False,
            "error": "Missing required argument(s): path, content_pattern",
        }
    base = registry.workspace.resolve(requested_path)
    if not base.exists():
        return _search_error(_empty_search_result(), f"Search path does not exist: {requested_path}")
    use_regex = bool(args.get("use_regex", True))
    recursive = bool(args.get("recursive", True))
    limit = _content_search_limit(args)
    max_matches = int(args.get("max_matches_per_file", 10))
    if max_matches < 1:
        raise ValueError("max_matches_per_file must be positive")
    before, after = _context_counts(args)
    options = (base, requested_path, pattern, use_regex, recursive, limit,
               max_matches, before, after, _trusted_search_mounts(registry, base))
    rg_result = await _content_search_with_rg(registry, *options)
    if rg_result is not None:
        return rg_result
    return await asyncio.to_thread(_content_search_python, *options)


def _empty_search_result(scan_limit_reached: bool = False) -> dict[str, Any]:
    return {
        "success": True,
        "results": [],
        "count": 0,
        "file_count": 0,
        "truncated": scan_limit_reached,
        "file_limit_reached": False,
        "files_with_more_matches": [],
        "scan_limit_reached": scan_limit_reached,
    }


def _add_file_matches(
    result: dict[str, Any],
    display_path: str,
    lines: list[str],
    line_numbers: list[int],
    *,
    limit: int,
    max_matches_per_file: int,
    before_context: int,
    after_context: int,
) -> bool:
    if not line_numbers:
        return True
    if result["file_count"] >= limit:
        result.update(truncated=True, file_limit_reached=True)
        return False
    if len(line_numbers) > max_matches_per_file:
        result["truncated"] = True
        result["files_with_more_matches"].append(display_path)
    for line_number in line_numbers[:max_matches_per_file]:
        index = line_number - 1
        start = max(0, index - before_context)
        end = min(len(lines), index + after_context + 1)
        result["results"].append({
            "file": display_path,
            "line": line_number,
            "text": lines[index],
            "context_start_line": start + 1,
            "context_end_line": end,
            "context": "\n".join(lines[start:end]),
        })
    result["file_count"] += 1
    result["count"] = len(result["results"])
    return True


def _search_error(result: dict[str, Any], error: str) -> dict[str, Any]:
    result.update(success=False, truncated=True, error=error)
    return result


def _rg_text(field: dict[str, str]) -> str:
    if "text" in field:
        return field["text"]
    return os.fsdecode(base64.b64decode(field.get("bytes", "")))


async def _finish_rg_process(proc: Any, stderr_task: Any, *, kill: bool) -> bytes:
    if kill and proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    # Draining stdout matters after early termination: waiting with a full pipe
    # can deadlock even after the child has exited.
    await proc.stdout.read()
    stderr = await stderr_task
    await proc.wait()
    return stderr


async def _rg_search_candidates(
    rg: str, base: Path, recursive: bool, deadline: float,
    trusted_mounts: tuple[Path, ...] = (),
):
    """Stream small bounded batches so matches need not wait for a full walk."""
    if any(part in _CONTENT_SEARCH_EXCLUDED_DIRS for part in base.parts) or (
        base.is_file() and not _is_searchable_text_file(base, _content_search_max_bytes())
    ):
        yield [], False, None
        return
    cmd = [rg, "--files", "--null", "--sort", "path", "--hidden", "--no-ignore",
           "--max-filesize", str(_content_search_max_bytes())]
    for name in sorted(_CONTENT_SEARCH_EXCLUDED_DIRS):
        cmd.extend(["--glob", f"!**/{name}/**"])
    for suffix in sorted(_CONTENT_SEARCH_SKIP_SUFFIXES):
        cmd.extend(["--glob", f"!*{suffix}"])
    if not recursive:
        cmd.extend(["--max-depth", "1"])
    # rg does not follow nested symlinks. Traverse the authorized root mount
    # explicitly; its virtual path spelling is preserved in results.
    roots = [base, *trusted_mounts] if recursive else [base]
    proc = await asyncio.create_subprocess_exec(
        *cmd, "--", *(str(path) for path in roots),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stderr_task = asyncio.create_task(proc.stderr.read())
    paths: list[Path] = []
    limited = interrupted = False
    failure = None
    total = 0
    yielded = False
    try:
        while True:
            try:
                encoded = await asyncio.wait_for(
                    proc.stdout.readuntil(b"\0"),
                    timeout=max(0.001, deadline - time.monotonic()),
                )
            except asyncio.IncompleteReadError:
                break
            if total >= _content_search_max_files():
                limited = True
                break
            total += 1
            paths.append(Path(os.fsdecode(encoded[:-1])))
            if len(paths) >= 256:
                yielded = True
                yield paths, False, None
                paths = []
    except asyncio.TimeoutError:
        failure = f"content_search timed out after {_content_search_timeout():g}s"
    except BaseException:
        interrupted = True
        raise
    finally:
        stderr = await _finish_rg_process(
            proc, stderr_task, kill=limited or failure is not None or interrupted,
        )
    if proc.returncode not in (0, 1) and not limited and failure is None:
        failure = stderr.decode("utf-8", errors="replace").strip() or f"rg exited with status {proc.returncode}"
    if paths or limited or failure or not yielded:
        yield paths, limited, failure


def _rg_path_batches(paths: list[Path]):
    """Usually one search process; split only to stay below the OS argv limit."""
    try:
        available = os.sysconf("SC_ARG_MAX") - sum(
            len(os.fsencode(key)) + len(os.fsencode(value)) + 2
            for key, value in os.environ.items()
        ) - 65536
    except (ValueError, OSError):
        available = 131072
    budget = max(32768, min(1024 * 1024, available))
    batch = []
    used = 0
    for path in paths:
        size = len(os.fsencode(path)) + 16
        if batch and used + size > budget:
            yield batch
            batch, used = [], 0
        batch.append(path)
        used += size
    if batch or not paths:
        yield batch


def _add_rg_matches(
    result: dict[str, Any], display_path: str, lines: dict[int, str],
    line_numbers: list[int], *, max_matches_per_file: int,
    before_context: int, after_context: int,
) -> None:
    if not line_numbers:
        return
    if len(line_numbers) > max_matches_per_file:
        result["truncated"] = True
        result["files_with_more_matches"].append(display_path)
    for number in line_numbers[:max_matches_per_file]:
        # JSON match/context events already contain the actual source lines;
        # do not reread a whole file or race a second read against edits.
        start, end = number, number
        while start > max(1, number - before_context) and start - 1 in lines:
            start -= 1
        while end < number + after_context and end + 1 in lines:
            end += 1
        result["results"].append({
            "file": display_path, "line": number, "text": lines[number],
            "context_start_line": start, "context_end_line": end,
            "context": "\n".join(lines[index] for index in range(start, end + 1)),
        })
    result["file_count"] += 1
    result["count"] = len(result["results"])


async def _content_search_with_rg(
    registry: ToolRegistry, base: Path, requested_path: str, pattern: str,
    use_regex: bool, recursive: bool, limit: int,
    max_matches_per_file: int = 10, before_context: int = 2, after_context: int = 2,
    trusted_mounts: tuple[Path, ...] = (),
) -> dict[str, Any] | None:
    rg = shutil.which("rg")
    if rg is None:
        return None
    deadline = time.monotonic() + _content_search_timeout()
    result = _empty_search_result()
    async with aclosing(_rg_search_candidates(
        rg, base, recursive, deadline, trusted_mounts,
    )) as candidates:
        async for paths, scan_limit, failure in candidates:
            if scan_limit:
                result.update(scan_limit_reached=True, truncated=True)
            if failure:
                return _search_error(result, failure)
            for batch in _rg_path_batches(paths):
                cmd = [rg, "--json", "--line-buffered", "--text", "--sort", "path",
                       "--max-count", str(max_matches_per_file + 1),
                       "--before-context", str(before_context), "--after-context", str(after_context),
                       "--max-filesize", str(_content_search_max_bytes())]
                if not use_regex:
                    cmd.append("-F")
                cmd.extend(["--", pattern, *([str(path) for path in batch] or ["-"])])
                proc = await asyncio.create_subprocess_exec(
                    *cmd, stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    limit=max(65536, _content_search_max_bytes() * 6 + 4096),
                )
                stderr_task = asyncio.create_task(proc.stderr.read())
                line_numbers: list[int] = []
                lines: dict[int, str] = {}
                path: Path | None = None
                limited = interrupted = False
                failure = None

                def commit_matches():
                    if path is not None:
                        _add_rg_matches(
                            result, _workspace_display_path(requested_path, base, path),
                            lines, line_numbers,
                            max_matches_per_file=max_matches_per_file,
                            before_context=before_context, after_context=after_context,
                        )
                    lines.clear()
                    line_numbers.clear()

                try:
                    while True:
                        encoded = await asyncio.wait_for(
                            proc.stdout.readline(), timeout=max(0.001, deadline - time.monotonic()),
                        )
                        if not encoded:
                            break
                        event = json.loads(encoded)
                        kind, data = event.get("type"), event.get("data", {})
                        if kind in {"match", "context"}:
                            path = Path(_rg_text(data["path"]))
                            number = data["line_number"]
                            lines[number] = _rg_text(data["lines"]).rstrip("\r\n")
                            if kind == "match":
                                if result["file_count"] >= limit:
                                    result.update(truncated=True, file_limit_reached=True)
                                    limited = True
                                    break
                                line_numbers.append(number)
                        elif kind == "end":
                            commit_matches()
                except asyncio.TimeoutError:
                    failure = f"content_search timed out after {_content_search_timeout():g}s"
                    commit_matches()
                except BaseException:
                    interrupted = True
                    raise
                finally:
                    stderr = await _finish_rg_process(
                        proc, stderr_task, kill=limited or failure is not None or interrupted,
                    )
                if failure:
                    return _search_error(result, failure)
                if limited:
                    return result
                if proc.returncode not in (0, 1):
                    return _search_error(
                        result, stderr.decode("utf-8", errors="replace").strip()
                        or f"rg exited with status {proc.returncode}",
                    )
    return result


def _content_search_python(
    base: Path, requested_path: str, pattern: str, use_regex: bool,
    recursive: bool, limit: int, max_matches_per_file: int = 10,
    before_context: int = 2, after_context: int = 2,
    trusted_mounts: tuple[Path, ...] = (),
) -> dict[str, Any]:
    result = _empty_search_result()
    deadline = time.monotonic() + _content_search_timeout()
    try:
        regex = re.compile(pattern) if use_regex else None
    except re.error as exc:
        return _search_error(result, f"Invalid search regex: {exc}")
    try:
        for index, path in enumerate(_iter_content_search_files(
            base, recursive=recursive, max_bytes=_content_search_max_bytes(),
            trusted_mounts=trusted_mounts, deadline=deadline,
        )):
            if index >= _content_search_max_files():
                result.update(scan_limit_reached=True, truncated=True)
                break
            if time.monotonic() >= deadline:
                raise TimeoutError
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError as exc:
                return _search_error(result, f"Cannot read search file {path.name}: {exc}")
            line_numbers = []
            for lineno, line in enumerate(lines, start=1):
                if time.monotonic() >= deadline:
                    _add_file_matches(
                        result, _workspace_display_path(requested_path, base, path),
                        lines, line_numbers, limit=limit,
                        max_matches_per_file=max_matches_per_file,
                        before_context=before_context, after_context=after_context,
                    )
                    raise TimeoutError
                matched = bool(regex.search(line)) if regex else pattern in line
                if matched:
                    line_numbers.append(lineno)
                    if len(line_numbers) > max_matches_per_file:
                        break
            if not _add_file_matches(
                result, _workspace_display_path(requested_path, base, path),
                lines, line_numbers, limit=limit,
                max_matches_per_file=max_matches_per_file,
                before_context=before_context, after_context=after_context,
            ):
                break
    except TimeoutError:
        return _search_error(result, f"content_search timed out after {_content_search_timeout():g}s")
    return result
