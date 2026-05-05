"""Test configuration for bush_stt.

bush_stt/__init__.py imports `bushutil` at module load time. To allow pytest
to import bush_stt.vad without requiring an installed workspace, we add the
sibling workspace package to sys.path here.
"""
from __future__ import annotations

import pathlib
import sys


_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_BUSHUTIL_SRC = _REPO_ROOT / "packages" / "bushutil" / "src"
if _BUSHUTIL_SRC.exists():
    sys.path.insert(0, str(_BUSHUTIL_SRC))
