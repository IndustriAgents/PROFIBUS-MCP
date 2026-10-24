"""CLI entry point for the PROFIBUS MCP server.

stdout carries the MCP JSON-RPC stream, so everything else goes to stderr.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from dotenv import dotenv_values

from . import __version__
from .config import ConfigError, ProfibusConfig
from .server import ProfibusMCPServer

log = logging.getLogger("profibus_mcp")


#: Variables outside the PROFIBUS_ prefix that the implicit .env may set.
EXTRA_SETTINGS = frozenset({"SLAVE_MAP_FILE"})

PACKAGE_NAME = "industriagents-profibus-mcp"


def normalise_key(name: str) -> str:
    """Upper-case and strip a variable name, as Windows does for the environment."""
    return name.strip().upper()


def is_safety_gate(name: str) -> bool:
    """True for variables that switch on device-changing behaviour (PROFIBUS_*_ENABLED), in any case."""
    key = normalise_key(name)
    return key.startswith("PROFIBUS_") and key.endswith("_ENABLED")


def is_own_setting(name: str) -> bool:
    """True for this server's own non-gate settings: PROFIBUS_* and SLAVE_MAP_FILE."""
    key = normalise_key(name)
    return (key.startswith("PROFIBUS_") or key in EXTRA_SETTINGS) and not is_safety_gate(key)


def find_project_dir(package_file: Path | None = None) -> Path | None:
    """The profibus-python/ directory of a source checkout, found from the package source.

    Returns None when the package is installed (site-packages has no
    pyproject.toml for us next to src/profibus_mcp), so an installed server
    never reads an implicit .env.
    """
    package_dir = (package_file or Path(__file__)).resolve().parent
    if package_dir.name != "profibus_mcp" or package_dir.parent.name != "src":
        return None
    project = package_dir.parent.parent
    pyproject = project / "pyproject.toml"
    try:
        text = pyproject.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    if f'name = "{PACKAGE_NAME}"' not in text:
        return None
    return project


def load_env_file(project_dir: Path | None) -> tuple[Path | None, list[str]]:
    """Load ``.env`` from the server's own project directory, keeping only its own settings.

    Only ``<project_dir>/.env`` is read: never the working directory or a
    parent, because a server launched by an MCP client may run in an untrusted
    project, and settings such as the mock endpoint or the slave map decide
    where a write goes. From that file, only this server's own non-gate
    settings are applied (PROFIBUS_* and SLAVE_MAP_FILE, names compared
    upper-cased). Safety gates (PROFIBUS_*_ENABLED in any case) and every
    other variable (PATH, LD_PRELOAD, PYTHON*, ...) are ignored. Variables
    already set in the environment win. Returns the file loaded (or None) and
    the names it ignored.
    """
    if project_dir is None:
        return None, []
    path = project_dir / ".env"
    if not path.is_file():
        return None, []
    ignored: list[str] = []
    for name, value in dotenv_values(path).items():
        if value is None:
            continue
        if not is_own_setting(name):
            ignored.append(name)
            continue
        os.environ.setdefault(normalise_key(name), value)
    return path, ignored


def load_explicit_env_file(path: Path) -> Path:
    """Load a file named with ``--env-file``; it may set every variable, gates included."""
    if not path.is_file():
        raise ConfigError(f"--env-file {path} does not exist or is not a file")
    for name, value in dotenv_values(path).items():
        if value is not None:
            os.environ.setdefault(name, value)
    return path


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="profibus-mcp",
        description="MCP server for PROFIBUS DP over stdio. Settings come from environment variables.",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        help="load settings from this file instead of profibus-python/.env; unlike that file it may enable writes",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("mcp").setLevel(logging.WARNING)

    ignored: list[str] = []
    try:
        if args.env_file is not None:
            env_file: Path | None = load_explicit_env_file(args.env_file)
        else:
            env_file, ignored = load_env_file(find_project_dir())
        config = ProfibusConfig.from_env()
        server = ProfibusMCPServer(config)
    except (ValueError, OSError) as exc:
        print(f"profibus-mcp: configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None

    if env_file:
        log.info("loaded %s", env_file)
    for name in ignored:
        if is_safety_gate(name):
            reason = "safety switches are only read from the process environment or a file passed with --env-file"
        else:
            reason = "this file may only set PROFIBUS_* settings and SLAVE_MAP_FILE"
        log.warning("%s in %s is ignored: %s", name, env_file, reason)
    for warning in config.warnings:
        log.warning(warning)
    if config.backend == "mock":
        target = f"mock slave at {config.mock_host}:{config.mock_port}"
    else:
        target = f"serial port {config.port} (bus operations not implemented; the port is not opened)"
    log.info(
        "PROFIBUS MCP server: backend=%s, %s, writes %s",
        config.backend,
        target,
        "ENABLED" if config.writes_enabled else "disabled",
    )
    try:
        server.run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
