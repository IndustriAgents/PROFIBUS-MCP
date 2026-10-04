"""End to end: the real server over stdio (MCP client SDK) against the real mock slave.

The mock is started from ../profibus-mock-slave with ``uv run``, in its own
locked environment, on a free local port, with a stdin pipe it watches: if
the test run is killed, the pipe closes and the mock exits.

Servers are started with an explicit, empty ``--env-file`` so a developer's
own profibus-python/.env never takes part; the .env rules have their own test
on a copy of the source tree.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from importlib.metadata import version
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .conftest import MOCK_DIR, MOCK_GSD_DIR, TESTS_DIR
from .fakes import free_port

TOOLS = {
    "scan_bus",
    "read_inputs",
    "write_outputs",
    "read_diagnosis",
    "load_gsd_file",
    "list_slaves",
    "read_slave_by_alias",
    "write_slave_by_alias",
    "ping",
    "get_master_status",
    "test_slave_communication",
}
SERVER_MODULE = [sys.executable, "-m", "profibus_mcp.cli"]


def server_cmd(directory: Path) -> list[str]:
    """The server command, isolated from any implicit .env by an empty --env-file."""
    isolated = directory / "isolated.env"
    isolated.touch()
    return [*SERVER_MODULE, "--env-file", str(isolated)]


def mock_command(uv: str, port: int) -> list[str]:
    command = [uv, "run", "--locked", "--directory", str(MOCK_DIR), "profibus-mock-slave"]
    return command + ["--host", "127.0.0.1", "--port", str(port), "--update-interval", "0.05", "--exit-on-stdin-eof"]


def _uv() -> str:
    uv = shutil.which("uv")
    if uv is None:
        if os.environ.get("CI"):
            pytest.fail("uv is needed to start profibus-mock-slave")
        pytest.skip("uv is needed to start profibus-mock-slave")
    return uv


def _mock_env() -> dict[str, str]:
    # Our own virtualenv must not leak into the mock's `uv run`.
    return {key: value for key, value in os.environ.items() if key != "VIRTUAL_ENV"}


# ---------------------------------------------------------------------------
# The real mock slave
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def mock_port(tmp_path_factory: pytest.TempPathFactory) -> Iterator[int]:
    uv = _uv()
    port = free_port()
    log_path = tmp_path_factory.mktemp("mock") / "mock.log"
    with log_path.open("wb") as log:
        # The stdin pipe is the mock's lifeline: it closes when this process dies.
        process = subprocess.Popen(
            mock_command(uv, port),
            stdin=subprocess.PIPE,
            stdout=log,
            stderr=subprocess.STDOUT,
            env=_mock_env(),
            start_new_session=True,
        )
    try:
        _wait_for_port(port, process, log_path)
        yield port
    finally:
        _stop(process)


def _wait_for_port(port: int, process: subprocess.Popen, log_path: Path) -> None:
    deadline = time.monotonic() + 180  # the first run may have to install the mock's environment
    while True:
        if process.poll() is not None:
            pytest.fail(f"profibus-mock-slave exited with {process.returncode}:\n{log_path.read_text()}")
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            return
        except OSError:
            if time.monotonic() > deadline:
                pytest.fail(f"profibus-mock-slave did not listen on {port}:\n{log_path.read_text()}")
            time.sleep(0.2)


def _stop(process: subprocess.Popen) -> None:
    if process.stdin:
        process.stdin.close()  # the mock exits on EOF
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)  # uv and the mock it started
        process.wait(timeout=10)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=10)


def mock_request(port: int, request: dict[str, Any]) -> dict[str, Any]:
    """Ask the mock directly, bypassing the MCP server."""
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(json.dumps(request).encode() + b"\n")
        return json.loads(sock.makefile("rb").readline())


# ---------------------------------------------------------------------------
# MCP sessions over stdio
# ---------------------------------------------------------------------------


@asynccontextmanager
async def mcp_session(
    env: dict[str, str], cwd: Path, command: list[str] | None = None, errlog: Any = None
) -> AsyncIterator[tuple[ClientSession, Any]]:
    command = command or server_cmd(cwd)
    params = StdioServerParameters(command=command[0], args=command[1:], env=env, cwd=str(cwd))
    async with stdio_client(params, errlog=errlog or sys.stderr) as (read, write):
        async with ClientSession(read, write) as session:
            with anyio.fail_after(30):
                init = await session.initialize()
            yield session, init


async def call(session: ClientSession, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    with anyio.fail_after(30):
        result = await session.call_tool(name, arguments or {})
    assert not result.isError, result
    envelope = json.loads(result.content[0].text)
    assert set(envelope) == {"success", "data", "error", "meta"}, envelope
    return envelope


def mock_env(port: int, **extra: str) -> dict[str, str]:
    return {"PROFIBUS_BACKEND": "mock", "PROFIBUS_MOCK_PORT": str(port), **extra}


@pytest.fixture
def slave_map(tmp_path: Path) -> Path:
    path = tmp_path / "slaves.json"
    path.write_text(
        json.dumps({"drive1": {"address": 5, "description": "Conveyor drive"}, "valve_island": {"address": 8}}),
        encoding="utf-8",
    )
    return path


@pytest.mark.anyio
async def test_every_tool_end_to_end_with_writes_disabled(mock_port: int, tmp_path: Path, slave_map: Path) -> None:
    env = mock_env(mock_port, SLAVE_MAP_FILE=str(slave_map), PROFIBUS_GSD_PATH=str(MOCK_GSD_DIR))
    outputs_before = mock_request(mock_port, {"op": "read_outputs", "address": 5})["data"]["raw_data_hex"]

    async with mcp_session(env, tmp_path) as (session, init):
        assert init.serverInfo.name == "PROFIBUS MCP Server"
        assert init.serverInfo.version == version("profibus-mcp")
        tools = (await session.list_tools()).tools
        assert {tool.name for tool in tools} == TOOLS
        assert all(tool.description for tool in tools)

        scan = await call(session, "scan_bus")
        assert scan["success"] is True
        assert scan["meta"]["backend"] == "mock" and scan["meta"]["endpoint"] == f"127.0.0.1:{mock_port}"
        slaves = {slave["address"]: slave for slave in scan["data"]["slaves"]}
        assert sorted(slaves) == [5, 8]

        # The mock's GSD files agree with what the mock reports on the bus.
        for address, gsd in [(5, "IA_809C.gsd"), (8, "IA_80A5.gsd")]:
            loaded = await call(session, "load_gsd_file", {"filepath": gsd})
            assert loaded["success"] is True and loaded["meta"]["source"] == "gsd_file"
            assert loaded["data"]["metadata"]["ident_number"] == slaves[address]["ident_number"]
            assert loaded["data"]["io_config"]["input_length"] == slaves[address]["input_length"]
            assert loaded["data"]["io_config"]["output_length"] == slaves[address]["output_length"]

        for address, length in [(5, 4), (8, 8)]:
            read = await call(session, "read_inputs", {"slave_address": address})
            assert read["success"] is True and read["meta"]["backend"] == "mock"
            assert read["data"]["length"] == length and len(bytes.fromhex(read["data"]["raw_data_hex"])) == length
        partial = await call(session, "read_inputs", {"slave_address": 8, "length": 3})
        assert partial["data"]["length"] == 3
        too_long = await call(session, "read_inputs", {"slave_address": 5, "length": 5})
        assert too_long["success"] is False and "4 input bytes" in too_long["error"]
        assert too_long["meta"]["backend"] == "mock"

        diag = await call(session, "read_diagnosis", {"slave_address": 5})
        assert diag["success"] is True
        assert diag["data"] == {"slave_address": 5, "status": "OK", "ident_number": "0x809C"}

        reachable = await call(session, "test_slave_communication", {"slave_address": 8})
        assert reachable["success"] is True and reachable["data"]["reachable"] is True

        # No slave at 9: every tool says so instead of inventing data.
        for tool in ("read_inputs", "read_diagnosis", "test_slave_communication"):
            missing = await call(session, tool, {"slave_address": 9})
            assert missing["success"] is False and "Slave not found at address 9" in missing["error"], tool
            assert missing["data"] is None and missing["meta"]["backend"] == "mock"

        listed = await call(session, "list_slaves")
        assert listed["success"] is True and listed["data"]["count"] == 2
        by_alias = await call(session, "read_slave_by_alias", {"alias": "valve_island"})
        assert by_alias["success"] is True and by_alias["data"]["length"] == 8

        refused = await call(session, "write_outputs", {"slave_address": 5, "data": [1, 2]})
        assert refused["success"] is False and "PROFIBUS_WRITES_ENABLED=true" in refused["error"]
        assert refused["meta"]["outcome"] == "not_sent" and refused["meta"]["request_sent"] is False
        refused = await call(session, "write_slave_by_alias", {"alias": "drive1", "data": [1]})
        assert refused["success"] is False and "Writes are disabled" in refused["error"]
        assert refused["meta"]["outcome"] == "not_sent"

        bad = await call(session, "read_inputs", {"slave_address": "abc"})
        assert bad["success"] is False and bad["error"].startswith("Invalid arguments: slave_address")

        ping = await call(session, "ping")
        assert ping["data"]["backend"] == "mock" and ping["data"]["writes_enabled"] is False
        status = await call(session, "get_master_status")
        assert status["data"]["endpoint"] == f"127.0.0.1:{mock_port}" and status["data"]["writes_enabled"] is False

    outputs_after = mock_request(mock_port, {"op": "read_outputs", "address": 5})["data"]["raw_data_hex"]
    assert outputs_after == outputs_before  # nothing was written


@pytest.mark.anyio
async def test_writes_reach_the_mock_when_enabled(mock_port: int, tmp_path: Path, slave_map: Path) -> None:
    env = mock_env(mock_port, PROFIBUS_WRITES_ENABLED="true", SLAVE_MAP_FILE=str(slave_map))
    async with mcp_session(env, tmp_path) as (session, _):
        written = await call(session, "write_outputs", {"slave_address": 5, "data": [0x12, 0x34]})
        assert written["success"] is True and written["data"]["written_bytes"] == 2
        assert written["meta"]["backend"] == "mock" and written["meta"]["attempts"] == 1
        assert written["meta"]["outcome"] == "written" and written["meta"]["request_sent"] is True
        assert mock_request(mock_port, {"op": "read_outputs", "address": 5})["data"]["raw_data_hex"] == "1234"

        too_many = await call(session, "write_outputs", {"slave_address": 5, "data": [1, 2, 3]})
        assert too_many["success"] is False and "2 output bytes" in too_many["error"]
        assert too_many["meta"]["outcome"] == "rejected" and too_many["meta"]["request_sent"] is True
        assert mock_request(mock_port, {"op": "read_outputs", "address": 5})["data"]["raw_data_hex"] == "1234"

        by_alias = await call(session, "write_slave_by_alias", {"alias": "valve_island", "data": [9, 8, 7, 6]})
        assert by_alias["success"] is True
        assert mock_request(mock_port, {"op": "read_outputs", "address": 8})["data"]["raw_data_hex"] == "09080706"


@pytest.mark.anyio
async def test_env_file_rules_over_stdio(mock_port: int, tmp_path: Path) -> None:
    """Only profibus-python/.env is read, and from it only this server's non-gate settings.

    Runs a copy of the source tree, so the developer's real profibus-python/.env is untouched.
    """
    project = tmp_path / "checkout" / "profibus-python"
    shutil.copytree(
        Path(profibus_mcp_source()),
        project / "src" / "profibus_mcp",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    shutil.copy(TESTS_DIR.parent / "pyproject.toml", project / "pyproject.toml")
    (project / ".env").write_text(
        f"PROFIBUS_BACKEND=mock\nprofibus_mock_port={mock_port}\n"
        "profibus_writes_enabled=true\nexport Profibus_Writes_Enabled='yes'\n"
        "PYTHONSTARTUP=/nonexistent/startup.py\nFOO_UNRELATED=1\n",
        encoding="utf-8",
    )
    # The client's (untrusted) project: neither its .env nor its parent's is read.
    work = tmp_path / "client-project" / "sub"
    work.mkdir(parents=True)
    (work / ".env").write_text("PROFIBUS_MOCK_PORT=1\nPROFIBUS_WRITES_ENABLED=true\n", encoding="utf-8")
    (work.parent / ".env").write_text("PROFIBUS_MOCK_HOST=192.0.2.1\n", encoding="utf-8")

    before = mock_request(mock_port, {"op": "read_outputs", "address": 8})["data"]["raw_data_hex"]
    stderr_path = tmp_path / "server.stderr"
    with stderr_path.open("w") as errlog:
        env = {"PYTHONPATH": str(project / "src")}
        async with mcp_session(env, work, SERVER_MODULE, errlog) as (session, _):
            ping = await call(session, "ping")
            assert ping["data"]["backend"] == "mock" and ping["data"]["writes_enabled"] is False
            scan = await call(session, "scan_bus")  # reaches the mock on the project's port, not the cwd's
            assert scan["success"] is True and scan["meta"]["endpoint"] == f"127.0.0.1:{mock_port}"
            refused = await call(session, "write_outputs", {"slave_address": 8, "data": [1, 2, 3, 4]})
            assert refused["success"] is False and refused["meta"]["outcome"] == "not_sent"
    assert mock_request(mock_port, {"op": "read_outputs", "address": 8})["data"]["raw_data_hex"] == before
    stderr = stderr_path.read_text()
    assert f"loaded {project / '.env'}" in stderr
    for name in ("profibus_writes_enabled", "Profibus_Writes_Enabled", "PYTHONSTARTUP", "FOO_UNRELATED"):
        assert f"{name} in " in stderr, name


def profibus_mcp_source() -> str:
    import profibus_mcp

    return os.path.dirname(profibus_mcp.__file__)


@pytest.mark.skipif(sys.platform == "win32", reason="uses SIGKILL")
def test_mock_started_by_the_tests_dies_with_its_parent(tmp_path: Path) -> None:
    """A killed test run (SIGKILL, no cleanup) must not leave the mock running."""
    port = free_port()
    parent_script = (
        "import subprocess, sys, json\n"
        "child = subprocess.Popen(json.loads(sys.argv[1]), stdin=subprocess.PIPE,"
        " stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)\n"
        "print(child.pid, flush=True)\n"
        "import time; time.sleep(600)\n"
    )
    command = json.dumps(mock_command(_uv(), port))
    parent = subprocess.Popen([sys.executable, "-c", parent_script, command], stdout=subprocess.PIPE, env=_mock_env())
    child_pid = int(parent.stdout.readline())
    try:
        deadline = time.monotonic() + 180
        while True:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
                break
            except OSError:
                assert time.monotonic() < deadline, "the mock never started listening"
                time.sleep(0.2)
        parent.send_signal(signal.SIGKILL)
        parent.wait(timeout=10)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            except OSError:
                break
            time.sleep(0.2)
        else:
            pytest.fail("the mock kept listening after its parent was killed")
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)
        try:
            os.killpg(child_pid, signal.SIGKILL)  # only if something survived
        except ProcessLookupError:
            pass


@pytest.mark.anyio
async def test_unreachable_mock_is_reported_and_the_server_stays_up(tmp_path: Path) -> None:
    env = mock_env(free_port(), PROFIBUS_MAX_RETRIES="1", PROFIBUS_RETRY_BACKOFF_BASE="0")
    async with mcp_session(env, tmp_path) as (session, _):
        scan = await call(session, "scan_bus")
        assert scan["success"] is False and "Cannot reach the PROFIBUS mock slave" in scan["error"]
        assert scan["meta"]["backend"] == "mock" and scan["meta"]["attempts"] == 2
        assert (await call(session, "ping"))["success"] is True


@pytest.mark.anyio
async def test_default_serial_backend_starts_without_an_adapter(tmp_path: Path) -> None:
    # The scaffold opened /dev/ttyUSB0 in the lifespan hook and died before
    # answering initialize when no adapter was present.
    async with mcp_session({}, tmp_path) as (session, init):
        assert init.serverInfo.name == "PROFIBUS MCP Server"
        scan = await call(session, "scan_bus")
        assert scan["success"] is False and "not implemented" in scan["error"]
        assert scan["meta"]["backend"] == "serial" and scan["data"] is None
        status = await call(session, "get_master_status")
        assert status["data"]["port_opened"] is False


# ---------------------------------------------------------------------------
# Raw process checks: stdout is JSON-RPC only, startup errors are clean
# ---------------------------------------------------------------------------


def _server_env(**extra: str) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("PROFIBUS_")}
    env.pop("SLAVE_MAP_FILE", None)
    env.update(extra)
    return env


def test_stdout_carries_only_json_rpc(mock_port: int, tmp_path: Path, slave_map: Path) -> None:
    """Every tool is called on an unbuffered server; any stray print() would break a stdout line."""
    env = _server_env(
        PYTHONUNBUFFERED="1",  # without this a stray print() sits in the buffer and is lost at exit
        PROFIBUS_MOCK="true",  # deprecated alias: logs a warning
        PROFIBUS_CONFIG_CMDS_ENABLED="true",  # ignored setting: logs a warning
        PROFIBUS_MOCK_PORT=str(mock_port),
        PROFIBUS_WRITES_ENABLED="true",
        SLAVE_MAP_FILE=str(slave_map),
        PROFIBUS_GSD_PATH=str(MOCK_GSD_DIR),
    )
    process = subprocess.Popen(
        server_cmd(tmp_path),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=tmp_path,
    )
    lines: queue.Queue[bytes] = queue.Queue()
    threading.Thread(target=lambda: [lines.put(line) for line in process.stdout], daemon=True).start()
    stderr_chunks: list[bytes] = []
    threading.Thread(target=lambda: stderr_chunks.extend(process.stderr), daemon=True).start()
    stdout_lines: list[bytes] = []

    def send(message: dict[str, Any]) -> None:
        process.stdin.write(json.dumps(message).encode() + b"\n")
        process.stdin.flush()

    def response(request_id: int) -> dict[str, Any]:
        while True:
            line = lines.get(timeout=30)
            stdout_lines.append(line)
            message = json.loads(line)  # every stdout line must be JSON-RPC
            assert message.get("jsonrpc") == "2.0", line
            if message.get("id") == request_id:
                return message

    calls = [
        ("scan_bus", {}),
        ("read_inputs", {"slave_address": 5}),
        ("read_inputs", {"slave_address": [1]}),  # invalid: still an envelope
        ("write_outputs", {"slave_address": 5, "data": [7]}),
        ("read_diagnosis", {"slave_address": 8}),
        ("test_slave_communication", {"slave_address": 9}),
        ("list_slaves", {}),
        ("read_slave_by_alias", {"alias": "drive1"}),
        ("write_slave_by_alias", {"alias": "valve_island", "data": [1]}),
        ("load_gsd_file", {"filepath": "IA_809C.gsd"}),
        ("load_gsd_file", {"filepath": "missing.gsd"}),
        ("ping", {}),
        ("get_master_status", {}),
    ]
    try:
        send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "pytest", "version": "0"},
                },
            }
        )
        assert response(1)["result"]["serverInfo"]["version"] == version("profibus-mcp")
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert {tool["name"] for tool in response(2)["result"]["tools"]} == TOOLS
        for request_id, (name, arguments) in enumerate(calls, start=3):
            send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                }
            )
            result = response(request_id)["result"]
            assert not result.get("isError"), (name, result)
            envelope = json.loads(result["content"][0]["text"])
            assert set(envelope) == {"success", "data", "error", "meta"}, (name, envelope)
        assert {name for name, _ in calls} == TOOLS
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    time.sleep(0.2)
    while not lines.empty():
        line = lines.get()
        stdout_lines.append(line)
        assert json.loads(line).get("jsonrpc") == "2.0", line
    assert len(stdout_lines) >= len(calls) + 2
    stderr = b"".join(stderr_chunks).decode()
    assert "deprecated" in stderr and "PROFIBUS_CONFIG_CMDS_ENABLED is ignored" in stderr


def test_configuration_error_is_reported_on_stderr_without_traceback(tmp_path: Path) -> None:
    result = subprocess.run(
        server_cmd(tmp_path),
        env=_server_env(PROFIBUS_BAUDRATE="fast"),
        cwd=tmp_path,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 2
    assert result.stdout == b""
    assert b"configuration error: PROFIBUS_BAUDRATE must be an integer" in result.stderr
    assert b"Traceback" not in result.stderr
