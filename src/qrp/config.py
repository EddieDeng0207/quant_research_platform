"""Small configuration helpers with no secret-bearing output."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict


def load_env_file(path: Path = Path(".env"), override: bool = False) -> Dict[str, str]:
    """Load simple KEY=VALUE entries from a local, git-ignored environment file.

    Values are returned only so callers can test the parser; production callers
    should ignore the return value and read the process environment normally.
    """
    loaded: Dict[str, str] = {}
    if not path.exists():
        return loaded
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            raise ValueError(f"Invalid environment entry at {path}:{line_number}")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or not key.replace("_", "").isalnum():
            raise ValueError(f"Invalid environment key at {path}:{line_number}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        loaded[key] = value
        if override or key not in os.environ:
            os.environ[key] = value
    return loaded
