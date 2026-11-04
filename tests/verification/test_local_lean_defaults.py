"""Check local verification selection without invoking Lean or a model service."""

import pytest

from dvp.cli.evaluate import build_parser as build_eval_parser
from dvp.cli.prove import build_parser as build_prove_parser
from dvp.cli.reverify import build_parser as build_reverify_parser


@pytest.mark.parametrize("build_parser", [build_prove_parser, build_eval_parser])
def test_cli_ignores_removed_verifier_environment(monkeypatch, build_parser):
    monkeypatch.delenv("DAVINCI_LEAN_API_URL", raising=False)
    monkeypatch.delenv("DAVINCI_LEAN_API_KEY", raising=False)
    original = vars(build_parser().parse_args([]))
    monkeypatch.setenv("DAVINCI_LEAN_API_URL", "http://retired-verifier.invalid:8765")
    monkeypatch.setenv("DAVINCI_LEAN_API_KEY", "retired-verifier-key")
    monkeypatch.setenv("LEAN_SERVER_API_KEY", "retired-server-key")
    assert vars(build_parser().parse_args([])) == original
    assert "lean_api_url" not in original and "lean_api_key" not in original


@pytest.mark.parametrize("build_parser,required", [
    (build_prove_parser, []),
    (build_eval_parser, []),
    (build_reverify_parser, ["--audit-candidates", "audit.json", "--output-dir", "output",
                            "--target-workspace", "workspace"]),
])
@pytest.mark.parametrize("option,value", [
    ("--lean-api-url", "local"),
    ("--lean-api-url", "http://retired-verifier.invalid:8765"),
    ("--lean-api-key", "retired-verifier-key"),
])
def test_removed_verifier_options_are_rejected(build_parser, required, option, value, capsys):
    with pytest.raises(SystemExit) as error:
        build_parser().parse_args([*required, option, value])
    assert error.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err
