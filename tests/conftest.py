"""Pytest configuration for the test suite.

Ensures the repository root is importable so tests can ``import shared.*``
regardless of the directory pytest is invoked from.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
