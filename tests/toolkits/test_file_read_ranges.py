"""File reads preserve source content and identify exactly which lines returned."""

import asyncio
from types import SimpleNamespace

import pytest

from dvp.runtime.workspace import Workspace
from dvp.agent.result_projection import _compact, _json
from dvp.toolkits.files import file_read


@pytest.fixture
def read_file(tmp_path):
    workspace = Workspace(root=tmp_path / "run")
    workspace.setup()
    registry = SimpleNamespace(workspace=workspace)

    def read(content, **arguments):
        (workspace.submission_dir / "source.lean").write_text(content, encoding="utf-8")
        return asyncio.run(file_read(registry, {
            "file_path": "Submission/source.lean", **arguments,
        }))

    return read


@pytest.mark.parametrize("line_range, expected_range, expected_lines", [
    ([2, 4], [2, 4], [2, 3]),
    ([-3, None], [3, 6], [3, 4, 5]),
    ([None, -1], [1, 5], [1, 2, 3, 4]),
    ([-4, -1], [2, 5], [2, 3, 4]),
    ([2], [2, 6], [2, 3, 4, 5]),
    ([None, None], [1, 6], [1, 2, 3, 4, 5]),
    ([-99, None], [1, 6], [1, 2, 3, 4, 5]),
    ([1, -6], [1, 1], []),
    ([99, None], [6, 6], []),
    ([4, 2], [4, 4], []),
    ([0, None], [6, 6], []),
    ([], [1, 6], [1, 2, 3, 4, 5]),
])
def test_line_ranges_are_half_open_and_preserve_negative_indices(
    read_file, line_range, expected_range, expected_lines,
):
    result = read_file("one\ntwo\nthree\nfour\nfive\n", line_range=line_range)
    assert result["success"]
    assert result["total_lines"] == 5
    assert result["requested_line_range"] == line_range
    assert result["line_range"] == result["returned_line_range"] == expected_range
    returned = result["content"].splitlines()
    assert [int(line.split("|", 1)[0]) for line in returned] == expected_lines
    assert result["remaining_lines"] == 6 - expected_range[1]
    assert result["next_line_range"] == (
        [expected_range[1], 6] if expected_range[1] < 6 else None
    )
    assert result["truncated"] is False


def test_default_page_names_continuation_and_reaches_eof_without_skips(read_file):
    content = "\n".join(f"declaration {number}" for number in range(1, 452))
    result = read_file(content)
    assert result["requested_line_range"] is None
    assert result["total_lines"] == 451
    assert result["returned_line_range"] == [1, 201]
    assert result["remaining_lines"] == 251
    assert result["next_line_range"] == [201, 401]
    returned = result["content"].splitlines()

    while result["next_line_range"]:
        result = read_file(content, line_range=result["next_line_range"])
        returned.extend(result["content"].splitlines())

    assert returned == [
        f"{number:6d}|declaration {number}" for number in range(1, 452)
    ]
    assert result["returned_line_range"] == [401, 452]
    assert result["remaining_lines"] == 0


def test_selected_content_is_not_subject_to_a_backend_character_limit(read_file):
    lines = ["α" * 5000, "β" * 9000, "theorem final_target : True := by trivial"]
    result = read_file("\n".join(lines), line_range=[1, None])
    assert result["content"] == "\n".join(
        f"{number:6d}|{line}" for number, line in enumerate(lines, 1)
    )
    assert len(result["content"]) > 12000
    assert result["returned_line_range"] == [1, 4]
    assert result["truncated"] is False
    assert result["next_line_range"] is None


def test_empty_file_has_an_empty_range_and_no_continuation(read_file):
    result = read_file("")
    assert result["total_lines"] == 0
    assert result["content"] == ""
    assert result["returned_line_range"] == [1, 1]
    assert result["remaining_lines"] == 0
    assert result["next_line_range"] is None


def test_start_column_resumes_only_the_first_selected_line(read_file):
    result = read_file("first\nαβγδε\nlast\n", line_range=[2, None], start_column=2)
    assert result["success"]
    assert result["content"] == "     2|γδε\n     3|last"
    assert result["start_column"] == 2
    assert result["requested_line_range"] == [2, None]
    assert result["returned_line_range"] == [2, 4]


def test_start_column_at_line_end_keeps_the_empty_line_and_later_lines(read_file):
    result = read_file("αβγ\nnext", line_range=[1, None], start_column=3)
    assert result["content"] == "     1|\n     2|next"
    assert result["start_column"] == 3


def test_long_single_line_can_be_resumed_without_losing_unicode_characters(read_file):
    content = "αβγ" * 6000
    first = read_file(content)
    assert "start_column" not in first
    prefix = first["content"].split("|", 1)[1][:12000]
    remaining = read_file(content, line_range=[1, 2], start_column=len(prefix))
    assert remaining["start_column"] == 12000
    assert prefix + remaining["content"].split("|", 1)[1] == content


@pytest.mark.parametrize("start_column", [None, "2", 1.5, True, False])
def test_invalid_column_types_are_reported_as_argument_errors(read_file, start_column):
    result = read_file("abc", start_column=start_column)
    assert result == {
        "success": False,
        "error": "TypeError: start_column must be a non-negative integer",
    }


