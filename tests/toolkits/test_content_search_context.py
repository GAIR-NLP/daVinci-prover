"""Source lookup preserves later declarations, context, and explicit limits."""

import asyncio
from contextlib import nullcontext
import json
from pathlib import Path
import shutil
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from dvp.agent.result_projection import _compact
from dvp.runtime.workspace import Workspace
from dvp.toolkits.registry import ToolRegistry
from dvp.toolkits.surface import CompatibilitySurface
from dvp.toolkits import search


class SearchRegistry:
    def __init__(self, workspace, backend):
        self.workspace = workspace
        self.backend = backend


@pytest.fixture(params=["python", "rg"])
def registry(tmp_path, request):
    if request.param == "rg" and shutil.which("rg") is None:
        pytest.skip("rg is not installed")
    workspace = Workspace(tmp_path / "run")
    workspace.setup()
    return SearchRegistry(workspace, request.param)


def run(registry, **kwargs):
    backend = (
        patch.object(search.shutil, "which", return_value=None)
        if registry.backend == "python" else nullcontext()
    )
    with backend:
        return asyncio.run(search.content_search(registry, {
            "path": "Submission", "content_pattern": "Algebra", "use_regex": False,
            **kwargs,
        }))


def test_import_does_not_hide_later_declarations_and_context(registry):
    lines = [
        "import Mathlib.Algebra.Field.Basic", "", "namespace Example",
        "variable {K : Type*} [Field K]", "-- desired declaration follows",
        "theorem Algebra_one : True := by", "  trivial", "",
        "theorem Algebra_two : True := by trivial", "end Example",
    ]
    (registry.workspace.submission_dir / "Basic.lean").write_text("\n".join(lines))
    result = run(registry)
    assert result["success"] and not result["truncated"]
    assert result["count"] == 3 and result["file_count"] == 1
    assert [item["line"] for item in result["results"]] == [1, 6, 9]
    declaration = result["results"][1]
    assert declaration == {
        "file": "Submission/Basic.lean", "line": 6, "text": lines[5],
        "context_start_line": 4, "context_end_line": 8,
        "context": "\n".join(lines[3:8]),
    }


def test_default_ten_matches_and_explicit_per_file_truncation(registry):
    (registry.workspace.submission_dir / "Many.lean").write_text("\n".join(
        f"theorem Algebra_{i} : True := by trivial" for i in range(11)
    ))
    result = run(registry)
    assert result["count"] == 10 and result["file_count"] == 1
    assert result["truncated"]
    assert result["files_with_more_matches"] == ["Submission/Many.lean"]
    assert not result["file_limit_reached"]
    complete = run(registry, max_matches_per_file=11)
    assert complete["count"] == 11 and not complete["truncated"]


def test_limit_counts_files_and_only_flags_observed_omissions(registry):
    for name in ("A.lean", "B.lean", "C.lean"):
        (registry.workspace.submission_dir / name).write_text("Algebra one\nAlgebra two\n")
    result = run(registry, limit=2)
    assert result["count"] == 4 and result["file_count"] == 2
    assert [item["file"] for item in result["results"]] == [
        "Submission/A.lean", "Submission/A.lean", "Submission/B.lean", "Submission/B.lean",
    ]
    assert result["truncated"] and result["file_limit_reached"]
    complete = run(registry, limit=3, max_matches_per_file=2)
    assert complete["count"] == 6 and not complete["truncated"]


@pytest.mark.parametrize("context, expected", [
    ({"A": 1, "B": 3}, (2, 6)),
    ({"C": 1, "A": 0, "B": 3}, (4, 6)),
    ({"C": 0}, (5, 5)),
])
def test_context_overrides_are_exact(registry, context, expected):
    lines = ["one", "two", "three", "four", "Algebra", "six", "seven"]
    (registry.workspace.submission_dir / "Context.lean").write_text("\n".join(lines))
    item = run(registry, **context)["results"][0]
    assert (item["context_start_line"], item["context_end_line"]) == expected
    start, end = expected
    assert item["context"] == "\n".join(lines[start - 1:end])


