"""Pytest configuration for aioimaplib tests.

Uses pytest-asyncio with ``asyncio_mode = "auto"`` (configured in
pyproject.toml) so that async test functions are collected and executed
automatically -- no per-test marker required.
"""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
