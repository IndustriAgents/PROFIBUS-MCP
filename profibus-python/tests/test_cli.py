"""CLI: .env from the working directory only, clean startup errors."""

from __future__ import annotations

import os

import pytest

from profibus_mcp import cli


def test_env_file_is_loaded_from_the_working_directory(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("PROFIBUS_BACKEND=mock\nPROFIBUS_MOCK_PORT=7123\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert cli.load_env_file() == tmp_path / ".env"
    assert os.environ["PROFIBUS_BACKEND"] == "mock"
    assert os.environ["PROFIBUS_MOCK_PORT"] == "7123"


def test_env_file_in_a_parent_directory_is_ignored(tmp_path, monkeypatch):
    # dotenv's default search walks up the directory tree and could pick up an
    # unrelated .env (for example the IndustriConnect checkout's).
    (tmp_path / ".env").write_text("PROFIBUS_WRITES_ENABLED=true\n", encoding="utf-8")
    child = tmp_path / "child"
    child.mkdir()
    monkeypatch.chdir(child)
    assert cli.load_env_file() is None
    assert "PROFIBUS_WRITES_ENABLED" not in os.environ


def test_real_environment_wins_over_env_file(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("PROFIBUS_MOCK_PORT=7123\n", encoding="utf-8")
    monkeypatch.setenv("PROFIBUS_MOCK_PORT", "7999")
    cli.load_env_file(tmp_path)
    assert os.environ["PROFIBUS_MOCK_PORT"] == "7999"


def test_env_file_reaches_every_setting(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(
        "PROFIBUS_BACKEND=mock\nPROFIBUS_BAUDRATE=187500\nPROFIBUS_MASTER_ADDRESS=3\nPROFIBUS_TIMEOUT=50\n"
        "PROFIBUS_WRITES_ENABLED=true\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    captured = {}

    class FakeServer:
        def __init__(self, config):
            captured["config"] = config

        def run(self):
            captured["ran"] = True

    monkeypatch.setattr(cli, "ProfibusMCPServer", FakeServer)
    cli.main()
    config = captured["config"]
    assert captured["ran"]
    assert (config.backend, config.baudrate, config.master_address, config.timeout_ms) == ("mock", 187500, 3, 50)
    assert config.writes_enabled is True


def test_configuration_error_exits_cleanly(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PROFIBUS_BAUDRATE", "fast")
    with pytest.raises(SystemExit) as info:
        cli.main()
    assert info.value.code == 2
    out, err = capsys.readouterr()
    assert out == ""  # stdout belongs to MCP
    assert "configuration error" in err and "PROFIBUS_BAUDRATE" in err
    assert "Traceback" not in err


def test_unknown_port_url_scheme_is_a_configuration_error(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PROFIBUS_PORT", "sockt://127.0.0.1:7100")
    with pytest.raises(SystemExit) as info:
        cli.main()
    assert info.value.code == 2
    assert "PROFIBUS_PORT" in capsys.readouterr().err