def test_long_matching_lines_are_not_silently_cut(registry):
    line = "theorem Algebra : " + "LongType " * 200 + ":= by trivial"
    (registry.workspace.submission_dir / "Long.lean").write_text(line)
    result = run(registry)
    assert result["results"][0]["text"] == line
    assert result["results"][0]["context"] == line
    assert not result["truncated"]


def test_backends_share_file_filters_and_scan_budget(registry, monkeypatch):
    scratch = registry.workspace.submission_dir
    (scratch / "A.lean").write_text("Algebra")
    (scratch / "B.lean").write_text("Algebra")
    (scratch / "binary.olean").write_text("Algebra")
    (scratch / "build").mkdir()
    (scratch / "build" / "Hidden.lean").write_text("Algebra")
    monkeypatch.setenv("DAVINCI_CONTENT_SEARCH_MAX_FILES", "1")
    result = run(registry)
    assert result["count"] == 1
    assert result["results"][0]["file"] == "Submission/A.lean"
    assert result["scan_limit_reached"] and result["truncated"]
    monkeypatch.setenv("DAVINCI_CONTENT_SEARCH_MAX_FILES", "20")
    result = run(registry)
    assert result["count"] == 2 and not result["truncated"]


def test_nested_symlinks_do_not_escape_but_trusted_mounts_work(registry, tmp_path):
    external = tmp_path / "library"
    external.mkdir()
    (external / "Real.lean").write_text("Algebra")
    (registry.workspace.submission_dir / "escape.lean").symlink_to(external / "Real.lean")
    (registry.workspace.submission_dir / "escape_dir").symlink_to(external, target_is_directory=True)
    assert run(registry)["results"] == []
    with pytest.raises(ValueError, match="escapes workspace"):
        run(registry, path="Submission/escape.lean")
    registry.workspace.target_workspace = external
    registry.workspace.setup()
    result = run(registry, path="target")
    assert result["results"][0]["file"] == "target/Real.lean"


def test_nonrecursive_and_explicit_file_search(registry):
    scratch = registry.workspace.submission_dir
    (scratch / "Top.lean").write_text("Algebra")
    (scratch / "nested").mkdir()
    (scratch / "nested" / "Child.lean").write_text("Algebra")
    assert run(registry, recursive=False)["count"] == 1
    explicit = run(registry, path="Submission/nested/Child.lean")
    assert explicit["count"] == 1
    assert explicit["results"][0]["file"] == "Submission/nested/Child.lean"


def test_rg_and_python_results_are_identical(tmp_path):
    if shutil.which("rg") is None:
        pytest.skip("rg is not installed")
    workspace = Workspace(tmp_path / "run")
    workspace.setup()
    (workspace.submission_dir / "A.lean").write_text("Algebra one\ncontext\nAlgebra two\nAlgebra three\n")
    (workspace.submission_dir / "B.lean").write_text("Algebra four\n")
    (workspace.submission_dir / ".hidden.lean").write_text("hidden context\nAlgebra five\n")
    for options in ({}, {"limit": 1}, {"max_matches_per_file": 2}, {"C": 0},
                    {"content_pattern": r"Algebra (one|four)", "use_regex": True}):
        rg_result = run(SearchRegistry(workspace, "rg"), **options)
        python_result = run(SearchRegistry(workspace, "python"), **options)
        assert rg_result == python_result


def test_file_size_settings_apply_to_both_backends(registry, monkeypatch):
    monkeypatch.delenv("DAVINCI_CONTENT_SEARCH_MAX_FILE_BYTES", raising=False)
    monkeypatch.setenv("DAVINCI_CONTENT_SEARCH_MAX_FILESIZE", "1K")
    (registry.workspace.submission_dir / "Large.lean").write_text("Algebra " + "x" * 1200)
    (registry.workspace.submission_dir / "Small.lean").write_text("Algebra")
    result = run(registry)
    assert result["count"] == 1
    assert result["results"][0]["file"] == "Submission/Small.lean"
    monkeypatch.setenv("DAVINCI_CONTENT_SEARCH_MAX_FILE_BYTES", "2000")
    assert run(registry)["count"] == 2


