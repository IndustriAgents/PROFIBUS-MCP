"""The MCP tool layer, driven through an in-memory MCP client session."""

from __future__ import annotations

import json
import shutil
from contextlib import asynccontextmanager
from importlib.metadata import version
from typing import Any

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from profibus_mcp.config import ProfibusConfig
from profibus_mcp.pb_master import ProfibusMaster
from profibus_mcp.server import ProfibusMCPServer

from .conftest import DATA_DIR
from .fakes import FakeBackend, RecordingStream, fake_mock_server

pytestmark = pytest.mark.anyio

TOOLS = {
    "load_gsd_file",
    "read_diagnosis",
    "get_master_status",
    "write_outputs",
    "list_slaves",
    "read_inputs",
    "scan_bus",
}
WRITE_TOOLS = {"write_outputs"}


def fake_replies() -> dict[str, Any]:
    return {
        "scan": [{"address": 5, "ident_number": "0x809C", "status": "OK", "input_length": 4, "output_length": 2}],
        "read_inputs": lambda params: {"raw_data_hex": "01020304"[: 2 * params.get("length", 4)]},
        "write_outputs": lambda params: {"written_bytes": len(params["data"])},
        "diagnosis": {"address": 5, "status": "OK", "ident_number": "0x809C"},
    }


@asynccontextmanager
async def connect(config: ProfibusConfig | None = None, backend: FakeBackend | None = None):
    config = config or ProfibusConfig()
    master = ProfibusMaster(config, backend) if backend else None
    server = ProfibusMCPServer(config, master)
    async with create_connected_server_and_client_session(server.mcp) as session:
        yield session


