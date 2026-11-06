"""Translate runtime arguments into the unchanged episode contract."""

from __future__ import annotations

import argparse
from dvp.config import BudgetConfig, EpisodeConfig


def episode_config_from_args(args: argparse.Namespace) -> EpisodeConfig:
    return EpisodeConfig(
        budget=BudgetConfig(
            max_turns=args.max_turns,
            max_tool_calls=args.max_tool_calls,
            max_lean_calls=args.max_lean_calls,
        ),
        enable_lean_retrieve=getattr(args, "lean_retrieve", True),
        abort_on_infrastructure_error=not args.continue_on_infrastructure_error,
        max_tool_result_chars=args.max_tool_result_chars,
        max_consecutive_format_errors=getattr(args, "max_consecutive_format_errors", 3),
        max_consecutive_output_truncations=getattr(args, "max_consecutive_output_truncations", 5),
        retain_truncated_thinking=getattr(args, "retain_truncated_thinking", False),
    )
