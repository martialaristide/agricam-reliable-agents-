"""
Tests d'intégration du serveur MCP réel.

Deux niveaux :
1. En mémoire : client et serveur MCP reliés par des flux anyio dans le
   même processus (rapide, déterministe, couvert par pytest-cov).
2. En sous-processus : le vrai point d'entrée `python -m ...server` est
   lancé sur stdio, et un vrai `ClientSession` du SDK négocie le protocole.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import anyio
import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.shared.memory import create_client_server_memory_streams

from agricam_reliable_agents.mcp_tools import server as server_module
from agricam_reliable_agents.mcp_tools.server import (
    SERVER_NAME,
    build_server,
    default_tools,
    mcp_tool_definitions,
    serve_stdio,
)
from agricam_reliable_agents.mcp_tools.tools import ALL_TOOL_SCHEMAS

EXPECTED_TOOL_NAMES = [s["name"] for s in ALL_TOOL_SCHEMAS]
SRC_DIR = str(Path(__file__).resolve().parents[1] / "src")

pytestmark = pytest.mark.anyio


class _InMemoryMCP:
    """Contexte : serveur `build_server(...)` + `ClientSession` initialisée."""

    def __init__(self) -> None:
        self.tools = default_tools()

    async def __aenter__(self) -> ClientSession:
        self._streams = create_client_server_memory_streams()
        client_streams, server_streams = await self._streams.__aenter__()
        server = build_server(self.tools)
        self._tg = anyio.create_task_group()
        await self._tg.__aenter__()
        self._tg.start_soon(
            server.run, server_streams[0], server_streams[1],
            server.create_initialization_options(),
        )
        self._session = ClientSession(client_streams[0], client_streams[1])
        await self._session.__aenter__()
        await self._session.initialize()
        return self._session

    async def __aexit__(self, *exc) -> None:
        await self._session.__aexit__(*exc)
        self._tg.cancel_scope.cancel()
        await self._tg.__aexit__(None, None, None)
        await self._streams.__aexit__(None, None, None)


def test_tool_definitions_mirror_all_tool_schemas():
    definitions = mcp_tool_definitions()
    assert [t.name for t in definitions] == EXPECTED_TOOL_NAMES
    for definition, schema in zip(definitions, ALL_TOOL_SCHEMAS, strict=True):
        assert definition.description == schema["description"]
        assert definition.input_schema == schema["input_schema"]


async def test_list_tools_returns_the_five_tools():
    async with _InMemoryMCP() as session:
        result = await session.list_tools()
    assert sorted(t.name for t in result.tools) == sorted(EXPECTED_TOOL_NAMES)
    assert len(result.tools) == 5


async def test_call_get_sensor_data_returns_consistent_payload():
    async with _InMemoryMCP() as session:
        result = await session.call_tool("get_sensor_data", {"parcel_id": "P-003", "metric": "humidity"})

    assert result.is_error is not True
    assert result.structured_content is not None
    assert result.structured_content["parcel_id"] == "P-003"
    assert result.structured_content["readings"][0]["metric"] == "humidity"
    assert result.structured_content["readings"][0]["value"] == 82.0
    # Le contenu texte est le même JSON (clients sans structured_content)
    text = result.content[0].text
    assert json.loads(text) == result.structured_content


async def test_call_tool_mutates_the_shared_store():
    ctx = _InMemoryMCP()
    async with ctx as session:
        before = await session.call_tool("check_marketplace_stock", {"product_id": "PRD-7"})
        treated = await session.call_tool("recommend_treatment", {"diagnostic_id": "D-42"})
        after = await session.call_tool("check_marketplace_stock", {"product_id": "PRD-7"})

    assert treated.structured_content["status"] == "treated"
    assert after.structured_content["stock_qty"] == before.structured_content["stock_qty"] - 1
    assert ctx.tools._store.snapshot()["diagnostic.D-42.status"] == "treated"


async def test_business_error_is_returned_as_tool_error_not_protocol_error():
    async with _InMemoryMCP() as session:
        result = await session.call_tool("get_sensor_data", {"parcel_id": "P-NOPE"})
    assert result.is_error is True
    assert "Erreur outil" in result.content[0].text


async def test_unknown_tool_and_bad_arguments_are_tool_errors():
    async with _InMemoryMCP() as session:
        unknown = await session.call_tool("delete_everything", {})
        bad_args = await session.call_tool("get_sensor_data", {"unexpected": 1})
    assert unknown.is_error is True and "inconnu" in unknown.content[0].text
    assert bad_args.is_error is True and "invalide" in bad_args.content[0].text


async def test_server_identity():
    async with _InMemoryMCP() as session:
        init = await session.initialize()
    # `initialize` est idempotent côté test : on relit l'identité annoncée
    assert init.server_info.name == SERVER_NAME


async def test_serve_stdio_uses_stdio_transport(monkeypatch):
    """`serve_stdio` doit brancher le serveur sur `stdio_server` : on
    substitue le transport par des flux mémoire pour le vérifier sans
    toucher au vrai stdin/stdout du processus de test."""
    from contextlib import asynccontextmanager

    async with create_client_server_memory_streams() as (client_streams, server_streams):

        @asynccontextmanager
        async def fake_stdio_server():
            yield server_streams

        monkeypatch.setattr(server_module, "stdio_server", fake_stdio_server)

        async with anyio.create_task_group() as tg:
            tg.start_soon(serve_stdio)
            async with ClientSession(client_streams[0], client_streams[1]) as session:
                await session.initialize()
                result = await session.list_tools()
                assert len(result.tools) == 5
            tg.cancel_scope.cancel()


def test_main_runs_serve_stdio_with_anyio(monkeypatch):
    captured = {}
    monkeypatch.setattr(server_module.anyio, "run", lambda fn, *a, **k: captured.setdefault("fn", fn))
    server_module.main()
    assert captured["fn"] is serve_stdio


async def test_real_subprocess_over_stdio_lists_tools_and_calls_one():
    """Lance le vrai module en sous-processus (transport stdio réel)."""
    env = {**os.environ, "PYTHONPATH": SRC_DIR, "PYTHONIOENCODING": "utf-8"}
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "agricam_reliable_agents.mcp_tools.server"],
        env=env,
    )
    with anyio.fail_after(60):
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
                called = await session.call_tool("get_diagnostic_history", {"parcel_id": "P-003"})

    assert sorted(t.name for t in listed.tools) == sorted(EXPECTED_TOOL_NAMES)
    assert called.is_error is not True
    assert called.structured_content["diagnostics"][0]["id"] == "D-42"
