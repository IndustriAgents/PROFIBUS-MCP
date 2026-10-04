# PROFIBUS MCP Server

[![CI](https://github.com/IndustriAgents/PROFIBUS-MCP/actions/workflows/ci.yml/badge.svg)](https://github.com/IndustriAgents/PROFIBUS-MCP/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![MCP](https://img.shields.io/badge/MCP-compatible-purple)](https://modelcontextprotocol.io)

An MCP server for PROFIBUS DP, plus a mock slave to run it against. It lets AI
agents and MCP-compatible clients scan the bus, exchange cyclic data, read
diagnostics and parse GSD files. Today every tool works end to end against the
mock slave in this repository; talking to a real PROFIBUS DP bus is not
implemented yet (see [Status](#status)). PROFIBUS PA is a goal, not something
the code supports. It follows the same shape as the other
[IndustriConnect](https://github.com/IndustriAgents/IndustriConnect) servers
(Modbus, S7comm, EtherCAT and the rest): one `uv` project for the server, one
for the mock, and the shared `{ success, data, error, meta }` response
envelope.

## Status

The server has two backends, chosen with `PROFIBUS_BACKEND`:

| Backend | What it talks to | State |
|---------|------------------|-------|
| `mock` | `profibus-mock-slave` over TCP (JSON lines, `127.0.0.1:7100` by default) | Works for every tool. The data comes from the mock's simulated slaves; no PROFIBUS telegrams are involved. |
| `serial` (default) | A DP master on an RS-485 adapter | **Not implemented.** Bus tools return `success: false` with a "not implemented" error. The port is never opened and nothing is sent. |

Every bus tool reports the configured backend in `meta.backend`, on success and
on failure, including when its arguments are rejected. No tool returns invented
data: a slave that does not answer, a refused write or an unimplemented backend
is always a `success: false` envelope.

Writes are **off by default**. `write_outputs` and `write_slave_by_alias`
refuse every call unless `PROFIBUS_WRITES_ENABLED=true` is set in the process
environment (for example the MCP client's `env` block) or in a file passed with
`--env-file`. The only `.env` the server reads by itself is
`profibus-python/.env` in a source checkout (never the working directory or a
parent; none when the package is installed). That file may set this server's
own connection settings but cannot enable writes: the switch and any other
variable there are ignored with a warning. Every write reports
`meta.outcome`: `written`, `rejected` (the slave refused it before changing
anything), `not_sent`, or `unknown` when the request went out but no valid
reply confirmed it. An `unknown` write may have been applied, and it is never
re-sent automatically. Every request to the mock has an overall deadline, so no
call hangs without an outcome. If the MCP client cancels a call or disconnects,
a write that has not left yet is never sent (checked right before the send);
after a disconnect the server exits. Write tools are annotated as destructive and
not idempotent. See [SECURITY.md](SECURITY.md).

`load_gsd_file` parses real PROFIBUS DP GSD files (the ASCII keyword format:
`#Profibus_DP`, `Keyword = value`, `Module ... EndModule`) and decodes each
module's I/O lengths from its configuration bytes. It needs no backend. With
`PROFIBUS_GSD_PATH` set, it only opens files inside that directory.

### Known issues and limitations

- No real DP master. A credible one needs a DP master stack (token handling,
  parameterisation, Chk_Cfg, cyclic Data_Exchange) on suitable hardware, or a
  gateway. [pyprofibus](https://bues.ch/a/profibus) was evaluated and not
  integrated: it is GPL-2.0-or-later (this project is MIT), describes itself as
  beta, and its API could not be verified without hardware.
- The mock's protocol is a JSON test protocol, not PROFIBUS. It simulates two
  fixed slaves (addresses 5 and 8) and always reports their status as `OK`.
- `read_diagnosis` returns the status and ident number the backend reports; the
  standard 6-byte DP diagnosis is not decoded yet.
- The server has no tool to read back a slave's outputs, so after a write with
  outcome `unknown` the check has to happen elsewhere (for the mock, its
  `read_outputs` request).
- GSD files: keywords inside `PrmText`, `ExtUserPrmData` and other blocks are
  skipped rather than returned. GSDML (XML) files are rejected: they describe
  PROFINET IO devices.
- PROFIBUS PA and DP-V1 acyclic services are not supported.

## Highlights

- Tools for bus scanning, cyclic I/O and diagnostics (`scan_bus`,
  `read_inputs`, `write_outputs`, `read_diagnosis`, `test_slave_communication`)
- Read-only by default; writes need `PROFIBUS_WRITES_ENABLED=true` from the
  environment or `--env-file`, never from an implicit `.env`; an implicit
  `.env` is read only from `profibus-python/` and only for this server's own
  settings
- GSD keyword-file parser behind `load_gsd_file`, with DP configuration-byte
  decoding
- Alias-based slave map (`SLAVE_MAP_FILE`)
- Bad arguments, unreachable slaves and refused writes come back as
  `success: false` envelopes, never as raw exceptions
- A TCP mock slave with GSD files describing its two slaves, and a test suite
  that drives the real server over stdio against it

## Layout

```
PROFIBUS-MCP/
├── README.md
├── LICENSE
├── CONTRIBUTING.md · CODE_OF_CONDUCT.md · SECURITY.md
├── .github/                # CI, issue/PR templates, IndustriConnect notification
├── profibus-python/        # Python MCP server (uv); tests/ holds the pytest suite
└── profibus-mock-slave/    # Mock PROFIBUS DP slaves (TCP JSON lines); gsd/ holds their GSD files
```

Inside IndustriConnect this repository is mounted as the `PROFIBUS-Project/`
submodule, so the same tree appears there under that name.

## Install

Requirements: Python 3.11+ and [`uv`](https://docs.astral.sh/uv/). No PROFIBUS
hardware is needed.

```bash
git clone https://github.com/IndustriAgents/PROFIBUS-MCP.git
cd PROFIBUS-MCP
```

## Quick start

From the repository root, start the mock slave in the background, then the
server under the [MCP Inspector](https://modelcontextprotocol.io/docs/tools/inspector)
so you can call the tools by hand:

```bash
cd profibus-mock-slave
uv sync
uv run profibus-mock-slave &        # simulated slaves 5 and 8 on 127.0.0.1:7100
cd ../profibus-python
uv sync
npx @modelcontextprotocol/inspector -e PROFIBUS_BACKEND=mock uv run profibus-mcp
```

Stop the mock afterwards with `kill %1`. Try `scan_bus`, then `read_inputs`
with `slave_address` 5, and `load_gsd_file` with
`../profibus-mock-slave/gsd/IA_809C.gsd`.

Without the Inspector, the server speaks MCP over stdio: started from a
terminal (`PROFIBUS_BACKEND=mock uv run profibus-mcp` in `profibus-python/`) it
logs one line to stderr and waits for a client on stdin (Ctrl-C to stop).

To use it from an MCP client such as Claude Desktop or Claude Code, point the
client at the project directory, and keep the mock running:

```json
{
  "mcpServers": {
    "profibus": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/PROFIBUS-MCP/profibus-python", "run", "profibus-mcp"],
      "env": { "PROFIBUS_BACKEND": "mock" }
    }
  }
}
```

Without `PROFIBUS_BACKEND=mock` the server uses the `serial` backend: it
starts, but bus tools answer "not implemented". To allow writes, add
`"PROFIBUS_WRITES_ENABLED": "true"` to the same `env` block. All settings are
listed under [Configuration](profibus-python/README.md#configuration).

## Tools

| Tool | What it does |
|------|--------------|
| `scan_bus` | Lists the slaves the backend reports: address, ident number, status, I/O lengths |
| `read_inputs(slave_address, length?)` | Reads a slave's input bytes (all of them if `length` is omitted), returned as hex |
| `write_outputs(slave_address, data)` | Writes output bytes. Changes physical outputs; refused unless `PROFIBUS_WRITES_ENABLED=true` |
| `read_diagnosis(slave_address)` | Reads a slave's status and ident number |
| `test_slave_communication(slave_address)` | Checks that a slave answers, by requesting its diagnosis |
| `list_slaves` | Lists the aliases defined in `SLAVE_MAP_FILE` |
| `read_slave_by_alias(alias, length?)` | `read_inputs` for the slave behind an alias |
| `write_slave_by_alias(alias, data)` | `write_outputs` for the slave behind an alias; same write gate |
| `load_gsd_file(filepath)` | Parses a GSD file: vendor, model, ident number, baud rates, modules and their I/O lengths. Confined to `PROFIBUS_GSD_PATH` when set |
| `ping` | Server version, backend, write gate and alias count (does not contact the bus) |
| `get_master_status` | Backend, port or mock endpoint, master address, timeouts and write gate |

Every tool returns `{ success, data, error, meta }`, and every tool has a
description that MCP clients show to the model. Addresses, lengths and byte
values must be JSON integers: strings, floats and booleans are refused with an
`Invalid arguments` envelope.

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
