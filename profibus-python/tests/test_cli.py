"""CLI: where an implicit .env may come from and what it may set; clean startup errors."""

from __future__ import annotations

import logging
import os
from importlib.metadata import version
from pathlib import Path

import pytest

from profibus_mcp import cli

GATE_FORMS = [
    "PROFIBUS_WRITES_ENABLED=true",
    "profibus_writes_enabled=true",  # Windows upper-cases environment names
    "Profibus_Writes_Enabled=yes",
    "export profibus_writes_enabled='true'",
    '  export   PROFIBUS_WRITES_ENABLED = "1"',
]
FOREIGN = ["LD_PRELOAD", "DYLD_INSERT_LIBRARIES", "PYTHONPATH", "PYTHONSTARTUP", "SSL_CERT_FILE", "HTTPS_PROXY", "PATH"]


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


@pytest.fixture
def project(tmp_path, monkeypatch) -> Path:
    """A stand-in for profibus-python/, so the developer's real .env never takes part."""
    project = tmp_path / "profibus-python"
    project.mkdir()
    monkeypatch.setattr(cli, "find_project_dir", lambda *args: project)
    return project


@pytest.fixture
def no_project(monkeypatch) -> None:
    monkeypatch.setattr(cli, "find_project_dir", lambda *args: None)


class UpperCaseEnviron(dict):
    """Behaves like os.environ on Windows: names are upper-cased."""

    def __setitem__(self, key, value):
        super().__setitem__(key.upper(), value)

    def setdefault(self, key, value=None):
        return super().setdefault(key.upper(), value)

    def __contains__(self, key):
        return super().__contains__(key.upper())


# --- where the implicit .env comes from -----------------------------------


def test_project_dir_is_this_source_checkout():
    assert cli.find_project_dir() == Path(__file__).resolve().parents[1]


def test_installed_package_has_no_implicit_env_file(tmp_path):
    installed = tmp_path / "lib" / "python3.11" / "site-packages" / "profibus_mcp"
    installed.mkdir(parents=True)
    (installed / "cli.py").write_text("", encoding="utf-8")
    (tmp_path / "lib" / "python3.11" / "pyproject.toml").write_text(
        'name = "industriagents-profibus-mcp"\n', encoding="utf-8"
    )
    assert cli.find_project_dir(installed / "cli.py") is None


def test_a_src_layout_of_another_project_is_not_ours(tmp_path):
    package = tmp_path / "other" / "src" / "profibus_mcp"
    package.mkdir(parents=True)
    (tmp_path / "other" / "pyproject.toml").write_text('name = "something-else"\n', encoding="utf-8")
    assert cli.find_project_dir(package / "cli.py") is None


def test_project_env_file_is_loaded(project):
    (project / ".env").write_text("PROFIBUS_BACKEND=mock\nPROFIBUS_MOCK_PORT=7123\n", encoding="utf-8")
    assert cli.load_env_file(project) == (project / ".env", [])
    assert os.environ["PROFIBUS_BACKEND"] == "mock"
    assert os.environ["PROFIBUS_MOCK_PORT"] == "7123"


def test_env_files_in_the_working_directory_and_its_parent_are_not_read(tmp_path, monkeypatch, project, captured):
    # The working directory of a client-launched server may be an untrusted
    # project; its settings (endpoint, maps) decide where a write goes.
    work = tmp_path / "client-project" / "sub"
    work.mkdir(parents=True)
    (work / ".env").write_text("PROFIBUS_BACKEND=mock\nPROFIBUS_MOCK_HOST=evil.example\n", encoding="utf-8")
    (work.parent / ".env").write_text("PROFIBUS_MOCK_PORT=6666\n", encoding="utf-8")
    monkeypatch.chdir(work)
    cli.main([])
    config = captured[0]
    assert (config.backend, config.mock_host, config.mock_port) == ("serial", "127.0.0.1", 7100)
    for name in ("PROFIBUS_BACKEND", "PROFIBUS_MOCK_HOST", "PROFIBUS_MOCK_PORT"):
        assert name not in os.environ


