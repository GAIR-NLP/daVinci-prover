"""Shared helpers for the pinned LeanExplore description-generation pipeline."""

from __future__ import annotations

import hashlib
from pathlib import Path
import sqlite3
import subprocess
import sys


PIN = "17b9d6c87a2bf0dc4ea27de05da8a1c9965391b7"


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def readonly(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def git(path: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(path), *args], text=True).strip()


def load_upstream(source: Path) -> dict:
    if git(source, "rev-parse", "HEAD") != PIN:
        raise ValueError("Unexpected upstream revision")
    if git(source, "status", "--porcelain", "--untracked-files=no"):
        raise ValueError("Tracked upstream source files have been modified")
    sys.path.insert(0, str(source / "src"))
    from lean_explore.extract import _cache
    from lean_explore.extract.informalize import pipeline, generation, dependency_layers
    from lean_explore import models
    from lean_explore.util import openrouter_client

    modules = {
        "pipeline": pipeline, "generation": generation,
        "dependency_layers": dependency_layers, "cache": _cache,
        "models": models, "client": openrouter_client,
    }
    for module in modules.values():
        if not Path(module.__file__).resolve().is_relative_to(source / "src"):
            raise ValueError(f"Imported a different LeanExplore installation: {module.__file__}")
    return modules
