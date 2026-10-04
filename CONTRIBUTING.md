# Contributing to PROFIBUS MCP

Thanks for taking an interest. This repository is an MCP server scaffold for
PROFIBUS DP (PA is a goal) plus a mock slave for local testing.

It is also one of the ten servers in
[IndustriConnect](https://github.com/IndustriAgents/IndustriConnect), which
includes this repository as its `PROFIBUS-Project` submodule. **Changes land
here, not in IndustriConnect**: open issues and pull requests against
[IndustriAgents/PROFIBUS-MCP](https://github.com/IndustriAgents/PROFIBUS-MCP).
When `main` moves, IndustriConnect bumps its submodule pointer to pick the
change up.

## Repository layout

| Path | What it is |
|------|------------|
| `profibus-python/` | The MCP server: `pyproject.toml` + `src/profibus_mcp/`, console script `profibus-mcp` |
| `profibus-mock-slave/` | Mock PROFIBUS DP slave speaking a TCP JSON-lines protocol (`127.0.0.1:7100`) |
| `.github/workflows/ci.yml` | The checks every pull request has to pass |

Each directory is its own [`uv`](https://docs.astral.sh/uv/) project with a
committed `uv.lock`, so work inside the one you are changing.

## What holds the suite together

IndustriConnect's value is that all ten servers behave the same way. A change
that breaks one of these needs a reason in the pull request:

1. **One response envelope.** Every tool returns `{ success, data, error, meta }`.
   A client that can read one server's output can read all ten.
2. **Read-only by default.** Tools that write outputs change physical
   equipment. They are separated from the read tools and gated by
   `PROFIBUS_WRITES_ENABLED`. Today that flag defaults to `true` here; making
   this server read-only by default, like the rest of the suite, is open work.
3. **A mock for every protocol.** If you add a tool, the mock has to be able to
   answer it, or nobody can test it without a plant.

## Getting set up

Prerequisites: Python 3.11+ and [`uv`](https://docs.astral.sh/uv/). No
PROFIBUS hardware is needed for development.

```bash
git clone https://github.com/IndustriAgents/PROFIBUS-MCP.git
cd PROFIBUS-MCP
```

The MCP server, in mock mode (no serial port is opened):

```bash
cd profibus-python
uv sync
PROFIBUS_MOCK=true uv run profibus-mcp
```

The mock slave, in a second terminal:

```bash
cd profibus-mock-slave
uv sync
uv run profibus-mock-slave            # --port / MOCK_PROFIBUS_PORT to move it off 7100
```

The serial master in `profibus-python/src/profibus_mcp/pb_master.py` does not
yet have a client for the mock's TCP protocol, so for now the two are exercised
separately: the server with `PROFIBUS_MOCK=true`, the mock directly over TCP
(see its [README](profibus-mock-slave/README.md)). Wiring them together is a
welcome contribution.

## Testing a change

There is no test suite yet. CI runs the checks below; run them locally before
pushing:

```bash
cd profibus-python
uv sync --locked
uv run --no-sync python -m compileall -q src
uv run --no-sync python -c "import profibus_mcp, profibus_mcp.cli"

cd ../profibus-mock-slave
uv sync --locked
uv run --no-sync python -m compileall -q profibus_mock_slave.py
uv run --no-sync profibus-mock-slave --help
```

CI also opens a real MCP session against the server (initialize, `tools/list`,
`ping`) and drives the mock's JSON protocol in-process; the exact scripts are in
[`.github/workflows/ci.yml`](.github/workflows/ci.yml). If you add or rename a
tool, update the expected tool set there.

Then drive the server the way a user would, through the
[MCP Inspector](https://modelcontextprotocol.io/docs/tools/inspector) or a
real client:

```bash
cd profibus-python
npx @modelcontextprotocol/inspector -e PROFIBUS_MOCK=true uv run profibus-mcp
```

Call the tool you changed, and check the envelope, not just the value.

If you change dependencies, run `uv lock` in that project and commit the
updated `uv.lock`; CI installs with `--locked` and fails if the lockfile is out
of date.

**Do not test against production equipment.** If a change genuinely cannot be
verified against the mock, say so in the pull request and describe the bench
you used.

## House rules for the code

- MCP speaks over **stdio**, so `stdout` belongs to the protocol. All logging
  goes to `stderr`. A stray `print()` corrupts the session.
- Fail at startup, not at the first tool call, when configuration is unusable.
- An error is a returned `{ success: false, error }`, not an exception that
  kills the server. The model needs to be told what went wrong.
- Keep tool names and argument names aligned with the other IndustriConnect
  servers.

## Pull requests

- Branch off `main` and keep commits focused, with descriptive messages.
- Say how you tested, and whether the change touches anything that can write
  to a device.
- Reference related issues (e.g. "Fixes #1").
- If a change was AI-assisted, keep the `Co-Authored-By:` trailer.
- PRs from forks: enable **"Allow edits by maintainers"** so reviewers can
  rebase.

## Code of Conduct

By taking part you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).
