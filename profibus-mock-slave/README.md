# profibus-mock-slave

Simple PROFIBUS DP slave simulator for local testing. Instead of implementing the full fieldbus, it exposes a TCP JSON protocol that mimics bus scans, I/O buffers, and diagnostics so PROFIBUS tooling can be exercised without hardware.

The MCP server in [`../profibus-python`](../profibus-python) does not connect to this mock yet (its serial master is still a scaffold); for now, drive the mock directly over TCP, or run the server with `PROFIBUS_MOCK=true`.

## Usage

From the repository root:

```bash
cd profibus-mock-slave
uv sync
uv run profibus-mock-slave
```

Defaults:

- Listens on `127.0.0.1:7100`
- Simulates two slaves, at addresses 5 (ident `0x809C`) and 8 (ident `0x80A5`), whose 4 input bytes change every update interval
- Accepts one JSON object per line and answers with one JSON line (`{"success": ..., "data": ...}` or `{"success": false, "error": ...}`):
  - `{ "op": "scan" }`
  - `{ "op": "read_inputs", "address": 5, "length": 4 }`
  - `{ "op": "write_outputs", "address": 5, "data": [1,0] }` (the output buffer is 2 bytes)
  - `{ "op": "diagnosis", "address": 5 }`

Flags/ENV let you adjust host, port, update interval, and verbosity:

| Flag | Environment variable | Default |
|------|----------------------|---------|
| `--host` | `MOCK_PROFIBUS_HOST` | `127.0.0.1` |
| `--port` | `MOCK_PROFIBUS_PORT` | `7100` |
| `--update-interval` | `MOCK_PROFIBUS_UPDATE_INTERVAL` | `1.0` (seconds) |
| `--verbose` | `MOCK_PROFIBUS_VERBOSE` | off |

The slave list is fixed in code. Extend `profibus_mock_slave.py` later to integrate with a real PROFIBUS stack, parameter blocks, and richer diagnostics.