@pytest.fixture
def real_registry(registry, monkeypatch):
    if registry.backend == "python":
        monkeypatch.setattr(search.shutil, "which", lambda _: None)
    return ToolRegistry(
        workspace=registry.workspace,
        verifier=SimpleNamespace(config=SimpleNamespace(target_workspace=None, timeout=1)),
    )


def registry_search(registry, **kwargs):
    return asyncio.run(registry.call("content_search", {
        "path": "Submission", "content_pattern": "Algebra", "use_regex": False,
        **kwargs,
    }))


def serialized_length(value):
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def test_registry_exposes_and_forwards_context_controls(real_registry):
    schema = next(
        item["function"] for item in CompatibilitySurface(real_registry).schemas()
        if item["function"]["name"] == "content_search"
    )
    properties = schema["parameters"]["properties"]
    assert properties["limit"]["default"] == 20
    assert properties["max_matches_per_file"]["default"] == 10
    assert properties["A"]["default"] == properties["B"]["default"] == 2
    assert "C" in properties
    (real_registry.workspace.submission_dir / "A.lean").write_text(
        "header\nAlgebra first\nbody\nAlgebra second\ntail\n"
    )
    (real_registry.workspace.submission_dir / "B.lean").write_text("Algebra third\n")
    raw = registry_search(real_registry, limit=1, max_matches_per_file=1, A=0, B=1)
    assert raw["success"] and raw["file_count"] == raw["count"] == 1
    assert raw["files_with_more_matches"] == ["Submission/A.lean"]
    assert raw["file_limit_reached"]
    item = raw["results"][0]
    assert item["context"] == "header\nAlgebra first"
    assert (item["context_start_line"], item["context_end_line"]) == (1, 2)
    c_override = registry_search(real_registry, C=0, A=3, B=3)
    assert c_override["results"][0]["context"] == "Algebra first"
    visible = _compact(raw, tool_name="content_search", max_chars=12000)
    assert visible == {key: value for key, value in raw.items() if key != "stop"}
    assert raw["stop"] is False


def test_model_receives_more_than_twelve_search_matches_when_they_fit(real_registry):
    for name in ("A.lean", "B.lean", "C.lean"):
        (real_registry.workspace.submission_dir / name).write_text("\n".join(
            f"theorem Algebra_{i} : True := by trivial" for i in range(7)
        ))
    raw = registry_search(real_registry, C=0)
    assert raw["count"] == 21 and not raw["truncated"]
    assert serialized_length(raw) < 12000
    visible = _compact(raw, tool_name="content_search", max_chars=12000)
    assert visible == {key: value for key, value in raw.items() if key != "stop"}
    assert raw["stop"] is False
    assert len(visible["results"]) == 21
    assert visible["results"][-1]["file"] == "Submission/C.lean"


@pytest.mark.parametrize("budget", [512, 1024, 2048, 12000])
def test_search_projection_reports_omissions_and_respects_json_budget(real_registry, budget):
    for name in ("A.lean", "B.lean", "C.lean"):
        (real_registry.workspace.submission_dir / name).write_text("\n".join(
            f"theorem Algebra_{i} : " + "α中文→β " * 100 + ":= by trivial"
            for i in range(7)
        ))
    raw = registry_search(real_registry, C=1)
    assert raw["count"] == 21 and not raw["truncated"]
    assert serialized_length(raw) > budget
    visible = _compact(raw, tool_name="content_search", max_chars=budget)
    assert serialized_length(visible) <= budget
    assert visible["result_truncated"]
    assert visible["results_shown"] == len(visible["results"])
    assert visible["results_omitted"] == raw["count"] - visible["results_shown"]
    assert visible["results_omitted"] > 0
    assert "file_read" in visible["continuation"]
    for item in visible["results"]:
        original = next(row for row in raw["results"]
                        if row["file"] == item["file"] and row["line"] == item["line"])
        if "context" in item:
            assert item == original
        else:
            assert item["context_omitted"]
            if item["text"] != original["text"]:
                assert item["text_truncated"]


