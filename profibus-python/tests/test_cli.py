"""CLI: .env handling (no safety gates from an implicit .env), clean startup errors."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from profibus_mcp import cli


class CapturedServer:
    """Stands in for ProfibusMCPServer so main() returns instead of serving stdio."""

    configs: list = []

    def __init__(self, config):
        CapturedServer.configs.append(config)

    def run(self):
        pass


@pytest.fixture
def captured(monkeypatch) -> list:
    CapturedServer.configs = []
    monkeypatch.setattr(cli, "ProfibusMCPServer", CapturedServer)
    return CapturedServer.configs


def test_env_file_is_loaded_from_the_working_directory(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("PROFIBUS_BACKEND=mock\nPROFIBUS_MOCK_PORT=7123\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert cli.load_env_file() == (tmp_path / ".env", [])
    assert os.environ["PROFIBUS_BACKEND"] == "mock"
    assert os.environ["PROFIBUS_MOCK_PORT"] == "7123"


def test_env_file_in_a_parent_directory_is_ignored(tmp_path, monkeypatch):
    # dotenv's default search walks up the directory tree and could pick up an
    # unrelated .env (for example the IndustriConnect checkout's).
    (tmp_path / ".env").write_text("PROFIBUS_BACKEND=mock\n", encoding="utf-8")
    child = tmp_path / "child"
    child.mkdir()
    monkeypatch.chdir(child)
    assert cli.load_env_file() == (None, [])
    assert "PROFIBUS_BACKEND" not in os.environ


def test_real_environment_wins_over_env_file(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("PROFIBUS_MOCK_PORT=7123\n", encoding="utf-8")
    monkeypatch.setenv("PROFIBUS_MOCK_PORT", "7999")
    cli.load_env_file(tmp_path)
    assert os.environ["PROFIBUS_MOCK_PORT"] == "7999"


def test_implicit_env_file_cannot_enable_writes(tmp_path, monkeypatch, captured, caplog):
    # The working directory of a client-launched server may be an untrusted
    # project: its .env may set connection settings but never a safety gate.
    (tmp_path / ".env").write_text(
        "PROFIBUS_BACKEND=mock\nPROFIBUS_WRITES_ENABLED=true\nPROFIBUS_CONFIG_CMDS_ENABLED=true\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    with caplog.at_level(logging.WARNING, logger="profibus_mcp"):
        cli.main([])
    config = captured[0]
    assert config.backend == "mock"  # connection settings still apply
    assert config.writes_enabled is False
    assert "PROFIBUS_WRITES_ENABLED" not in os.environ
    warnings = [record.getMessage() for record in caplog.records if record.levelno == logging.WARNING]
    assert any(m.startswith("PROFIBUS_WRITES_ENABLED in ") and "is ignored" in m for m in warnings), warnings
    assert any(m.startswith("PROFIBUS_CONFIG_CMDS_ENABLED in ") for m in warnings), warnings


def test_ignored_gate_warning_goes_to_stderr(tmp_path, monkeypatch, captured, capsys):
    (tmp_path / ".env").write_text("PROFIBUS_WRITES_ENABLED=yes\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    root = logging.getLogger()
    saved = root.handlers[:]
    root.handlers = []  # let main() install its stderr handler
    try:
        cli.main([])
    finally:
        root.handlers = saved
    out, err = capsys.readouterr()
    assert out == ""
    assert "PROFIBUS_WRITES_ENABLED" in err and "is ignored" in err


def test_real_environment_can_enable_writes(tmp_path, monkeypatch, captured):
    (tmp_path / ".env").write_text("PROFIBUS_WRITES_ENABLED=false\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PROFIBUS_WRITES_ENABLED", "true")
    cli.main([])
    assert captured[0].writes_enabled is True


def test_explicit_env_file_may_enable_writes(tmp_path, monkeypatch, captured):
    settings = tmp_path / "bench.env"
    settings.write_text("PROFIBUS_BACKEND=mock\nPROFIBUS_WRITES_ENABLED=true\n", encoding="utf-8")
    # An implicit .env in the working directory is not read when --env-file is given.
    (tmp_path / ".env").write_text("PROFIBUS_MOCK_PORT=7555\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    cli.main(["--env-file", str(settings)])
    config = captured[0]
    assert config.writes_enabled is True and config.backend == "mock"
    assert config.mock_port == 7100


def test_missing_explicit_env_file_is_a_configuration_error(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as info:
        cli.main(["--env-file", str(tmp_path / "missing.env")])
    assert info.value.code == 2
    assert "--env-file" in capsys.readouterr().err


def test_env_file_reaches_every_connection_setting(tmp_path, monkeypatch, captured):
    (tmp_path / ".env").write_text(
        "PROFIBUS_BACKEND=mock\nPROFIBUS_BAUDRATE=187500\nPROFIBUS_MASTER_ADDRESS=3\nPROFIBUS_TIMEOUT=50\n"
        "PROFIBUS_MOCK_HOST=localhost\nPROFIBUS_MAX_RETRIES=1\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    cli.main([])
    config = captured[0]
    assert (config.backend, config.baudrate, config.master_address, config.timeout_ms) == ("mock", 187500, 3, 50)
    assert (config.mock_host, config.max_retries) == ("localhost", 1)


def test_configuration_error_exits_cleanly(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PROFIBUS_BAUDRATE", "fast")
    with pytest.raises(SystemExit) as info:
        cli.main([])
    assert info.value.code == 2
    out, err = capsys.readouterr()
    assert out == ""  # stdout belongs to MCP
    assert "configuration error" in err and "PROFIBUS_BAUDRATE" in err
    assert "Traceback" not in err


def test_unknown_port_url_scheme_is_a_configuration_error(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PROFIBUS_PORT", "hwgrep://.*")
    with pytest.raises(SystemExit) as info:
        cli.main([])
    assert info.value.code == 2
    assert "PROFIBUS_PORT" in capsys.readouterr().err


def test_is_safety_gate():
    assert cli.is_safety_gate("PROFIBUS_WRITES_ENABLED")
    assert cli.is_safety_gate("PROFIBUS_CONFIG_CMDS_ENABLED")
    assert not cli.is_safety_gate("PROFIBUS_BACKEND")
    assert not cli.is_safety_gate("OTHER_WRITES_ENABLED")


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as info:
        cli.main(["--version"])
    assert info.value.code == 0
    from importlib.metadata import version

    assert capsys.readouterr().out.strip() == f"profibus-mcp {version('profibus-mcp')}"


def test_env_example_documents_every_setting():
    example = Path(__file__).resolve().parents[1] / ".env.example"
    text = example.read_text(encoding="utf-8")
    for name in ("PROFIBUS_BACKEND", "PROFIBUS_MOCK_HOST", "PROFIBUS_MOCK_PORT", "PROFIBUS_WRITES_ENABLED"):
        assert name in text
    # The gate appears only commented out, with the reason.
    assert "\nPROFIBUS_WRITES_ENABLED" not in text and "--env-file" in text