async def call(session, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    result = await session.call_tool(name, arguments or {})
    assert result.isError is (json.loads(result.content[0].text).get("success") is False), result
    envelope = json.loads(result.content[0].text)
    assert set(envelope) == {"success", "data", "error", "meta"}
    assert result.structuredContent == envelope
    return envelope


async def test_every_tool_has_a_description_and_write_tools_say_so():
    async with connect() as session:
        tools = {tool.name: tool for tool in (await session.list_tools()).tools}
    assert set(tools) == TOOLS
    for name, tool in tools.items():
        assert tool.description and tool.description.strip(), name
        if name in WRITE_TOOLS:
            assert "physical outputs" in tool.description
            assert "PROFIBUS_WRITES_ENABLED" in tool.description
            assert tool.annotations.readOnlyHint is False and tool.annotations.destructiveHint is True
            # A write must never advertise idempotency, or clients may retry it on their own.
            assert tool.annotations.idempotentHint is False
        else:
            assert tool.annotations.readOnlyHint is True, name


async def test_status_reports_serial_limit_without_opening_a_bus():
    from jsonschema import Draft202012Validator

    async with connect() as session:
        result = await call(session, "get_master_status")
        caps = result["data"]["capabilities"]
        assert caps["device_verified"] is False and caps["backend"] == "serial"
        assert caps["operations"]["read_inputs"]["supported"] is False
        assert caps["operations"]["write_outputs"]["enabled"] is False
        assert caps["operations"]["load_gsd_file"]["supported"] is True
        assert result["data"]["port_opened"] is False
        tools = {t.name: t for t in (await session.list_tools()).tools}
        Draft202012Validator(tools["get_master_status"].outputSchema).validate(result)


def test_server_reports_its_own_version():
    # FastMCP would report the mcp SDK's version; the stdio test checks serverInfo too.
    server = ProfibusMCPServer(ProfibusConfig())
    options = server.mcp._mcp_server.create_initialization_options()
    assert options.server_name == "PROFIBUS MCP Server"
    assert options.server_version == version("industriagents-profibus-mcp")
    assert options.server_version != version("mcp")


async def test_writes_are_disabled_by_default():
    backend = FakeBackend(fake_replies())
    async with connect(ProfibusConfig(backend="mock"), backend) as session:
        result = await call(session, "write_outputs", {"slave_address": 5, "data": [1, 2]})
        assert result["success"] is False
        assert "PROFIBUS_WRITES_ENABLED=true" in result["error"] and "Nothing was sent" in result["error"]
        assert result["meta"]["writes_enabled"] is False
        assert result["meta"]["outcome"] == "not_sent" and result["meta"]["request_sent"] is False

        result = await call(session, "write_slave_by_alias", {"alias": "drive1", "data": [1]})
        assert result["success"] is False and "Writes are disabled" in result["error"]
        assert result["meta"]["outcome"] == "not_sent"

        ping = await call(session, "ping")
        status = await call(session, "get_master_status")
        assert ping["data"]["writes_enabled"] is status["data"]["writes_enabled"] is False
    assert backend.calls == []


async def test_enabled_writes_reach_the_backend():
    backend = FakeBackend(fake_replies())
    async with connect(ProfibusConfig(backend="mock", writes_enabled=True), backend) as session:
        result = await call(session, "write_outputs", {"slave_address": 5, "data": [1, 255]})
    assert result["success"] is True
    assert result["data"] == {"slave_address": 5, "written_bytes": 2}
    assert result["meta"]["backend"] == "fake"
    assert result["meta"]["outcome"] == "written"
    assert backend.calls == [("write_outputs", {"address": 5, "data": [1, 255]}, False)]


@pytest.mark.parametrize(
    ("tool", "arguments", "problem"),
    [
        ("read_inputs", {"slave_address": "abc"}, "slave_address"),
        ("read_inputs", {"slave_address": "5"}, "slave_address"),  # strict: no numeric strings
        ("read_inputs", {"slave_address": 5.0}, "slave_address"),  # strict: no floats
        ("read_inputs", {"slave_address": True}, "slave_address"),  # strict: no booleans
        ("read_inputs", {"slave_address": 126}, "slave_address"),
        ("read_inputs", {"slave_address": 5, "length": 0}, "length"),
        ("read_inputs", {"slave_address": 5, "length": True}, "length"),
        ("read_inputs", {}, "slave_address: Field required"),
        ("write_outputs", {"slave_address": 5, "data": [256]}, "data.0"),
        ("write_outputs", {"slave_address": 5, "data": [True]}, "data.0"),
        ("write_outputs", {"slave_address": 5, "data": ["1"]}, "data.0"),
        ("write_outputs", {"slave_address": 5, "data": []}, "data"),
        ("write_outputs", {"slave_address": 5, "data": "not a list"}, "data"),
        ("write_outputs", {"slave_address": False, "data": [1]}, "slave_address"),
        ("write_slave_by_alias", {"alias": "drive1", "data": [1.5]}, "data.0"),
        ("read_diagnosis", {"slave_address": 2.5}, "slave_address"),
        ("test_slave_communication", {"slave_address": None}, "slave_address"),
        ("read_slave_by_alias", {"alias": ""}, "alias"),
    ],
)
async def test_bad_arguments_come_back_as_envelopes(tool, arguments, problem):
    backend = FakeBackend(fake_replies())
    async with connect(ProfibusConfig(backend="mock", writes_enabled=True), backend) as session:
        result = await call(session, tool, arguments)
    assert result["success"] is False
    assert result["error"].startswith("Invalid arguments:")
    assert problem in result["error"]
    expected_meta = {"tool": tool, "backend": "fake"}
    if tool in {"write_outputs", "write_slave_by_alias"}:
        expected_meta.update(outcome="not_sent", request_sent=False)
    assert result["meta"] == expected_meta
    assert backend.calls == []


async def test_unknown_tool_is_a_protocol_error():
    async with connect() as session:
        result = await session.call_tool("no_such_tool", {})
    assert result.isError


async def test_serial_backend_reports_not_implemented_instead_of_inventing_data():
    async with connect(ProfibusConfig(writes_enabled=True)) as session:
        for tool, arguments in [
            ("scan_bus", {}),
            ("read_inputs", {"slave_address": 5, "length": 4}),
            ("write_outputs", {"slave_address": 5, "data": [1]}),
            ("read_diagnosis", {"slave_address": 5}),
            ("test_slave_communication", {"slave_address": 5}),
        ]:
            result = await call(session, tool, arguments)
            assert result["success"] is False, tool
            assert "not implemented" in result["error"]
            assert result["data"] is None
            assert result["meta"]["backend"] == "serial"

        status = await call(session, "get_master_status")
        assert status["success"] is True
        assert status["data"]["bus_communication"] == "not implemented"
        assert status["data"]["port_opened"] is False
        assert "config_cmds_enabled" not in status["data"]


async def test_read_tools_return_backend_data():
    backend = FakeBackend(fake_replies())
    async with connect(ProfibusConfig(backend="mock"), backend) as session:
        scan = await call(session, "scan_bus")
        assert scan["success"] and scan["data"]["slaves"][0]["address"] == 5
        read = await call(session, "read_inputs", {"slave_address": 5})
        assert read["data"] == {"slave_address": 5, "length": 4, "raw_data_hex": "01020304"}
        diag = await call(session, "read_diagnosis", {"slave_address": 5})
        assert diag["data"] == {"slave_address": 5, "status": "OK", "ident_number": "0x809C"}
        test = await call(session, "test_slave_communication", {"slave_address": 5})
        assert test["data"] == {"slave_address": 5, "reachable": True, "status": "OK", "ident_number": "0x809C"}


async def test_unexpected_backend_exception_becomes_an_envelope():
    replies = fake_replies()
    replies["scan"] = RuntimeError("boom")
    replies["write_outputs"] = RuntimeError("bang")
    config = ProfibusConfig(backend="mock", writes_enabled=True)
    async with connect(config, FakeBackend(replies)) as session:
        result = await call(session, "scan_bus")
        assert result["success"] is False and "boom" in result["error"]
        # An unexpected failure during a write cannot prove nothing was sent.
        result = await call(session, "write_outputs", {"slave_address": 5, "data": [1]})
        assert result["success"] is False and "may have been applied" in result["error"]
        assert result["meta"]["outcome"] == "unknown" and result["meta"]["request_sent"] is True


async def test_backend_not_implemented_write_is_not_sent():
    async with connect(ProfibusConfig(writes_enabled=True)) as session:
        result = await call(session, "write_outputs", {"slave_address": 5, "data": [1]})
    assert result["success"] is False and "not implemented" in result["error"]
    assert result["meta"]["outcome"] == "not_sent" and result["meta"]["request_sent"] is False


async def test_slave_aliases(tmp_path):
    slave_map = tmp_path / "slaves.json"
    slave_map.write_text(
        json.dumps(
            {
                "drive1": {"address": 5, "description": "Conveyor drive"},
                "no_address": {"description": "forgot the address"},
                "text_address": {"address": "5"},
                "not_an_object": 5,
            }
        ),
        encoding="utf-8",
    )
    backend = FakeBackend(fake_replies())
    config = ProfibusConfig(backend="mock", writes_enabled=True, slave_map_path=slave_map)
    async with connect(config, backend) as session:
        listed = await call(session, "list_slaves")
        assert listed["success"] and listed["data"]["count"] == 4
        by_alias = {entry["alias"]: entry for entry in listed["data"]["slaves"]}
        assert by_alias["drive1"] == {"alias": "drive1", "address": 5, "description": "Conveyor drive"}
        assert "error" in by_alias["no_address"] and "error" in by_alias["not_an_object"]

        read = await call(session, "read_slave_by_alias", {"alias": "drive1", "length": 2})
        assert read["success"] and read["data"]["alias"] == "drive1" and read["data"]["raw_data_hex"] == "0102"
        write = await call(session, "write_slave_by_alias", {"alias": "drive1", "data": [7]})
        assert write["success"] and write["data"]["written_bytes"] == 1

        # The scaffold silently used address 0 for these.
        for alias in ("no_address", "text_address", "not_an_object"):
            result = await call(session, "read_slave_by_alias", {"alias": alias})
            assert result["success"] is False and f"Alias '{alias}'" in result["error"], alias
            result = await call(session, "write_slave_by_alias", {"alias": alias, "data": [1]})
            assert result["success"] is False and result["meta"]["outcome"] == "not_sent", alias
        unknown = await call(session, "read_slave_by_alias", {"alias": "nope"})
        assert unknown["success"] is False and "Unknown alias" in unknown["error"]
    assert [call_[1]["address"] for call_ in backend.calls] == [5, 5]


async def test_broken_or_missing_slave_map_is_an_error_not_an_empty_list(tmp_path):
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    async with connect(ProfibusConfig(slave_map_path=broken)) as session:
        result = await call(session, "list_slaves")
        assert result["success"] is False and "not valid JSON" in result["error"]
        ping = await call(session, "ping")
        assert "not valid JSON" in ping["data"]["slave_map_error"]
    async with connect(ProfibusConfig(slave_map_path=tmp_path / "missing.json")) as session:
        result = await call(session, "list_slaves")
        assert result["success"] is False and "cannot be read" in result["error"]
    async with connect(ProfibusConfig()) as session:
        result = await call(session, "list_slaves")
        assert result["success"] is True and result["data"] == {"slaves": [], "count": 0}


async def test_load_gsd_file_tool(tmp_path):
    shutil.copy(DATA_DIR / "IA_1234.gsd", tmp_path / "IA_1234.gsd")
    (tmp_path / "device.xml").write_text("<ISO15745Profile/>", encoding="utf-8")
    config = ProfibusConfig(gsd_base_path=tmp_path)
    async with connect(config) as session:
        first = await call(session, "load_gsd_file", {"filepath": "IA_1234.gsd"})
        assert first["success"] and first["data"]["metadata"]["ident_number"] == "0x1234"
        assert first["meta"]["source"] == "gsd_file" and first["meta"]["cached"] is False
        again = await call(session, "load_gsd_file", {"filepath": "IA_1234.gsd"})
        assert again["meta"]["cached"] is True

        missing = await call(session, "load_gsd_file", {"filepath": "nope.gsd"})
        assert missing["success"] is False and "not found" in missing["error"]

        rejected = await call(session, "load_gsd_file", {"filepath": "device.xml"})
        assert rejected["success"] is False and "GSDML" in rejected["error"]

        # With PROFIBUS_GSD_PATH set, nothing outside it is opened.
        outside = await call(session, "load_gsd_file", {"filepath": str(DATA_DIR / "IA_1234.gsd")})
        assert outside["success"] is False and "outside PROFIBUS_GSD_PATH" in outside["error"]
        outside = await call(session, "load_gsd_file", {"filepath": "../../etc/passwd"})
        assert outside["success"] is False and "outside PROFIBUS_GSD_PATH" in outside["error"]


async def test_ping_reports_backend_and_version():
    async with connect(ProfibusConfig(backend="mock")) as session:
        ping = await call(session, "ping")
    assert ping["success"] is True
    assert ping["data"]["version"] == version("industriagents-profibus-mcp")
    assert ping["data"]["slave_map_error"] is None
    assert ping["data"]["backend"] == "mock" and ping["meta"]["backend"] == "mock"
    assert ping["data"]["writes_enabled"] is False
    assert "config_cmds_enabled" not in ping["data"]


async def test_mcp_cancel_in_the_same_step_as_connect_never_sends(monkeypatch):
    """CANCEL RACE through the real SDK: RequestResponder.cancel() in the connect's completion step.

    Also fails if the private mcp internals this relies on move: the scope the
    gate sees must be the in-flight responder's _cancel_scope.
    """
    import anyio
    from mcp.server.lowlevel.server import request_ctx
    from mcp.shared.exceptions import McpError

    from profibus_mcp import pb_master

    async def ok(request):
        if request["op"] == "write_outputs":
            return {"success": True, "data": {"written_bytes": len(request["data"])}}
        return {"success": False, "error": "unexpected", "applied": False}

    attempted: list[bytes] = []
    async with fake_mock_server(ok) as (port, received):
        config = ProfibusConfig(backend="mock", mock_port=port, writes_enabled=True)
        server = ProfibusMCPServer(config)
        real_connect = anyio.connect_tcp
        async with create_connected_server_and_client_session(server.mcp) as session:
            for _ in range(30):
                waiting, go, seen = anyio.Event(), anyio.Event(), {}

                async def connect(*args, _go=go, _waiting=waiting, _seen=seen, **kwargs):
                    stream = await real_connect(*args, **kwargs)
                    rc = request_ctx.get()
                    responder = rc.session._in_flight[rc.request_id]
                    _seen["responder"] = responder
                    _seen["gate_scope_is_responder_scope"] = (
                        pb_master._REQUEST_CANCEL_SCOPE.get() is responder._cancel_scope
                    )
                    _waiting.set()
                    await _go.wait()  # completes in the same step as the SDK's cancel below
                    return RecordingStream(stream, attempted)

                monkeypatch.setattr(anyio, "connect_tcp", connect)
                outcome: dict[str, Any] = {}

                async def call(_outcome=outcome):
                    try:
                        _outcome["result"] = await session.call_tool("write_outputs", {"slave_address": 5, "data": [1]})
                    except McpError as exc:
                        _outcome["error"] = exc

                async with anyio.create_task_group() as tg:
                    tg.start_soon(call)
                    await waiting.wait()
                    go.set()
                    await seen["responder"].cancel()  # what the SDK does on notifications/cancelled
                assert seen["gate_scope_is_responder_scope"] is True
                assert "Request cancelled" in str(outcome.get("error")), outcome
            monkeypatch.setattr(anyio, "connect_tcp", real_connect)
            # The server survived every cancelled call (no second reply to a cancelled request).
            ping = await call_envelope(session, "ping")
            assert ping["success"] is True
        await anyio.sleep(0.1)
    assert attempted == []
    assert received == []


async def call_envelope(session, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    return await call(session, name, arguments)


def test_request_cancel_scope_is_none_outside_a_request():
    from profibus_mcp.tools import capture_request_cancel_scope

    assert capture_request_cancel_scope(ProfibusMCPServer(ProfibusConfig()).mcp) is None