def test_unicode_long_line_and_context_reach_model_intact(real_registry):
    declaration = "theorem Algebra : " + "α中文→β " * 700 + ":= by trivial"
    context = "前文：局部变量 β\n" + declaration + "\n后文：声明结束"
    (real_registry.workspace.submission_dir / "Unicode.lean").write_text(context)
    raw = registry_search(real_registry)
    assert len(declaration) > 4000
    assert serialized_length(raw) < 12000
    visible = _compact(raw, tool_name="content_search", max_chars=12000)
    assert visible == {key: value for key, value in raw.items() if key != "stop"}
    assert raw["stop"] is False
    assert visible["results"][0]["text"] == declaration
    assert visible["results"][0]["context"] == context
    assert visible["results"][0]["context_start_line"] == 1
    assert visible["results"][0]["context_end_line"] == 3


def test_nested_file_order_and_scan_limit_match_rg(registry, monkeypatch):
    scratch = registry.workspace.submission_dir
    (scratch / "a").mkdir()
    (scratch / "a" / "First.lean").write_text("Algebra first\n")
    (scratch / "z.lean").write_text("Algebra last\n")
    result = run(registry, limit=1)
    assert result["results"][0]["file"] == "Submission/a/First.lean"
    assert result["file_limit_reached"]
    monkeypatch.setenv("DAVINCI_CONTENT_SEARCH_MAX_FILES", "1")
    capped = run(registry)
    assert capped["results"][0]["file"] == "Submission/a/First.lean"
    assert capped["scan_limit_reached"]


def test_native_enumeration_matches_fallback_filters_before_scan_cap(registry, monkeypatch):
    scratch = registry.workspace.submission_dir
    (scratch / ".gitignore").write_text("B.lean\n")
    (scratch / "A.lean").write_text("Algebra " + "x" * 300)
    (scratch / "B.lean").write_text("Algebra visible despite gitignore\n")
    (scratch / "C.lean").write_text("Algebra later\n")
    (scratch / ".hidden").mkdir()
    (scratch / ".hidden" / "File.lean").write_text("Algebra hidden file\n")
    monkeypatch.setenv("DAVINCI_CONTENT_SEARCH_MAX_FILE_BYTES", "100")
    result = run(registry)
    assert [row["file"] for row in result["results"]] == [
        "Submission/.hidden/File.lean", "Submission/B.lean", "Submission/C.lean",
    ]
    # .gitignore itself is a searchable text file, so it consumes one scan slot.
    monkeypatch.setenv("DAVINCI_CONTENT_SEARCH_MAX_FILES", "3")
    capped = run(registry)
    assert [row["file"] for row in capped["results"]] == [
        "Submission/.hidden/File.lean", "Submission/B.lean",
    ]
    assert capped["scan_limit_reached"]


def test_rg_uses_native_enumeration_and_one_search_process(tmp_path, monkeypatch):
    if shutil.which("rg") is None:
        pytest.skip("rg is not installed")
    workspace = Workspace(tmp_path / "run")
    workspace.setup()
    for index in range(80):
        (workspace.submission_dir / f"File{index:03}.lean").write_text("unrelated text\n")
    original = asyncio.create_subprocess_exec
    commands = []

    async def record_command(*args, **kwargs):
        commands.append(args)
        return await original(*args, **kwargs)

    def no_python_walk(*args, **kwargs):
        raise AssertionError("Native search must not pre-stat the whole tree in Python")

    monkeypatch.setattr(search.asyncio, "create_subprocess_exec", record_command)
    monkeypatch.setattr(search, "_iter_content_search_files", no_python_walk)
    result = run(SearchRegistry(workspace, "rg"))
    assert result["success"] and result["count"] == 0
    assert len(commands) == 2
    assert "--files" in commands[0] and "--null" in commands[0]
    assert "--json" in commands[1]


