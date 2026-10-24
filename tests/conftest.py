"""Pytest configuration and environment setup."""

import sys
from pathlib import Path

repo_root = Path(__file__).resolve().parent.parent
code_dir = repo_root / "code"

for p in [str(code_dir), str(repo_root)]:
    if p not in sys.path:
        sys.path.insert(0, p)
