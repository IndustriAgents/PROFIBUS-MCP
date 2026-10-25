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


@pytest.fixture(scope="session")
def _canonical_output_validators():
    """Read the reviewed schema once; exercise it with real MCP responses below."""
    import json
    from pathlib import Path

    from jsonschema import Draft202012Validator

    contract = json.loads((Path(__file__).resolve().parents[2] / "contract/tools.json").read_text())
    return {tool["name"]: Draft202012Validator(tool["outputSchema"]) for tool in contract["tools"]}


@pytest.fixture(autouse=True)
def _validate_canonical_wire_results(monkeypatch, _canonical_output_validators):
    """Validate success, error and partial results from real stdio integration calls.

    Hidden legacy aliases can have their own compatibility result format, so this
    enforces the output guarantee only for the advertised canonical tool names.
    """
    import json

    from mcp import ClientSession

    original = ClientSession.call_tool

    async def checked(session, name, *args, **kwargs):
        result = await original(session, name, *args, **kwargs)
        validator = _canonical_output_validators.get(name)
        if validator is not None:
            value = result.structuredContent
            assert isinstance(value, dict), f"{name}: missing structured result"
            validator.validate(value)
            assert result.isError == (value["success"] is False), name
            assert json.loads(result.content[0].text) == value, name
        return result

    monkeypatch.setattr(ClientSession, "call_tool", checked)