def test_negative_column_is_reported_as_an_argument_error(read_file):
    result = read_file("abc", start_column=-1)
    assert result == {
        "success": False,
        "error": "ValueError: start_column must be a non-negative integer",
    }


@pytest.mark.parametrize("content, line_range, start_column, length", [
    ("αβγ\nlonger line", [1, None], 4, 3),
    ("abc", [1, 1], 1, 0),
    ("", None, 1, 0),
])
def test_column_beyond_selected_first_line_is_rejected(
    read_file, content, line_range, start_column, length,
):
    result = read_file(content, line_range=line_range, start_column=start_column)
    assert result == {
        "success": False,
        "error": (
            "ValueError: start_column exceeds the selected first line length "
            f"({length})"
        ),
    }


def _read_projected_pages(read_file, source_lines, *, budget=12000, start_column=0):
    """Follow actual continuation arguments and rebuild complete source lines."""
    content = "\n".join(source_lines)
    arguments = {"line_range": [1, None], "start_column": start_column}
    pages = []
    completed_lines = []
    current_line = 1
    current_column = start_column
    current_content = ""
    seen_requests = set()
    for _ in range(200):
        request_key = (tuple(arguments["line_range"]), arguments.get("start_column", 0))
        assert request_key not in seen_requests, "continuation repeated without progress"
        seen_requests.add(request_key)
        raw = read_file(content, **arguments)
        assert raw["success"]
        page = _compact(raw, max_chars=budget, tool_name="file_read")
        assert len(_json(page)) <= budget
        pages.append(page)
        if not page["success"]:
            assert page.get("error")
            assert not page.get("next_read"), "an error must not create an endless continuation"
            return completed_lines, pages

        rendered_lines = page["content"].splitlines()
        assert rendered_lines, "a nonempty remaining file must return content or an explicit error"
        for rendered_line in rendered_lines:
            label, separator, body = rendered_line.partition("|")
            assert separator
            number = int(label)
            assert number == current_line
            expected_line = source_lines[current_line - 1]
            assert body == expected_line[current_column:current_column + len(body)]
            current_content += body
            if page.get("partial_line") == number:
                assert len(rendered_lines) == 1
                assert body, "a partial line must consume characters"
                assert page["returned_columns"] == [current_column, current_column + len(body)]
                current_column += len(body)
                assert current_column < len(expected_line)
                assert page["next_read"]["start_column"] == current_column
            else:
                # Normal pages must finish every displayed line, including a
                # continued long line; no newline is added between fragments.
                assert body == expected_line[current_column:]
                completed_lines.append(current_content)
                current_content = ""
                current_column = 0
                current_line += 1

        next_read = page.get("next_read")
        if not next_read:
            next_range = page.get("next_line_range")
            if next_range:
                next_read = {"file_path": "Submission/source.lean", "line_range": next_range}
        if not next_read:
            assert current_line == len(source_lines) + 1
            assert not current_content
            return completed_lines, pages
        assert next_read["file_path"] == "Submission/source.lean"
        arguments = {key: value for key, value in next_read.items() if key != "file_path"}
    pytest.fail("continuation did not reach EOF in 200 pages")


def test_projection_keeps_file_content_above_old_four_thousand_character_limit(read_file):
    source_lines = [f"theorem declaration_{index} : True := by trivial" for index in range(120)]
    completed_lines, pages = _read_projected_pages(read_file, source_lines)
    assert len(pages) == 1
    assert 4000 < len(pages[0]["content"]) < 12000
    assert completed_lines == source_lines
    assert pages[0]["truncated"] is False


def test_projection_paginates_multiple_complete_lines_without_skips_or_duplicates(read_file):
    source_lines = [f"-- line {index}: " + "αβγ " * 17 for index in range(452)]
    completed_lines, pages = _read_projected_pages(read_file, source_lines, budget=1800)
    assert len(pages) > 2
    assert all("partial_line" not in page for page in pages)
    assert completed_lines == source_lines


@pytest.mark.parametrize("start_column", [0, 137])
def test_projection_resumes_long_unicode_line_then_reads_following_lines(read_file, start_column):
    source_lines = ["αβγδ" * 7000, "", "theorem target : True := by trivial", "-- final line"]
    completed_lines, pages = _read_projected_pages(
        read_file, source_lines, budget=12000, start_column=start_column,
    )
    assert sum("partial_line" in page for page in pages) >= 2
    assert completed_lines == [source_lines[0][start_column:], *source_lines[1:]]


def test_projection_accounts_for_json_escaping_when_paging_source(read_file):
    source_lines = ['-- "quoted" \\ path \\ ' * 800, 'def escaped := "\\\\quoted\\\\"', "-- done"]
    completed_lines, pages = _read_projected_pages(read_file, source_lines, budget=1600)
    assert len(pages) > 2
    assert any("partial_line" in page for page in pages)
    assert completed_lines == source_lines


def test_small_projection_budget_returns_progress_or_an_explicit_error(read_file):
    source_lines = ["α\\\"" * 1000, "-- following line"]
    completed_lines, pages = _read_projected_pages(read_file, source_lines, budget=512)
    if pages[-1]["success"]:
        assert completed_lines == source_lines
    else:
        assert "budget" in pages[-1]["error"].lower()
