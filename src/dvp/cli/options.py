"""Shared model, tools, and resource options for proving commands."""

from __future__ import annotations

import argparse
import math
import os
from pathlib import Path

from dvp.runtime.paths import DEFAULT_TARGET_WORKSPACE


def _default_retrieve_index() -> Path | None:
    """Select JSONL only when the caller explicitly chooses the local backend."""

    raw_path = os.getenv("DAVINCI_RETRIEVE_INDEX", "").strip()
    backend = os.getenv("DAVINCI_RETRIEVE_BACKEND", "lean_explore").strip().lower()
    if raw_path and backend in {"local", "jsonl"}:
        return Path(raw_path)
    return None


def _parse_optional_bool(value: str | None) -> bool | None:
    if value is None or value.strip().lower() in {"", "none", "auto"}:
        return None
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on", "enabled"}:
        return True
    if normalized in {"0", "false", "no", "off", "disabled"}:
        return False
    raise argparse.ArgumentTypeError(f"invalid boolean value: {value!r}")


def _parse_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "on"}:
        return True
    if normalized in {"0", "false", "off"}:
        return False
    raise argparse.ArgumentTypeError(
        f"invalid boolean value: {value!r}; expected 0/1, true/false, or on/off"
    )


def _parse_nonnegative_int(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("expected a non-negative integer")
    return number


def _parse_positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("expected a positive integer")
    return number


def _parse_positive_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("expected a positive finite number")
    return number


def add_python_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--python-execute", type=_parse_bool, default=True,
        help="Enable local Python experiments with Landlock and seccomp (default: true; use false to disable).",
    )
    parser.add_argument(
        "--python-timeout", type=_parse_positive_float, default=30.0,
        help="Maximum seconds per Python experiment (default: 30).",
    )
    parser.add_argument(
        "--python-memory-mb", type=_parse_positive_int, default=1024,
        help="Python process address-space limit in MiB (default: 1024).",
    )
    parser.add_argument(
        "--python-max-output-bytes", type=_parse_positive_int, default=12000,
        help="Combined Python stdout/stderr limit (default: 12000 bytes).",
    )


def _env_float(name: str) -> float | None:
    value = os.getenv(name)
    return float(value) if value not in (None, "") else None


def _env_int(name: str) -> int | None:
    value = os.getenv(name)
    return int(value) if value not in (None, "") else None


def _env_int_list(name: str) -> list[int] | None:
    value = os.getenv(name)
    if value in (None, ""):
        return None
    return [int(item.strip()) for item in value.split(",") if item.strip()]


