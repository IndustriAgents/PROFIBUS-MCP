# profibus-python

Python MCP server scaffold for PROFIBUS DP. Built with `uv`, `pyserial`, and `mcp[cli]`, it exposes the `profibus-mcp` CLI with tools for AI agents and MCP-compatible clients to scan the bus, exchange data, and query diagnostics. PROFIBUS PA is a goal, not supported yet.

The serial master is still a scaffold: tools return placeholder data and no DP telegrams are exchanged yet. See [Status](../README.md#status) in the repository README.

## Requirements

- Python 3.11+
- [uv](https://github.com/astral-sh/uv)
- Serial/USB PROFIBUS adapter accessible from the host (permissions for `/dev/tty*`); not needed in mock mode

## Quick Start

From the repository root:

```bash
cd profibus-python
uv sync
PROFIBUS_PORT=/dev/ttyUSB0 PROFIBUS_BAUDRATE=500000 uv run profibus-mcp
```

For development without a real serial adapter, enable mock mode. This skips opening the serial port and uses the in-process mock data paths:

```bash
uv sync
PROFIBUS_MOCK=true uv run profibus-mcp
```

## Configuration

Set these as environment variables, for example in your MCP client's `env` block:

| Variable | Default | Meaning |
|----------|---------|---------|
| `PROFIBUS_PORT` | `/dev/ttyUSB0` | Serial port of the PROFIBUS adapter. `mock` or `none` also enables mock mode |
| `PROFIBUS_BAUDRATE` | `500000` | Bus baud rate |
| `PROFIBUS_MASTER_ADDRESS` | `2` | Address of this master on the bus |
| `PROFIBUS_TIMEOUT` | `1000` | Serial read timeout in milliseconds |
| `PROFIBUS_MOCK` | `false` | `true` skips the serial port and serves in-process mock data |
| `PROFIBUS_WRITES_ENABLED` | `true` | `false` makes `write_outputs` and `write_slave_by_alias` refuse every call |
| `PROFIBUS_CONFIG_CMDS_ENABLED` | `false` | Reported by `ping` and `get_master_status`; no tool uses it yet |
| `SLAVE_MAP_FILE` | unset | JSON file mapping aliases to slaves, e.g. `{"drive1": {"address": 5, "description": "Conveyor drive"}}` |
| `PROFIBUS_GSD_PATH` | unset | Directory `load_gsd_file` also searches, by file name |
| `PROFIBUS_MAX_RETRIES`, `PROFIBUS_RETRY_BACKOFF_BASE` | `3`, `0.1` | Parsed but not used yet |

The server also loads a `.env` file at startup. python-dotenv finds it by walking up from the package's source directory, not the working directory, so in a checkout that is `profibus-python/.env` (or the nearest one above it). At the moment that file only reaches `SLAVE_MAP_FILE`, `PROFIBUS_GSD_PATH`, `PROFIBUS_WRITES_ENABLED` and `PROFIBUS_CONFIG_CMDS_ENABLED` as the tools see them. The serial master's settings (`PROFIBUS_PORT`, `PROFIBUS_BAUDRATE`, `PROFIBUS_MASTER_ADDRESS`, `PROFIBUS_TIMEOUT`, `PROFIBUS_MOCK`, ...) are read before `.env` is loaded, so set those in the real environment.

## Layout

```
profibus-python/
├── README.md
├── pyproject.toml
├── uv.lock
├── .python-version
└── src/profibus_mcp
    ├── __init__.py
    ├── cli.py
    ├── server.py
    ├── tools.py
    ├── pb_master.py
    └── gsd_parser.py
```

This scaffold wires a serial master wrapper (`pb_master.py`), XML device-description parser (scaffold), slave map loader, and MCP tools for bus scanning, I/O reads/writes, alias helpers, and health reporting. Flesh out the protocol-specific operations next; [CONTRIBUTING.md](../CONTRIBUTING.md) describes how to work on it.
