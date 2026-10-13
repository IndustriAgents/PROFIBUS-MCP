# profibus-python

Python MCP server for PROFIBUS DP. Built with `uv`, `mcp[cli]` (1.17 or later,
below 2) and `anyio`, it
exposes the `profibus-mcp` CLI with tools for AI agents and MCP-compatible
clients to scan the bus, exchange cyclic data, read diagnostics and parse GSD
files. PROFIBUS PA is a goal, not supported yet.

Two backends exist. `mock` talks to [`profibus-mock-slave`](../profibus-mock-slave)
over TCP and works for every tool. `serial` (the default) stands for a DP
master on an RS-485 adapter and is **not implemented**: bus tools return
`success: false` and the port is never opened. See
[Status](../README.md#status) in the repository README.

## Requirements

- Python 3.11+
- [uv](https://github.com/astral-sh/uv)
- For the `mock` backend: `profibus-mock-slave` running (see its
  [README](../profibus-mock-slave/README.md))

## Quick start

From the repository root, with the mock slave already running on
`127.0.0.1:7100`:

```bash
cd profibus-python
uv sync
PROFIBUS_BACKEND=mock uv run profibus-mcp
```

The server speaks MCP over stdio, so it waits for a client on stdin; stop it
with Ctrl-C. The repository [README](../README.md#quick-start) shows how to
start the mock and run the server under the MCP Inspector.

## Configuration

Settings are environment variables, for example in your MCP client's `env`
block. They are read once at startup, after the `.env` file (see below) is loaded; an unusable value
(say `PROFIBUS_BAUDRATE=fast`) stops the server with a one-line error on stderr
and exit code 2.

| Variable | Default | Meaning |
|----------|---------|---------|
| `PROFIBUS_BACKEND` | `serial` | `mock`: talk to `profibus-mock-slave`. `serial`: DP master on an RS-485 adapter (not implemented; bus tools return `success: false`) |
| `PROFIBUS_WRITES_ENABLED` | `false` | `true` lets `write_outputs` and `write_slave_by_alias` change outputs; otherwise they refuse every call. Read only from the process environment or `--env-file`, never from the implicit `.env` (see below) |
| `PROFIBUS_MOCK_HOST` | `127.0.0.1` | Host of the mock slave (`mock` backend) |
| `PROFIBUS_MOCK_PORT` | `7100` | TCP port of the mock slave (`mock` backend) |
| `PROFIBUS_TIMEOUT` | `1000` | Timeout per attempt in milliseconds (connect, send and wait for the reply). Each request also has an overall deadline: timeout × (retries + 1) + backoff delays + 1 s, reported as `deadline_s` by `get_master_status` |
| `PROFIBUS_MAX_RETRIES` | `3` | Retries after a transport failure (0-10). Reads are retried on any transport failure; writes only when the connection could not be made, so a write is never sent twice |
| `PROFIBUS_RETRY_BACKOFF_BASE` | `0.1` | Seconds before the first retry; doubles on each further retry |
| `PROFIBUS_PORT` | `/dev/ttyUSB0` | Serial port of the adapter (`serial` backend). A device path or a `socket://`, `rfc2217://` or `loop://` URL, checked at startup; other pyserial URL schemes (`hwgrep://`, `spy://`, ...) are refused. The port is never opened |
| `PROFIBUS_BAUDRATE` | `500000` | Bus rate in bit/s; must be a DP rate (9600 to 12000000) |
| `PROFIBUS_MASTER_ADDRESS` | `2` | Station address of this master (0-125) |
| `SLAVE_MAP_FILE` | unset | JSON file mapping aliases to slaves, e.g. `{"drive1": {"address": 5, "description": "Conveyor drive"}}` |
| `PROFIBUS_GSD_PATH` | unset | When set, `load_gsd_file` only opens files inside this directory (relative paths are taken relative to it; symlinks are followed before the check). When unset, it opens any readable path |

Booleans accept `true/false`, `yes/no`, `on/off` and `1/0`; anything else is a
startup error rather than a silent `false`.

Changed from the scaffold:

- `PROFIBUS_MOCK=true` and `PROFIBUS_PORT=mock` used to serve invented data
  in-process. They are deprecated aliases for `PROFIBUS_BACKEND=mock` now (a
  warning is logged), so the mock slave has to be running.
- `PROFIBUS_CONFIG_CMDS_ENABLED` is gone; no tool used it. Setting it logs a
  warning.
- `.env` is no longer read from the working directory; only
  `profibus-python/.env`, and only for this server's own settings. See below.
- With `PROFIBUS_GSD_PATH` set, `load_gsd_file` no longer opens paths outside
  it.

### `.env` and `--env-file`

The server reads one `.env` by itself: **`profibus-python/.env` of a source
checkout**, found relative to the package source. It never reads a `.env` from
the working directory or any parent directory, and an installed package (in
site-packages) reads none at all. A server started by an MCP client may run in
the client's project directory, which may not be trusted, and settings such as
the mock endpoint or the slave map decide where a write goes.
[`.env.example`](.env.example) lists every setting.

From that file, only this server's own settings are applied: `PROFIBUS_*`
variables and `SLAVE_MAP_FILE`. Names are compared upper-cased (as Windows
does), so `profibus_backend=mock` and `export PROFIBUS_BACKEND=mock` work.
Everything else is ignored with a warning on stderr naming the variable:

- safety switches such as `PROFIBUS_WRITES_ENABLED`, in any spelling
  (`profibus_writes_enabled`, `export Profibus_Writes_Enabled=...`);
- variables of other programs (`PATH`, `LD_PRELOAD`, `PYTHONPATH`, ...).

Variables already set in the environment win over the file. Enable writes in
the process environment (the MCP client's `env` block), or in a file you name
explicitly:

```bash
uv run profibus-mcp --env-file /path/to/bench.env
```

`--env-file` replaces `profibus-python/.env` (only the named file is read) and
may set every variable. `profibus-mcp --version` prints the version.

## Responses

Every tool returns `{ "success": bool, "data": ..., "error": str | null,
"meta": {...} }`. Bus tools put the backend in `meta.backend` (`mock` or
`serial`), plus `operation`, `duration_ms` and, for the mock, `endpoint` and
`attempts`. Invalid arguments come back as `success: false` with an
`Invalid arguments: ...` error and `meta.backend`. Addresses, lengths and byte
values are strict integers: `"5"`, `5.0` and `true` are refused.

Write tools also report `meta.outcome` and `meta.request_sent`:

| `outcome` | Meaning |
|-----------|---------|
| `written` | The slave acknowledged every byte |
| `rejected` | The slave refused the write before changing anything (the mock says so with `"applied": false`; for example too many bytes) |
| `not_sent` | Nothing left the server: writes disabled, invalid arguments, unknown alias, unreachable mock, or the serial backend |
| `unknown` | The request was sent but no valid reply confirmed it (timeout, dropped connection, garbled reply, an error without a refusal, the overall deadline). It may have been applied; check the slave's outputs before retrying |

A write is never sent twice: only a connection that could not be made is
retried. When the MCP client disconnects (its stdin closes), pending requests
are cancelled: one that has not been sent yet ends as `not_sent` and is never
sent, and the server exits with status 0.

## Layout

```
profibus-python/
├── README.md
├── pyproject.toml
├── uv.lock
├── .python-version
├── .env.example        # every setting, with the .env safety rule
├── src/profibus_mcp
│   ├── __init__.py
│   ├── cli.py          # entry point: .env, config, logging to stderr
│   ├── config.py       # all settings, read at startup
│   ├── server.py       # FastMCP wiring, argument errors as envelopes
│   ├── tools.py        # the 11 tools and the slave-alias map
│   ├── pb_master.py    # master facade, mock and serial backends
│   └── gsd_parser.py   # GSD keyword-file parser
└── tests/              # pytest: unit tests and stdio tests against the mock
```

[CONTRIBUTING.md](../CONTRIBUTING.md) describes how to run the checks and work
on the code.
