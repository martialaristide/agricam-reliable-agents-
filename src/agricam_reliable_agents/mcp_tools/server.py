"""
Serveur MCP réel exposant les outils AgriCam via le SDK officiel `mcp`.

Ce module relie le protocole (Model Context Protocol, transport stdio)
au dispatch déjà existant (`AgriCamTools.dispatch`) sans dupliquer la
logique métier : les schémas déclarés au client MCP sont exactement ceux
de `ALL_TOOL_SCHEMAS`, ce qui garantit qu'un agent connecté via MCP et
l'agent interne (`agent/loop.py`) voient le même contrat d'outils.

Décisions de conception
-----------------------
- API bas niveau `mcp.server.lowlevel.Server` plutôt que `MCPServer`
  (ex-FastMCP) : cette dernière dérive les schémas des signatures Python,
  alors que le projet possède déjà des schémas JSON validés et versionnés.
  Le bas niveau permet de les exposer tels quels.
- Les erreurs métier (`AgriCamDataError`) et d'appel (`ValueError`,
  `TypeError` : outil inconnu, arguments invalides) sont converties en
  `CallToolResult(is_error=True)` plutôt qu'en erreur JSON-RPC : le client
  LLM reçoit un résultat d'outil en échec qu'il peut interpréter, ce qui
  reproduit le comportement de la boucle agent interne.
- Le résultat est renvoyé à la fois en texte JSON (`content`) et en
  `structured_content` : les clients récents exploitent le second, les
  anciens le premier.
- Le serveur stdio choisit son store d'après l'environnement
  (`tools_from_env`) : `AGRICAM_DATABASE_URL` définie → store persistant
  SQLAlchemy (SQLite/PostgreSQL), semé une seule fois s'il est vide ;
  absente → store en mémoire pré-rempli (`default_tools`). Pour tout autre
  cas, injecter directement `build_server(AgriCamTools(store))`.

Lancement :
    PYTHONPATH=src python -m agricam_reliable_agents.mcp_tools.server
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import Any

import anyio
from mcp import types
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from agricam_reliable_agents import __version__
from agricam_reliable_agents.mcp_tools.data_store import (
    AgriCamDataError,
    AgriCamDataStore,
)
from agricam_reliable_agents.mcp_tools.sql_store import SqlAlchemyDataStore
from agricam_reliable_agents.mcp_tools.tools import ALL_TOOL_SCHEMAS, AgriCamTools
from agricam_reliable_agents.persistence.engine import DATABASE_URL_ENV_VAR

SERVER_NAME = "agricam-reliable-agents"


def mcp_tool_definitions() -> list[types.Tool]:
    """Convertit `ALL_TOOL_SCHEMAS` au format `mcp.types.Tool`."""
    return [
        types.Tool(
            name=schema["name"],
            description=schema["description"],
            input_schema=schema["input_schema"],
        )
        for schema in ALL_TOOL_SCHEMAS
    ]


def _text_result(payload: Any, *, is_error: bool = False) -> types.CallToolResult:
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, default=str)
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)],
        structured_content=payload if isinstance(payload, dict) else None,
        is_error=is_error,
    )


def build_server(tools: AgriCamTools) -> Server[Any]:
    """Instancie un serveur MCP dont les 5 outils délèguent à `tools.dispatch`."""

    async def on_list_tools(
        _ctx: ServerRequestContext[Any], _params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return types.ListToolsResult(tools=mcp_tool_definitions())

    async def on_call_tool(
        _ctx: ServerRequestContext[Any], params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        try:
            result = tools.dispatch(params.name, dict(params.arguments or {}))
        except AgriCamDataError as exc:
            return _text_result(f"Erreur outil : {exc}", is_error=True)
        except (ValueError, TypeError) as exc:
            return _text_result(f"Appel d'outil invalide : {exc}", is_error=True)
        return _text_result(result)

    return Server(
        SERVER_NAME,
        version=__version__,
        description="Outils AgriCam : capteurs, diagnostics, marketplace, notifications.",
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )


def default_tools() -> AgriCamTools:
    """Outils liés à un store en mémoire pré-rempli (mode démonstration)."""
    store = AgriCamDataStore()
    store.seed_demo_data()
    return AgriCamTools(store)


def tools_from_env(environ: Mapping[str, str] | None = None) -> AgriCamTools:
    """Store persistant si `AGRICAM_DATABASE_URL` est définie, mémoire sinon."""
    env = os.environ if environ is None else environ
    url = env.get(DATABASE_URL_ENV_VAR)
    if not url:
        return default_tools()
    store = SqlAlchemyDataStore.from_url(url)
    store.seed_demo_data()  # sans effet si la base contient déjà des données
    return AgriCamTools(store)


async def serve_stdio(tools: AgriCamTools | None = None) -> None:
    """Démarre le serveur sur stdin/stdout et bloque jusqu'à fermeture du flux."""
    server = build_server(tools or tools_from_env())
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    anyio.run(serve_stdio)


if __name__ == "__main__":  # pragma: no cover
    main()
