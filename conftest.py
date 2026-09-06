"""Ensure tests import this checkout's ``src`` tree, not an editable install.

Worktree-based runs (and fresh clones without ``pip install -e .``) must test
the code in *this* repository checkout; a globally installed editable copy of
the package must never shadow it.
"""
from pathlib import Path
import sys

_SRC = Path(__file__).resolve().parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
