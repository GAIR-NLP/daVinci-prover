"""Tool-text envelopes cannot turn damaged output into executable calls."""

import pytest

from dvp.llm_clients.parser import looks_like_tool_call_text, parse_assistant_action


VALID_CALL = (
    '<｜DSML｜ invoke name="file_read">'
    '<｜DSML｜ parameter name="file_path" string="true">Submission.lean</｜DSML｜ parameter>'
    '</｜DSML｜ invoke>'
)
BROKEN_CALL = (
    '<｜DSML｜ parameter name="file_write">'
    '<｜DSML｜ parameter name="content" string="true">'
    '{"file_path":"Submission.lean","content":"must not be written"}'
    '</｜DSML｜ parameter></｜DSML｜ invoke></｜DSML｜ calls>'
)


@pytest.mark.parametrize("content", [
    BROKEN_CALL,
    VALID_CALL.removesuffix('</｜DSML｜ invoke>'),
    '<｜DSML｜ calls>' + VALID_CALL,
    '<｜DSML｜ calls>' + VALID_CALL + '</｜DSML｜ function_calls>',
    VALID_CALL + BROKEN_CALL,
    VALID_CALL + '<｜DSML｜ invoke name="file_write">',
    '<｜DSML｜invok name="file_write">{"code":"must not execute"}',
    '<｜DSML｜tool_calls>{"code":"must not execute"}</｜DSML｜tool_calls>',
    VALID_CALL.replace('</｜DSML｜ parameter>', ''),
    VALID_CALL.replace('</｜DSML｜ parameter>',
        '<｜DSML｜ parameter name="content">trailing</｜DSML｜ parameter>'),
    VALID_CALL.replace('</｜DSML｜ invoke>',
        '<｜DSML｜ parameter name="file_path">second</｜DSML｜ parameter></｜DSML｜ invoke>'),
    '<tool_call>{"name":"file_write","content":"embedded"}</function_call>',
    '{"name":"file_write","arguments":',
    '{"tool_calls":[',
])
def test_malformed_envelope_blocks_embedded_json_and_reasoning_fallback(content):
    message = {
        "content": content, "finish_reason": "stop",
        "reasoning_content": '{"file_path":"Submission.lean","content":"also must not be written"}',
    }
    parsed = parse_assistant_action(message, turn=1)
    assert looks_like_tool_call_text(content)
    assert parsed.tool_calls == ()
    assert parsed.content == content
    assert parsed.raw == message


@pytest.mark.parametrize("wrapper", [None, "calls", "function_calls"])
@pytest.mark.parametrize("spacing", [True, False])
def test_complete_dsml_invocations_still_parse_with_optional_marker_spacing(wrapper, spacing):
    content = VALID_CALL * 2
    if wrapper:
        content = f'<｜DSML｜ {wrapper}>{content}</｜DSML｜ {wrapper}>'
    if not spacing:
        content = content.replace('｜DSML｜ ', '｜DSML｜')
    parsed = parse_assistant_action({"content": content, "finish_reason": "stop"}, turn=4)
    assert len(parsed.tool_calls) == 2
    assert [c.id for c in parsed.tool_calls] == ['synthetic_4_1_file_read', 'synthetic_4_2_file_read']
    assert all(c.name == 'file_read' and c.arguments == {"file_path": "Submission.lean"}
               for c in parsed.tool_calls)


@pytest.mark.parametrize("content", [
    'The tool returned no answer. I could not finish.',
    'The error was `<｜DSML｜ parameter name="file_edit">`.',
    '> <｜DSML｜ parameter name="file_edit">',
    '```xml\n<｜DSML｜ parameter name="file_edit">\n```',
    'For example:\n' + VALID_CALL,
    '{"name":"theorem", "status":"incomplete"}',
])
def test_prose_and_quoted_tool_examples_are_not_classified_as_call_envelopes(content):
    assert not looks_like_tool_call_text(content)


@pytest.mark.parametrize("content", [
    'I cannot finish. Example: `{"tool":"file_write","file_path":"Submission.lean","content":"unexpected"}`',
    '```text\n<｜DSML｜ parameter name="lean_verify">{"code":"unexpected"}\n```',
    'Example:\n```json\n{"tool":"file_write","content":"unexpected"}\n```',
])
def test_prose_or_quoted_examples_cannot_execute_embedded_json(content):
    parsed = parse_assistant_action({"content": content, "finish_reason": "stop"}, turn=1)
    assert parsed.tool_calls == ()
    assert not looks_like_tool_call_text(content)


@pytest.mark.parametrize("wrapper", ["arguments", "params", "parameters", "input"])
def test_bad_explicit_argument_container_is_not_salvaged_from_siblings(wrapper):
    content = ('{"name":"file_write", "' + wrapper + '":"broken JSON",'
               '"file_path":"Submission.lean","content":"must not be written"}')
    parsed = parse_assistant_action({"content": content}, turn=1)
    assert len(parsed.tool_calls) == 1
    assert parsed.tool_calls[0].argument_parse_error
    assert parsed.tool_calls[0].arguments == {}


def test_broken_explicit_json_fence_is_still_tool_intent():
    content = '```json\n{"name":"file_write","arguments":\n```'
    assert looks_like_tool_call_text(content)
    assert parse_assistant_action({"content": content}, turn=1).tool_calls == ()


def test_native_calls_remain_authoritative_even_with_broken_text():
    native = {"id": "real", "type": "function", "function": {
        "name": "file_read", "arguments": '{"file_path":"Submission.lean"}',
    }}
    parsed = parse_assistant_action({"content": BROKEN_CALL, "tool_calls": [native]}, turn=1)
    assert len(parsed.tool_calls) == 1
    assert parsed.tool_calls[0].id == 'real'
    assert parsed.tool_calls[0].source == 'native'


@pytest.mark.parametrize("fence", [None, "json", ""])
def test_explicit_json_call_remains_parseable(fence):
    content = '{"name":"file_read","arguments":{"file_path":"Submission.lean"}}'
    if fence is not None:
        content = f'```{fence}\n{content}\n```'
    parsed = parse_assistant_action({"content": content}, turn=1)
    assert len(parsed.tool_calls) == 1
    assert parsed.tool_calls[0].name == 'file_read'
    assert parsed.tool_calls[0].arguments == {"file_path": "Submission.lean"}
