# profibus-mock-slave

PROFIBUS DP slave simulator for local testing. Instead of implementing the
fieldbus, it serves a TCP JSON-lines protocol that mimics bus scans, I/O
buffers and diagnostics, so PROFIBUS tooling can be exercised without hardware.

The MCP server in [`../profibus-python`](../profibus-python) talks to it when
started with `PROFIBUS_BACKEND=mock`; every server tool works against it.

## Usage

From the repository root:

```bash
cd profibus-mock-slave
uv sync
uv run profibus-mock-slave
```

It runs until Ctrl-C (or SIGTERM) and logs to stderr.

## Simulated slaves

| Address | Ident number | Input bytes | Output bytes | GSD file |
|---------|--------------|-------------|--------------|----------|
| 5 | `0x809C` | 4 | 2 | [`gsd/IA_809C.gsd`](gsd/IA_809C.gsd) |
| 8 | `0x80A5` | 8 | 4 | [`gsd/IA_80A5.gsd`](gsd/IA_80A5.gsd) |

Input bytes change every update interval; output bytes hold what was last
written. Both slaves always report status `OK`. The GSD files describe the
simulated slaves only. The ident numbers are arbitrary and may coincide with
real devices, so never put the mock or its GSD files on a real bus.

## Protocol

One JSON object per line in, one JSON line out: `{"success": true, "data": ...}`
or `{"success": false, "error": "..."}`. A malformed request (not JSON, not an
object, a non-integer address, an out-of-range byte) gets an error reply and
the connection stays open. A line over 64 KiB gets an error reply and the
connection is closed.

| Request | Reply `data` |
|---------|--------------|
| `{"op": "scan"}` | list of `{address, ident_number, status, input_length, output_length}` |
| `{"op": "read_inputs", "address": 5, "length": 4}` | `{address, length, raw_data_hex}`; `length` is optional (default: all input bytes) and may not exceed the slave's input bytes |
| `{"op": "write_outputs", "address": 5, "data": [1, 0]}` | `{address, written_bytes}`; writes from the first output byte, at most the slave's output bytes, each value 0-255 |
| `{"op": "read_outputs", "address": 5}` | `{address, raw_data_hex}` of the output buffer (for tests) |
| `{"op": "diagnosis", "address": 5}` | `{address, status, ident_number}` |

Addresses are integers 0-125; an address without a slave gets
`"Slave not found at address N"`.

## Options

| Flag | Environment variable | Default |
|------|----------------------|---------|
| `--host` | `MOCK_PROFIBUS_HOST` | `127.0.0.1` |
| `--port` | `MOCK_PROFIBUS_PORT` | `7100` (`0` picks a free port) |
| `--update-interval` | `MOCK_PROFIBUS_UPDATE_INTERVAL` | `1.0` (seconds) |
| `--verbose` | `MOCK_PROFIBUS_VERBOSE` | off (logs every connection and update) |

The slave list is fixed in `default_slaves()` in `profibus_mock_slave.py`.

## Tests

```bash
uv run pytest
```