def add_runtime_options(parser: argparse.ArgumentParser) -> None:
    add_python_options(parser)
    parser.add_argument(
        "--target-workspace",
        type=Path,
        default=Path(
            os.getenv("DAVINCI_TARGET_WORKSPACE", str(DEFAULT_TARGET_WORKSPACE))
        ),
        help="Lean project root used as a read-only target workspace.",
    )
    parser.add_argument(
        "--run-dir",
        type=Path,
        help="Run directory. Defaults to ./runs/dvp_<timestamp>.",
    )
    parser.add_argument(
        "--lean-retrieve",
        type=_parse_bool,
        default=os.getenv("DAVINCI_LEAN_RETRIEVE", "true"),
        help=(
            "Enable indexed Lean retrieval (default: DAVINCI_LEAN_RETRIEVE or true). "
            "Use false to hide and reject lean_retrieve; file search/read remain available."
        ),
    )
    parser.add_argument(
        "--retrieve-index",
        type=Path,
        default=_default_retrieve_index(),
        help=(
            "Optional local JSONL retrieval index. Supplying this switches DVP "
            "from the default LeanExplore backend to deterministic JSONL search."
        ),
    )
    parser.add_argument(
        "--model",
        default=os.getenv("DAVINCI_MODEL") or os.getenv("OPENAI_MODEL"),
        help="Served model name, or a LiteLLM provider/model identifier.",
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("OPENAI_BASE_URL"),
        help="Optional API base URL; omit for the provider default.",
    )
    parser.add_argument("--api-key", default=os.getenv("OPENAI_API_KEY"))
    parser.add_argument("--max-turns", type=int, default=50)
    parser.add_argument("--max-tool-calls", type=int, default=200)
    parser.add_argument(
        "--max-consecutive-format-errors",
        type=_parse_nonnegative_int,
        default=3,
        help=(
            "End an attempt after this many consecutive tool-format errors "
            "(default: 3; 0 disables the limit). Output truncations have a separate limit."
        ),
    )
    parser.add_argument(
        "--max-consecutive-output-truncations",
        type=_parse_nonnegative_int,
        default=5,
        help=(
            "End an attempt after this many consecutive output-truncated responses "
            "(default: 5; 0 disables this limit). Turn and tool budgets still apply."
        ),
    )
    parser.add_argument(
        "--retain-truncated-thinking",
        type=_parse_bool,
        default=False,
        help=(
            "Keep reasoning from output-truncated responses in the next request's "
            "history (default: false). When enabled, clearly repetitive reasoning "
            "is still excluded under the current history policy. "
            "Visible partial text is retained in either mode; malformed tool "
            "calls are never resent. This does not change recovery counters "
            "or enable native token-level continuation."
        ),
    )
    parser.add_argument(
        "--history-policy",
        choices=("current", "legacy"),
        default="current",
        help=(
            "Response history policy: current uses selective truncation recovery; "
            "legacy keeps normalized assistant/tool exchanges, including replies "
            "without tools and rejected calls. Limits are configured separately. "
            "Legacy overrides --retain-truncated-thinking."
        ),
    )
    parser.add_argument(
        "--trace-level",
        choices=("off", "sft", "full"),
        default="full",
        help=(
            "off=result only; sft=conversation without lossless trajectory; "
            "full=conversation plus trajectory"
        ),
    )
    parser.add_argument(
        "--max-lean-calls",
        type=int,
        default=None,
        help="Optional strict cap on verification calls.",
    )
    parser.add_argument(
        "--max-tool-result-chars",
        type=int,
        default=12_000,
        help="Maximum raw tool-result text exposed in an observation.",
    )
    parser.add_argument(
        "--guidance",
        choices=("off", "soft"),
        default="off",
        help=(
            "Optional advisory workflow hints (default: off). Tool diagnostics "
            "and protocol-error feedback remain available in either mode."
        ),
    )
    parser.add_argument("--max-tokens", type=int, default=_env_int("DAVINCI_MAX_TOKENS"))
    parser.add_argument(
        "--temperature",
        type=float,
        default=_env_float("DAVINCI_TEMPERATURE"),
    )
    parser.add_argument(
        "--top-p",
        type=float,
        default=_env_float("DAVINCI_TOP_P"),
    )
    parser.add_argument(
        "--presence-penalty",
        type=float,
        default=_env_float("DAVINCI_PRESENCE_PENALTY"),
        help="Optional presence penalty sent to the model endpoint.",
    )
    parser.add_argument(
        "--reasoning-effort",
        default=os.getenv("DAVINCI_REASONING_EFFORT"),
    )
    parser.add_argument(
        "--thinking",
        choices=("enabled", "disabled"),
        default=os.getenv("DAVINCI_THINKING"),
    )
    parser.add_argument(
        "--chat-template-thinking",
        type=_parse_optional_bool,
        default=_parse_optional_bool(os.getenv("DAVINCI_CHAT_TEMPLATE_THINKING")),
    )
    parser.add_argument(
        "--chat-template-reasoning-effort",
        default=os.getenv("DAVINCI_CHAT_TEMPLATE_REASONING_EFFORT"),
    )
    parser.add_argument(
        "--stop-token-ids",
        type=int,
        nargs="*",
        default=_env_int_list("DAVINCI_STOP_TOKEN_IDS"),
    )
    parser.add_argument("--llm-timeout", type=float, default=1200.0)
    parser.add_argument(
        "--lean-timeout",
        type=float,
        default=float(os.getenv("DAVINCI_LEAN_TIMEOUT", "60")),
    )
    parser.add_argument(
        "--continue-on-infrastructure-error",
        action="store_true",
        help="Do not abort an episode after a verifier infrastructure failure.",
    )
