# PROFIBUS MCP Server

[![CI](https://github.com/IndustriAgents/PROFIBUS-MCP/actions/workflows/ci.yml/badge.svg)](https://github.com/IndustriAgents/PROFIBUS-MCP/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![MCP](https://img.shields.io/badge/MCP-compatible-purple)](https://modelcontextprotocol.io)

MCP server scaffold for PROFIBUS DP: a Python MCP server plus a mock slave,
aimed at letting AI agents and MCP-compatible clients scan the bus, exchange
cyclic data, and inspect diagnostics. The tool surface, configuration and a TCP
mock slave are in place; the server does not communicate on the bus yet (see
[Status](#status)). PROFIBUS PA is a goal, not something the code supports
today. It follows the same shape as the
other [IndustriConnect](https://github.com/IndustriAgents/IndustriConnect)
servers (Modbus, S7comm, EtherCAT and the rest): one `uv` project for the
server, one for the mock, and the shared `{ success, data, error, meta }`
response envelope.

## Status

**This is a scaffold.** The MCP server, its tool surface, configuration,
slave-alias map and the mock slave are in place, so feature work can focus on
the protocol specifics. PROFIBUS communication itself is not implemented yet:

- The serial master (`profibus-python/src/profibus_mcp/pb_master.py`) opens the
  configured port but does not send or receive DP telegrams. `scan_bus`,
  `read_inputs` and `read_diagnosis` return placeholder data, and
  `write_outputs` reports bytes as written without sending them. Because
  nothing is sent, `read_inputs` and `test_slave_communication` report success
  (with zero bytes) for any address, including slaves that do not exist.
- Writes are enabled unless `PROFIBUS_WRITES_ENABLED=false` is set; see
  [SECURITY.md](SECURITY.md).
- The MCP server does not talk to the mock slave yet. The mock is a standalone
  TCP JSON simulator you can drive directly.
- `load_gsd_file` reads a placeholder XML format (`Manufacturer`, `Type` and
  `IdentNumber` attributes on the root element, `Len` on `Input`/`Output`
  elements). Neither classic text GSD files (`.gsd`) nor GSDML are parsed yet.

## Highlights

- Tool surface for bus scanning, cyclic I/O and diagnostics (`scan_bus`,
  `read_inputs`, `write_outputs`, `read_diagnosis`), returning placeholder
  data until the serial master is implemented
- Alias-based slave map (`SLAVE_MAP_FILE`) and a write gate
  (`PROFIBUS_WRITES_ENABLED`)
- XML device-description parser (scaffold) behind `load_gsd_file`
- Health and master status tools plus a TCP JSON mock slave for local testing

## Layout

```
PROFIBUS-MCP/
├── README.md
├── LICENSE
├── CONTRIBUTING.md · CODE_OF_CONDUCT.md · SECURITY.md
├── .github/                # CI, issue/PR templates, IndustriConnect notification
├── profibus-python/        # Python MCP server (uv, pyserial)
└── profibus-mock-slave/    # Mock PROFIBUS slave (TCP JSON protocol)
```

Inside IndustriConnect this repository is mounted as the `PROFIBUS-Project/`
submodule, so the same tree appears there under that name.

## Install

Requirements: Python 3.11+ and [`uv`](https://docs.astral.sh/uv/). A serial/USB
PROFIBUS adapter is only needed outside mock mode.

```bash
git clone https://github.com/IndustriAgents/PROFIBUS-MCP.git
cd PROFIBUS-MCP/profibus-python
uv sync
```

## Quick start

Run the server without hardware, from `profibus-python/` where Install left
you. Mock mode never opens a serial port:

```bash
PROFIBUS_MOCK=true uv run profibus-mcp
```

The server speaks MCP over stdio: started from a terminal it prints nothing and
waits for a client on stdin (Ctrl-C to stop). To try the tools by hand, run it
under the [MCP Inspector](https://modelcontextprotocol.io/docs/tools/inspector)
instead:

```bash
npx @modelcontextprotocol/inspector -e PROFIBUS_MOCK=true uv run profibus-mcp
```

With an adapter, from the same directory:

```bash
PROFIBUS_PORT=/dev/ttyUSB0 PROFIBUS_BAUDRATE=500000 uv run profibus-mcp
```

If the port cannot be opened the server exits at startup and prints the error
to stderr. All settings are listed under
[Configuration](profibus-python/README.md#configuration).

To use it from an MCP client such as Claude Desktop or Claude Code, point the
client at the project directory:

```json
{
  "mcpServers": {
    "profibus": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/PROFIBUS-MCP/profibus-python", "run", "profibus-mcp"],
      "env": { "PROFIBUS_MOCK": "true" }
    }
  }
}
```

The mock slave runs on its own; see its [README](profibus-mock-slave/README.md).
From `profibus-python/` (from the repository root, use `cd profibus-mock-slave`):

```bash
cd ../profibus-mock-slave
uv sync
uv run profibus-mock-slave          # listens on 127.0.0.1:7100
```

## Tools

| Tool | What it does |
|------|--------------|
| `scan_bus` | Lists the slaves on the bus |
| `read_inputs(slave_address, length)` | Reads a slave's input bytes, returned as hex |
| `write_outputs(slave_address, data)` | Writes output bytes; refused when `PROFIBUS_WRITES_ENABLED=false` |
| `read_diagnosis(slave_address)` | Reads a slave's diagnosis |
| `test_slave_communication(slave_address)` | Reads 4 input bytes as a reachability check |
| `list_slaves` | Lists the aliases defined in `SLAVE_MAP_FILE` |
| `read_slave_by_alias(alias, length=4)` | `read_inputs` for the slave behind an alias |
| `write_slave_by_alias(alias, data)` | `write_outputs` for the slave behind an alias; same write gate |
| `load_gsd_file(filepath)` | Parses a device description file for manufacturer, ident number and I/O lengths |
| `ping` | Connection settings, write/config flags and the number of slave aliases |
| `get_master_status` | Serial port, baud rate, master address and flags |

See [Status](#status) for what these return today.

## Part of IndustriConnect

This server is one of the protocol servers in
[IndustriConnect](https://github.com/IndustriAgents/IndustriConnect), a suite of
MCP servers for industrial automation (Modbus, MQTT/Sparkplug B, OPC UA, BACnet,
DNP3, EtherCAT, EtherNet/IP, PROFIBUS, PROFINET and S7comm). IndustriConnect
includes this repository as its `PROFIBUS-Project` submodule and tracks `main`.

Development happens here: please open issues and pull requests in
[IndustriAgents/PROFIBUS-MCP](https://github.com/IndustriAgents/PROFIBUS-MCP),
not in IndustriConnect.

## Contributing and security

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup and the checks CI runs, and
[SECURITY.md](SECURITY.md) for running the server safely and reporting
vulnerabilities. Everyone taking part agrees to the
[Code of Conduct](CODE_OF_CONDUCT.md).

## License

MIT — see [LICENSE](LICENSE).
