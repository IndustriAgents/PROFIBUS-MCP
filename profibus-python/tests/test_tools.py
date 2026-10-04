"""The MCP tool layer, driven through an in-memory MCP client session."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

import profibus_mcp
from profibus_mcp.config import ProfibusConfig
from profibus_mcp.pb_master import ProfibusMaster
from profibus_mcp.server import ProfibusMCPServer

from .conftest import DATA_DIR
from .fakes import FakeBackend

pytestmark = pytest.mark.anyio

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
WRITE_TOOLS = {"write_outputs", "write_slave_by_alias"}


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
    assert not result.isError, result
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
        else:
            assert tool.annotations.readOnlyHint is True, name


def test_server_reports_its_own_version():
    # FastMCP would report the mcp SDK's version; the stdio test checks serverInfo too.
    server = ProfibusMCPServer(ProfibusConfig())
    options = server.mcp._mcp_server.create_initialization_options()
    assert options.server_name == "PROFIBUS MCP Server"
    assert options.server_version == profibus_mcp.__version__


async def test_writes_are_disabled_by_default():
    backend = FakeBackend(fake_replies())
    async with connect(ProfibusConfig(backend="mock"), backend) as session:
        result = await call(session, "write_outputs", {"slave_address": 5, "data": [1, 2]})
        assert result["success"] is False
        assert "PROFIBUS_WRITES_ENABLED=true" in result["error"] and "Nothing was sent" in result["error"]
        assert result["meta"]["writes_enabled"] is False

        result = await call(session, "write_slave_by_alias", {"alias": "drive1", "data": [1]})
        assert result["success"] is False and "Writes are disabled" in result["error"]

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
    assert backend.calls == [("write_outputs", {"address": 5, "data": [1, 255]}, False)]


@pytest.mark.parametrize(
    ("tool", "arguments", "problem"),
    [
        ("read_inputs", {"slave_address": "abc"}, "slave_address"),
        ("read_inputs", {"slave_address": True}, "slave_address"),
        ("read_inputs", {"slave_address": 126}, "slave_address"),
        ("read_inputs", {"slave_address": 5, "length": 0}, "length"),
        ("read_inputs", {}, "slave_address: Field required"),
        ("write_outputs", {"slave_address": 5, "data": [256]}, "data.0"),
        ("write_outputs", {"slave_address": 5, "data": []}, "data"),
        ("write_outputs", {"slave_address": 5, "data": "not a list"}, "data"),
        ("read_diagnosis", {"slave_address": 2.5}, "slave_address"),
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
    assert result["meta"] == {"tool": tool}
    assert backend.calls == []


async def test_numeric_strings_are_accepted_for_integers():
    backend = FakeBackend(fake_replies())
    async with connect(ProfibusConfig(backend="mock"), backend) as session:
        result = await call(session, "read_inputs", {"slave_address": "5", "length": "2"})
    assert result["success"] is True and result["data"]["raw_data_hex"] == "0102"


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
    async with connect(ProfibusConfig(backend="mock"), FakeBackend(replies)) as session:
        result = await call(session, "scan_bus")
    assert result["success"] is False and "boom" in result["error"]


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
        unknown = await call(session, "read_slave_by_alias", {"alias": "nope"})
        assert unknown["success"] is False and "Unknown alias" in unknown["error"]
    assert [call_[1]["address"] for call_ in backend.calls] == [5, 5]


async def test_broken_or_missing_slave_map_is_an_error_not_an_empty_list(tmp_path):
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    async with connect(ProfibusConfig(slave_map_path=broken)) as session:
        result = await call(session, "list_slaves")
        assert result["success"] is False and "not valid JSON" in result["error"]
    async with connect(ProfibusConfig(slave_map_path=tmp_path / "missing.json")) as session:
        result = await call(session, "list_slaves")
        assert result["success"] is False and "cannot be read" in result["error"]
    async with connect(ProfibusConfig()) as session:
        result = await call(session, "list_slaves")
        assert result["success"] is True and result["data"] == {"slaves": [], "count": 0}


async def test_load_gsd_file_tool(tmp_path):
    config = ProfibusConfig(gsd_base_path=DATA_DIR)
    async with connect(config) as session:
        first = await call(session, "load_gsd_file", {"filepath": "IA_1234.gsd"})
        assert first["success"] and first["data"]["metadata"]["ident_number"] == "0x1234"
        assert first["meta"]["source"] == "gsd_file" and first["meta"]["cached"] is False
        again = await call(session, "load_gsd_file", {"filepath": "IA_1234.gsd"})
        assert again["meta"]["cached"] is True

        missing = await call(session, "load_gsd_file", {"filepath": "nope.gsd"})
        assert missing["success"] is False and "not found" in missing["error"]

        xml = tmp_path / "device.xml"
        xml.write_text("<ISO15745Profile/>", encoding="utf-8")
        rejected = await call(session, "load_gsd_file", {"filepath": str(xml)})
        assert rejected["success"] is False and "GSDML" in rejected["error"]


async def test_ping_reports_backend_and_version():
    async with connect(ProfibusConfig(backend="mock")) as session:
        ping = await call(session, "ping")
    assert ping["success"] is True
    assert ping["data"]["version"] == profibus_mcp.__version__
    assert ping["data"]["backend"] == "mock" and ping["meta"]["backend"] == "mock"
    assert ping["data"]["writes_enabled"] is False
    assert "config_cmds_enabled" not in ping["data"]