def test_rg_handles_json_match_lines_above_stream_reader_default(tmp_path):
    if shutil.which("rg") is None:
        pytest.skip("rg is not installed")
    workspace = Workspace(tmp_path / "run")
    workspace.setup()
    long_line = "Algebra " + "\\\"" * 40000
    (workspace.submission_dir / "Long.lean").write_text(long_line)
    result = run(SearchRegistry(workspace, "rg"), C=0)
    assert result["success"] and result["count"] == 1
    assert result["results"][0]["text"] == long_line
    assert result["results"][0]["context"] == long_line


def test_directory_prefix_uses_rg_component_sort_before_scan_cap(registry, monkeypatch):
    scratch = registry.workspace.submission_dir
    (scratch / "a").mkdir()
    (scratch / "a" / "Child.lean").write_text("Algebra child\n")
    (scratch / "a.lean").write_text("Algebra sibling\n")
    result = run(registry, limit=1)
    assert result["results"][0]["file"] == "Submission/a/Child.lean"
    monkeypatch.setenv("DAVINCI_CONTENT_SEARCH_MAX_FILES", "1")
    capped = run(registry)
    assert capped["results"][0]["file"] == "Submission/a/Child.lean"
    assert capped["scan_limit_reached"]


def test_invalid_regex_is_rejected_even_when_no_files_are_searchable(registry):
    result = run(registry, content_pattern="[", use_regex=True)
    assert result["success"] is False
    assert result["error"]
    assert result["results"] == []


def test_missing_search_path_is_an_error_in_both_backends(registry):
    result = run(registry, path="Submission/missing")
    assert result["success"] is False
    assert "does not exist" in result["error"]
    assert result["results"] == []


def test_file_search_root_keeps_target_alias_and_skips_untrusted_symlinks(tmp_path):
    library = tmp_path / "mathlib"
    library.mkdir()
    (library / "Mathlib").mkdir()
    (library / "Mathlib" / "Basic.lean").write_text("Algebra")
    outside = tmp_path / "private"
    outside.mkdir()
    (outside / "Secret.lean").write_text("private")
    (library / "escape").symlink_to(outside, target_is_directory=True)
    workspace = Workspace(tmp_path / "run", target_workspace=library)
    workspace.setup()
    (workspace.submission_dir / "Helper.lean").write_text("Algebra")
    (workspace.submission_dir / "escape").symlink_to(outside, target_is_directory=True)
    (workspace.submission_dir / "escape.lean").symlink_to(outside / "Secret.lean")
    workspace.submission_file.write_text("Algebra")
    registry = SearchRegistry(workspace, "rg")

    def files(path, pattern="*.lean", **options):
        return asyncio.run(search.file_search(registry, {
            "directory_path": path, "pattern": pattern, **options,
        }))["matches"]

    assert files(".") == ["Submission/Helper.lean", "Submission.lean", "target/Mathlib/Basic.lean"]
    assert files("target") == ["target/Mathlib/Basic.lean"]
    assert files(".", "target/Mathlib/*.lean", recursive=False) == ["target/Mathlib/Basic.lean"]
    assert files(".", recursive=False) == ["Submission.lean"]
    assert files(".", limit=1) == ["Submission/Helper.lean"]
    for pattern in ("../private/*.lean", "/tmp/*.lean"):
        with pytest.raises(ValueError, match="within the requested workspace"):
            files(".", pattern)


