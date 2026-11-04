"""Project paths independent of the location of individual source modules.

An installed DVP can use ``DVP_PROJECT_ROOT`` to select a checkout containing
datasets, retrieval assets and the Lean runtime. Source checkouts are discovered
automatically; outside a checkout the current directory is the project root.
"""

from __future__ import annotations

import os
from pathlib import Path


def project_root() -> Path:
    override = os.environ.get("DVP_PROJECT_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    for start in (Path(__file__).resolve().parent, Path.cwd().resolve()):
        for candidate in (start, *start.parents):
            if (
                (candidate / "pyproject.toml").is_file()
                and (candidate / "src" / "dvp" / "__init__.py").is_file()
                and (candidate / "inputs").is_dir()
            ):
                return candidate
    return Path.cwd().resolve()


REPOSITORY_ROOT = project_root()
SOURCE_ROOT = REPOSITORY_ROOT / "src"
INPUTS_ROOT = REPOSITORY_ROOT / "inputs"
DATA_ROOT = REPOSITORY_ROOT / "data"
DEFAULT_TARGET_WORKSPACE = REPOSITORY_ROOT / "runtime" / "mathlib4-4.35"

# This is the import root of the installed package, which can differ from the
# selected project. Child parser processes must import the same implementation.
PACKAGE_IMPORT_ROOT = Path(__file__).resolve().parents[2]
