# Security Policy

## What this server is, in security terms

This repository is a bridge between a language model and a PROFIBUS DP
network. The MCP server speaks stdio to its client and is meant to talk to the
bus through a PROFIBUS master interface. That puts the trust boundary *inside*
the server: whatever the model decides to call, the bus is asked to do.

PROFIBUS was designed for a physically isolated fieldbus and has no
authentication or encryption of its own. Anything that can reach the RS-485
segment, or the adapter on the host, can read inputs and write outputs.

So treat this server as equipment on the control network, not as an
application on the office network.

## Running it safely

- **Start with the mock.** Run `profibus-mock-slave` and the server with
  `PROFIBUS_BACKEND=mock`, which never opens a serial port, to exercise the
  tools without touching real plant. Do that first, every time.
- **Writes stay off until you mean otherwise.** `write_outputs` and
  `write_slave_by_alias` change physical outputs. They refuse every call
  unless `PROFIBUS_WRITES_ENABLED=true` is set in the server's environment;
  only set it on a lab bench or when you mean it. There is no undo, and a
  language model will call a tool it has been given.
- **Do not expose the server beyond the host running the client.** It is a
  stdio process meant to run next to the MCP client, not a network service.
- **Limit who can open the adapter.** Access to the serial device
  (`/dev/tty*`, `COM*`) is access to the bus. Grant it to the account running
  the server and nobody else.
- **Never point it at a safety system.** Safety-instrumented functions,
  including PROFIsafe devices, are out of scope for this repository.

The `serial` backend in `profibus-python` is not implemented (see the
[README](README.md#status)): its bus tools return `success: false` and it never
opens the configured port, because an RS-485 adapter opened without a DP stack
in control can switch its line driver on and disturb a running bus. That does
not make the advice above optional: the switches it describes are the ones that
will gate real telegrams once a DP master exists.

The mock slave listens on `127.0.0.1` by default and has no authentication.
Keep it on loopback; `--host 0.0.0.0` exposes it to the network.

## Supported versions

This project is pre-1.0. Security fixes land on `main`.

## Reporting a vulnerability

Please report privately, through
[GitHub private vulnerability reporting](https://github.com/IndustriAgents/PROFIBUS-MCP/security/advisories/new),
or by email to hi@industriagents.com.

Please do not open a public issue for a vulnerability.

Include the version or commit, what an attacker would gain, and a reproduction
if you have one. We will acknowledge within a week and keep you updated as we
work on a fix.

## Scope

In scope: the MCP server (`profibus-python`) and the mock slave
(`profibus-mock-slave`) in this repository. The same code ships inside
[IndustriConnect](https://github.com/IndustriAgents/IndustriConnect) as the
`PROFIBUS-Project` submodule; report it here either way.

Out of scope: vulnerabilities in PROFIBUS itself (the lack of authentication
is a property of the protocol), in third-party libraries such as `pyserial`
or the `mcp` SDK (report those upstream), and in adapter or device firmware.
