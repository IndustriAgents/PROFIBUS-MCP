"""Configuration is read at call time, validated, and read-only by default."""

from __future__ import annotations

from pathlib import Path

import pytest

from profibus_mcp.config import ConfigError, ProfibusConfig


def test_defaults_are_read_only_serial():
    config = ProfibusConfig.from_env({})
    assert config.writes_enabled is False
    assert config.backend == "serial"
    assert config.port == "/dev/ttyUSB0"
    assert (config.baudrate, config.master_address, config.timeout_ms) == (500000, 2, 1000)
    assert (config.max_retries, config.retry_backoff_base) == (3, 0.1)
    assert (config.mock_host, config.mock_port) == ("127.0.0.1", 7100)
    assert config.warnings == ()


def test_environment_is_read_when_called_not_at_import(monkeypatch):
    # The scaffold captured os.getenv in dataclass defaults at import time, so
    # a .env loaded later never reached the master.
    monkeypatch.setenv("PROFIBUS_BACKEND", "mock")
    monkeypatch.setenv("PROFIBUS_WRITES_ENABLED", "true")
    monkeypatch.setenv("PROFIBUS_MOCK_PORT", "7200")
    monkeypatch.setenv("PROFIBUS_BAUDRATE", "1500000")
    config = ProfibusConfig.from_env()
    assert (config.backend, config.writes_enabled, config.mock_port, config.baudrate) == ("mock", True, 7200, 1500000)


def test_all_variables(tmp_path):
    env = {
        "PROFIBUS_BACKEND": "Mock",
        "PROFIBUS_PORT": "rfc2217://gateway:4000",
        "PROFIBUS_BAUDRATE": "12000000",
        "PROFIBUS_MASTER_ADDRESS": "1",
        "PROFIBUS_TIMEOUT": "250",
        "PROFIBUS_MAX_RETRIES": "0",
        "PROFIBUS_RETRY_BACKOFF_BASE": "0.5",
        "PROFIBUS_WRITES_ENABLED": "yes",
        "PROFIBUS_MOCK_HOST": "localhost",
        "PROFIBUS_MOCK_PORT": "7101",
        "SLAVE_MAP_FILE": str(tmp_path / "map.json"),
        "PROFIBUS_GSD_PATH": str(tmp_path),
    }
    config = ProfibusConfig.from_env(env)
    assert config.backend == "mock"
    assert config.port == "rfc2217://gateway:4000"
    assert config.baudrate == 12000000 and config.master_address == 1
    assert config.timeout_ms == 250 and config.timeout_s == 0.25
    assert config.max_retries == 0 and config.retry_backoff_base == 0.5
    assert config.writes_enabled is True
    assert (config.mock_host, config.mock_port) == ("localhost", 7101)
    assert config.slave_map_path == tmp_path / "map.json"
    assert config.gsd_base_path == Path(tmp_path)


def test_empty_values_mean_default():
    config = ProfibusConfig.from_env({"PROFIBUS_WRITES_ENABLED": "", "PROFIBUS_BAUDRATE": " ", "PROFIBUS_PORT": ""})
    assert config.writes_enabled is False and config.baudrate == 500000 and config.port == "/dev/ttyUSB0"


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"PROFIBUS_BACKEND": "tcp"}, "PROFIBUS_BACKEND"),
        ({"PROFIBUS_BAUDRATE": "abc"}, "PROFIBUS_BAUDRATE must be an integer"),
        ({"PROFIBUS_BAUDRATE": "115200"}, "PROFIBUS DP rate"),
        ({"PROFIBUS_MASTER_ADDRESS": "126"}, "PROFIBUS_MASTER_ADDRESS"),
        ({"PROFIBUS_TIMEOUT": "0"}, "PROFIBUS_TIMEOUT"),
        ({"PROFIBUS_MAX_RETRIES": "-1"}, "PROFIBUS_MAX_RETRIES"),
        ({"PROFIBUS_RETRY_BACKOFF_BASE": "fast"}, "PROFIBUS_RETRY_BACKOFF_BASE"),
        ({"PROFIBUS_MOCK_PORT": "70000"}, "PROFIBUS_MOCK_PORT"),
        ({"PROFIBUS_MOCK_HOST": "münchen..example"}, "PROFIBUS_MOCK_HOST"),  # empty label: no IDNA form
        ({"PROFIBUS_MOCK_HOST": "ü" * 70}, "PROFIBUS_MOCK_HOST"),  # label too long
        # A typo must not silently turn into "false" (or "true").
        ({"PROFIBUS_WRITES_ENABLED": "enable"}, "PROFIBUS_WRITES_ENABLED must be true or false"),
        ({"PROFIBUS_MOCK": "true", "PROFIBUS_BACKEND": "serial"}, "conflicts|asks for the mock backend"),
    ],
)
def test_invalid_settings_raise_config_error(env, message):
    with pytest.raises(ConfigError, match=message):
        ProfibusConfig.from_env(env)


@pytest.mark.parametrize("env", [{"PROFIBUS_MOCK": "true"}, {"PROFIBUS_PORT": "mock"}, {"PROFIBUS_PORT": "none"}])
def test_legacy_mock_switches_select_the_mock_backend_with_a_warning(env):
    config = ProfibusConfig.from_env(env)
    assert config.backend == "mock"
    assert config.port == "/dev/ttyUSB0"
    assert any("deprecated" in warning and "PROFIBUS_BACKEND=mock" in warning for warning in config.warnings)


def test_config_cmds_flag_is_reported_as_ignored():
    config = ProfibusConfig.from_env({"PROFIBUS_CONFIG_CMDS_ENABLED": "true"})
    assert any("PROFIBUS_CONFIG_CMDS_ENABLED is ignored" in warning for warning in config.warnings)
    assert not hasattr(config, "config_cmds_enabled")


def test_valid_host_names_are_accepted():
    for host in ("127.0.0.1", "localhost", "::1", "münchen.example", "mock.local"):
        assert ProfibusConfig(mock_host=host).mock_host == host
