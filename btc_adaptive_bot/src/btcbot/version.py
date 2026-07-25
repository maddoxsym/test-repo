"""Software version and git provenance, recorded with every experiment."""

from __future__ import annotations

import subprocess
from functools import lru_cache
from pathlib import Path

__version__ = "1.0.0"


@lru_cache(maxsize=1)
def git_commit() -> str:
    """Short git commit of the working tree, or ``"unknown"`` outside a repo.

    Recorded in the experiment row so a result set can always be traced back to
    the exact code that produced it.
    """
    try:
        root = Path(__file__).resolve().parents[2]
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode == 0:
            commit = result.stdout.strip()
            dirty = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=root, capture_output=True, text=True, timeout=5, check=False,
            )
            if dirty.returncode == 0 and dirty.stdout.strip():
                return f"{commit}-dirty"
            return commit
    except (OSError, subprocess.SubprocessError):
        pass
    return "unknown"
