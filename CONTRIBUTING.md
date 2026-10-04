# Contributing to PROFIBUS MCP

Thanks for taking an interest. This repository is an MCP server for PROFIBUS DP
(PA is a goal) plus a mock slave for local testing. The server works end to end
against the mock; a real DP master backend is not implemented yet (see the
README's [Status](README.md#status)).

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
| `profibus-python/` | The MCP server: `pyproject.toml` + `src/profibus_mcp/`, console script `profibus-mcp`, tests in `tests/` |
| `profibus-mock-slave/` | Mock PROFIBUS DP slaves speaking a TCP JSON-lines protocol (`127.0.0.1:7100`), their GSD files in `gsd/`, tests in `tests/` |
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
   `PROFIBUS_WRITES_ENABLED`, which defaults to `false`.
3. **A mock for every protocol.** If you add a tool, the mock has to be able to
   answer it, or nobody can test it without a plant.
4. **No invented data.** A backend that cannot do something returns
   `success: false` with a clear error; it never reports success for something
   that did not happen. `meta.backend` says which backend answered.

## Getting set up

Prerequisites: Python 3.11+ and [`uv`](https://docs.astral.sh/uv/). No
PROFIBUS hardware is needed for development.

```bash
git clone https://github.com/IndustriAgents/PROFIBUS-MCP.git
cd PROFIBUS-MCP
```

Start the mock slave and leave it running in this terminal:

```bash
cd profibus-mock-slave
uv sync
uv run profibus-mock-slave            # --port / MOCK_PROFIBUS_PORT to move it off 7100
```

In a second terminal, change to the repository root (the `PROFIBUS-MCP`
directory you cloned), then start the server against the mock:

```bash
cd profibus-python
uv sync
PROFIBUS_BACKEND=mock uv run profibus-mcp
```

The server speaks MCP over stdio, so it waits for a client; the Inspector
command below gives you one.

## Testing a change

CI runs the checks below. Run them locally before pushing, starting from the
repository root:

```bash
cd profibus-python
uv sync --locked
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync pytest -v
cd ../profibus-mock-slave
uv sync --locked
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync pytest -v
uv run --no-sync profibus-mock-slave --help
cd ..
```

The server's suite includes `tests/test_integration_stdio.py`, which starts the
mock slave itself (with `uv run`, on a free port) and drives the real server
over stdio with the MCP client SDK: every tool, the write gate, stdout carrying
only JSON-RPC, and a clean exit on a bad setting. If you add or rename a tool,
update the expected tool set there and in `tests/test_tools.py`. `ruff format .`
fixes formatting.

Then drive the server the way a user would, through the
[MCP Inspector](https://modelcontextprotocol.io/docs/tools/inspector) or a
real client. With the mock running, from the repository root:

```bash
cd profibus-python
npx @modelcontextprotocol/inspector -e PROFIBUS_BACKEND=mock uv run profibus-mcp
```

Call the tool you changed, and check the envelope, not just the value. Add
`-e PROFIBUS_WRITES_ENABLED=true` to try the write tools.

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
  kills the server. The model needs to be told what went wrong. That includes
  bad arguments.
- Read configuration in `ProfibusConfig.from_env()`, never at import time, so
  `.env` reaches every setting.
- Every tool has a docstring: it is the description the model sees.
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