def test_root_content_search_includes_only_authorized_target_mount(registry, tmp_path):
    library = tmp_path / "mathlib"
    library.mkdir()
    (library / "Basic.lean").write_text("Algebra in target\n")
    (registry.workspace.submission_dir / "Helper.lean").write_text("Algebra in helper\n")
    (registry.workspace.workspaces_dir / "escape").symlink_to(library, target_is_directory=True)
    registry.workspace.target_workspace = library
    registry.workspace.setup()
    result = run(registry, path=".")
    assert result["success"]
    assert [item["file"] for item in result["results"]] == [
        "Submission/Helper.lean", "target/Basic.lean",
    ]


def test_rg_search_stops_before_remaining_enumeration_and_does_not_reread(tmp_path, monkeypatch):
    if shutil.which("rg") is None:
        pytest.skip("rg is not installed")
    workspace = Workspace(tmp_path / "run")
    workspace.setup()
    first, second = [workspace.submission_dir / name for name in ("A.lean", "B.lean")]
    first.write_text("before\nAlgebra first\nafter\n")
    second.write_text("Algebra second\n")

    async def candidates(*args, **kwargs):
        yield [first, second], False, None
        raise AssertionError("Do not finish a full-tree enumeration after the result limit")

    def no_reread(*args, **kwargs):
        raise AssertionError("Use rg JSON context instead of rereading matched files")

    monkeypatch.setattr(search, "_rg_search_candidates", candidates)
    monkeypatch.setattr(Path, "read_text", no_reread)
    result = run(SearchRegistry(workspace, "rg"), limit=1)
    assert result["success"] and result["file_limit_reached"]
    assert result["results"][0]["context"] == "before\nAlgebra first\nafter"


def test_rg_timeout_preserves_inflight_context_and_reaps_children(tmp_path, monkeypatch):
    import sys
    workspace = Workspace(tmp_path / "run")
    workspace.setup()
    source = workspace.submission_dir / "A.lean"
    source.write_text("before\nAlgebra\nafter\n")
    fake_rg = tmp_path / "rg"
    fake_rg.write_text(
        f"#!{sys.executable}\n"
        "import json,sys,time\n"
        f"path={str(source)!r}\n"
        "if '--files' in sys.argv:\n"
        "    sys.stdout.write(path+'\\0');sys.stdout.flush()\n"
        "else:\n"
        "    for kind,number,text in [('context',1,'before\\n'),('match',2,'Algebra\\n'),('context',3,'after\\n')]:\n"
        "        print(json.dumps({'type':kind,'data':{'path':{'text':path},'line_number':number,'lines':{'text':text}}}),flush=True)\n"
        "    time.sleep(10)\n"
    )
    fake_rg.chmod(0o755)
    monkeypatch.setattr(search.shutil, "which", lambda _: str(fake_rg))
    monkeypatch.setenv("DAVINCI_CONTENT_SEARCH_TIMEOUT", "0.5")
    spawn = asyncio.create_subprocess_exec
    children = []

    async def record_child(*args, **kwargs):
        proc = await spawn(*args, **kwargs)
        children.append(proc)
        return proc

    monkeypatch.setattr(search.asyncio, "create_subprocess_exec", record_child)
    result = run(SearchRegistry(workspace, "rg"))
    assert not result["success"] and result["truncated"]
    assert "timed out" in result["error"]
    assert result["count"] == 1
    assert result["results"][0]["context"] == "before\nAlgebra\nafter"
    assert children and all(proc.returncode is not None for proc in children)


def test_python_timeout_includes_lazy_walk_and_preserves_previous_files(tmp_path, monkeypatch):
    base = tmp_path / "sources"
    base.mkdir()
    (base / "A.lean").write_text("Algebra first\n")
    (base / "B.lean").write_text("Algebra second\n")
    now = [0.0]
    read = Path.read_text

    def delayed_read(path, *args, **kwargs):
        value = read(path, *args, **kwargs)
        if path.name == "B.lean":
            now[0] = 31.0
        return value

    monkeypatch.setattr(search, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(Path, "read_text", delayed_read)
    result = search._content_search_python(base, "target", "Algebra", False, True, 20)
    assert not result["success"] and result["truncated"]
    assert [item["file"] for item in result["results"]] == ["target/A.lean"]
