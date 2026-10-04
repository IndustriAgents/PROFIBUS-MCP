"""Shared fixtures for the profibus-mcp tests."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
DATA_DIR = TESTS_DIR / "data"
REPO_ROOT = TESTS_DIR.parents[1]
MOCK_DIR = REPO_ROOT / "profibus-mock-slave"
MOCK_GSD_DIR = MOCK_DIR / "gsd"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _isolated_environment() -> Iterator[None]:
    """Hide the developer's PROFIBUS_* settings and undo anything a test (or dotenv) sets."""
    saved = dict(os.environ)
    for name in list(os.environ):
        if name.startswith("PROFIBUS_") or name == "SLAVE_MAP_FILE":
            del os.environ[name]
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)