def test_no_implicit_env_file_when_installed(tmp_path, monkeypatch, no_project, captured):
    (tmp_path / ".env").write_text("PROFIBUS_BACKEND=mock\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    cli.main([])
    assert captured[0].backend == "serial"


def test_real_environment_wins_over_env_file(project, monkeypatch):
    (project / ".env").write_text("PROFIBUS_MOCK_PORT=7123\n", encoding="utf-8")
    monkeypatch.setenv("PROFIBUS_MOCK_PORT", "7999")
    cli.load_env_file(project)
    assert os.environ["PROFIBUS_MOCK_PORT"] == "7999"


# --- what the implicit .env may set ---------------------------------------


@pytest.mark.parametrize("line", GATE_FORMS)
def test_implicit_env_file_cannot_enable_writes_in_any_spelling(project, captured, caplog, line):
    (project / ".env").write_text(f"PROFIBUS_BACKEND=mock\n{line}\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="profibus_mcp"):
        cli.main([])
    config = captured[0]
    assert config.backend == "mock"  # its own connection settings still apply
    assert config.writes_enabled is False
    assert not any(name.upper() == "PROFIBUS_WRITES_ENABLED" for name in os.environ)
    warnings = [record.getMessage() for record in caplog.records if record.levelno == logging.WARNING]
    assert any(m.upper().startswith("PROFIBUS_WRITES_ENABLED IN ") and "is ignored" in m for m in warnings), warnings


@pytest.mark.parametrize("line", GATE_FORMS)
def test_gate_filter_holds_on_a_windows_like_environment(project, monkeypatch, line):
    environ = UpperCaseEnviron()
    monkeypatch.setattr(cli.os, "environ", environ)
    (project / ".env").write_text(f"{line}\nprofibus_backend=mock\n", encoding="utf-8")
    _, ignored = cli.load_env_file(project)
    assert "PROFIBUS_WRITES_ENABLED" not in environ
    assert environ.get("PROFIBUS_BACKEND") == "mock"
    assert len(ignored) == 1


def test_lower_case_own_settings_are_normalised(project):
    (project / ".env").write_text("profibus_backend=mock\nslave_map_file=/tmp/map.json\n", encoding="utf-8")
    cli.load_env_file(project)
    assert os.environ["PROFIBUS_BACKEND"] == "mock"
    assert os.environ["SLAVE_MAP_FILE"] == "/tmp/map.json"


def test_implicit_env_file_sets_only_this_servers_own_settings(project, captured, caplog):
    lines = [f"{name}=/tmp/injected" for name in FOREIGN] + ["FOO_UNRELATED=1", "export OTHER_WRITES_ENABLED=true"]
    (project / ".env").write_text("\n".join(["PROFIBUS_BACKEND=mock", *lines]) + "\n", encoding="utf-8")
    before = {name: os.environ.get(name) for name in [*FOREIGN, "FOO_UNRELATED", "OTHER_WRITES_ENABLED"]}
    with caplog.at_level(logging.WARNING, logger="profibus_mcp"):
        cli.main([])
    assert captured[0].backend == "mock"
    for name, value in before.items():
        assert os.environ.get(name) == value, name
    warnings = " ".join(record.getMessage() for record in caplog.records if record.levelno == logging.WARNING)
    for name in [*FOREIGN, "FOO_UNRELATED", "OTHER_WRITES_ENABLED"]:
        assert f"{name} in " in warnings, name


def test_ignored_variable_warning_goes_to_stderr(project, captured, capsys):
    (project / ".env").write_text("profibus_writes_enabled=yes\nLD_PRELOAD=/tmp/x.so\n", encoding="utf-8")
    root = logging.getLogger()
    saved = root.handlers[:]
    root.handlers = []  # let main() install its stderr handler
    try:
        cli.main([])
    finally:
        root.handlers = saved
    out, err = capsys.readouterr()
    assert out == ""
    assert "profibus_writes_enabled in" in err and "LD_PRELOAD in" in err and "is ignored" in err


def test_env_file_reaches_every_connection_setting(project, captured):
    (project / ".env").write_text(
        "PROFIBUS_BACKEND=mock\nPROFIBUS_BAUDRATE=187500\nPROFIBUS_MASTER_ADDRESS=3\nPROFIBUS_TIMEOUT=50\n"
        "PROFIBUS_MOCK_HOST=localhost\nPROFIBUS_MAX_RETRIES=1\n",
        encoding="utf-8",
    )
    cli.main([])
    config = captured[0]
    assert (config.backend, config.baudrate, config.master_address, config.timeout_ms) == ("mock", 187500, 3, 50)
    assert (config.mock_host, config.max_retries) == ("localhost", 1)


# --- the real environment and --env-file -----------------------------------


def test_real_environment_can_enable_writes(project, monkeypatch, captured):
    (project / ".env").write_text("PROFIBUS_WRITES_ENABLED=false\n", encoding="utf-8")
    monkeypatch.setenv("PROFIBUS_WRITES_ENABLED", "true")
    cli.main([])
    assert captured[0].writes_enabled is True


def test_explicit_env_file_may_enable_writes(tmp_path, project, captured):
    settings = tmp_path / "bench.env"
    settings.write_text("PROFIBUS_BACKEND=mock\nPROFIBUS_WRITES_ENABLED=true\n", encoding="utf-8")
    # The project .env is not read when --env-file is given.
    (project / ".env").write_text("PROFIBUS_MOCK_PORT=7555\n", encoding="utf-8")
    cli.main(["--env-file", str(settings)])
    config = captured[0]
    assert config.writes_enabled is True and config.backend == "mock"
    assert config.mock_port == 7100


def test_missing_explicit_env_file_is_a_configuration_error(tmp_path, no_project, capsys):
    with pytest.raises(SystemExit) as info:
        cli.main(["--env-file", str(tmp_path / "missing.env")])
    assert info.value.code == 2
    assert "--env-file" in capsys.readouterr().err


# --- startup errors and helpers ---------------------------------------------


def test_configuration_error_exits_cleanly(no_project, monkeypatch, capsys):
    monkeypatch.setenv("PROFIBUS_BAUDRATE", "fast")
    with pytest.raises(SystemExit) as info:
        cli.main([])
    assert info.value.code == 2
    out, err = capsys.readouterr()
    assert out == ""  # stdout belongs to MCP
    assert "configuration error" in err and "PROFIBUS_BAUDRATE" in err
    assert "Traceback" not in err


def test_unknown_port_url_scheme_is_a_configuration_error(no_project, monkeypatch, capsys):
    monkeypatch.setenv("PROFIBUS_PORT", "hwgrep://.*")
    with pytest.raises(SystemExit) as info:
        cli.main([])
    assert info.value.code == 2
    assert "PROFIBUS_PORT" in capsys.readouterr().err


def test_unencodable_mock_host_is_a_configuration_error(no_project, monkeypatch, capsys):
    monkeypatch.setenv("PROFIBUS_MOCK_HOST", "münchen..example")
    with pytest.raises(SystemExit) as info:
        cli.main([])
    assert info.value.code == 2
    assert "PROFIBUS_MOCK_HOST" in capsys.readouterr().err


def test_key_classification():
    for gate in ("PROFIBUS_WRITES_ENABLED", "profibus_writes_enabled", " Profibus_Config_Cmds_Enabled "):
        assert cli.is_safety_gate(gate) and not cli.is_own_setting(gate)
    for own in ("PROFIBUS_BACKEND", "profibus_mock_port", "PROFIBUS_GSD_PATH", "SLAVE_MAP_FILE", "slave_map_file"):
        assert cli.is_own_setting(own) and not cli.is_safety_gate(own)
    for foreign in ("LD_PRELOAD", "PYTHONPATH", "OTHER_WRITES_ENABLED", "PROFIBUSX_BACKEND", "PATH"):
        assert not cli.is_own_setting(foreign)


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as info:
        cli.main(["--version"])
    assert info.value.code == 0
    assert capsys.readouterr().out.strip() == f"profibus-mcp {version('industriagents-profibus-mcp')}"


def test_env_example_documents_every_setting():
    example = Path(__file__).resolve().parents[1] / ".env.example"
    text = example.read_text(encoding="utf-8")
    for name in ("PROFIBUS_BACKEND", "PROFIBUS_MOCK_HOST", "PROFIBUS_MOCK_PORT", "PROFIBUS_WRITES_ENABLED"):
        assert name in text
    # The gate appears only commented out, with the reason.
    assert "\nPROFIBUS_WRITES_ENABLED" not in text and "--env-file" in text


@pytest.mark.parametrize("policy_content", ['{"read_only": "no"}', None])
def test_invalid_policy_file_is_a_clean_configuration_error(tmp_path, policy_content):
    import os
    import subprocess
    import sys

    policy = tmp_path / "policy.json"
    if policy_content is not None:
        policy.write_text(policy_content)
    env = {k: v for k, v in os.environ.items() if not k.startswith("PROFIBUS_")}
    env.update({"PROFIBUS_BACKEND": "mock", "PROFIBUS_POLICY_FILE": str(policy)})
    result = subprocess.run(
        [sys.executable, "-m", "profibus_mcp.cli"],
        cwd=tmp_path,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2
    assert result.stdout == ""
    assert "configuration error" in result.stderr
    assert "Traceback" not in result.stderr
