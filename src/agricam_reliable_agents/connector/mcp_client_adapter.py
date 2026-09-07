"""
Adaptateur MCP client — Phase B du plan d'implémentation.

Pilote un agent exposé comme serveur MCP (le nôtre — `mcp_tools/server.py`
— ou un tiers), via le SDK officiel `mcp` déjà en dépendance du projet
(`mcp>=2.0.0`, `requirements.txt` : il servait déjà au serveur MCP
AgriCam ; aucune nouvelle dépendance ajoutée pour cet adaptateur).

Format de requête/réponse (pour un tiers qui lirait ce code)
-------------------------------------------------------------
Requête : appel de l'outil MCP `tool_name` (défaut : `"chat"`) avec les
arguments `{"prompt": <str>}` (personnalisable via `build_arguments`,
ex. pour appeler un outil métier existant plutôt qu'un outil
conversationnel dédié).
Réponse : le premier bloc `TextContent` du `CallToolResult` est le texte
de l'agent ; `declared_success` est calculé par un `SuccessClaimExtractor`
appliqué à ce texte si `result.is_error` est faux, sinon l'essai est
marqué en erreur (`RawAgentReply.error`).

Pont synchrone/asynchrone
--------------------------
Le SDK MCP est entièrement asynchrone (`ClientSession.call_tool` est une
coroutine) alors que `AgentConnector.send` est synchrone, comme tous les
appelants du harnais. `connect_stdio` ouvre la session MCP UNE SEULE FOIS
sur un `anyio.from_thread.BlockingPortal` : un thread dédié fait tourner
la boucle asyncio, la session y reste vivante entre les essais, et
`send()` ne fait que soumettre `session.call_tool(...)` au portail et
attendre son résultat de façon synchrone (`portal.call(...)`). Rouvrir un
sous-processus par essai serait correct mais inutilement lent sur une
campagne de dizaines d'essais.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from anyio.from_thread import BlockingPortal, start_blocking_portal
from mcp import types
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from agricam_reliable_agents.agent.loop import (
    DEFAULT_SUCCESS_CLAIM_EXTRACTOR,
    SuccessClaimExtractor,
)
from agricam_reliable_agents.connector.base import ConnectorMetadata, RawAgentReply

CONNECTOR_TYPE = "mcp"
DEFAULT_TOOL_NAME = "chat"


def default_build_arguments(prompt: str) -> dict[str, Any]:
    """Mapping requête par défaut : `{"prompt": prompt}`."""
    return {"prompt": prompt}


def _first_text(result: types.CallToolResult) -> str:
    """Premier bloc texte du résultat d'outil ; chaîne vide si aucun
    (résultat structuré pur, ou outil qui ne renvoie rien de textuel)."""
    for block in result.content:
        if isinstance(block, types.TextContent):
            return block.text
    return ""


class McpClientAdapter:
    """
    `AgentConnector` qui pilote une session MCP déjà initialisée.

    Construit normalement via `connect_stdio` (gestionnaire de contexte
    qui gère aussi l'ouverture/fermeture de la session), jamais
    directement — le constructeur suppose une session et un portail déjà
    vivants et ne les crée pas lui-même.
    """

    def __init__(
        self,
        session: ClientSession,
        portal: BlockingPortal,
        *,
        tool_name: str = DEFAULT_TOOL_NAME,
        build_arguments: Callable[[str], dict[str, Any]] = default_build_arguments,
        success_claim_extractor: SuccessClaimExtractor = DEFAULT_SUCCESS_CLAIM_EXTRACTOR,
        name: str = "mcp-agent",
    ) -> None:
        self._session = session
        self._portal = portal
        self._tool_name = tool_name
        self._build_arguments = build_arguments
        self._success_claim_extractor = success_claim_extractor
        self._name = name

    def send(self, prompt: str, trial_id: int) -> RawAgentReply:
        start = time.monotonic()
        arguments = self._build_arguments(prompt)
        try:
            result = self._portal.call(self._session.call_tool, self._tool_name, arguments)
        except Exception as exc:  # noqa: BLE001 - toute panne de transport MCP doit devenir un échec mesuré
            return RawAgentReply(
                text="", declared_success=False,
                latency_ms=(time.monotonic() - start) * 1000, error=str(exc),
            )

        text = _first_text(result)
        if result.is_error:
            return RawAgentReply(
                text=text, declared_success=False,
                latency_ms=(time.monotonic() - start) * 1000,
                error=text or "Erreur outil MCP.", raw=result,
            )
        return RawAgentReply(
            text=text, declared_success=self._success_claim_extractor(text),
            latency_ms=(time.monotonic() - start) * 1000, raw=result,
        )

    def describe(self) -> ConnectorMetadata:
        return ConnectorMetadata(
            connector_type=CONNECTOR_TYPE, name=self._name, capabilities=(self._tool_name,),
        )


@contextmanager
def connect_stdio(
    params: StdioServerParameters,
    *,
    tool_name: str = DEFAULT_TOOL_NAME,
    build_arguments: Callable[[str], dict[str, Any]] = default_build_arguments,
    success_claim_extractor: SuccessClaimExtractor = DEFAULT_SUCCESS_CLAIM_EXTRACTOR,
    name: str = "mcp-agent",
) -> Iterator[McpClientAdapter]:
    """
    Connecte un agent exposé comme serveur MCP réel via stdio
    (sous-processus) et rend un `McpClientAdapter` prêt à l'emploi.

    Ouvre un `BlockingPortal` (thread dédié + boucle asyncio), y lance le
    sous-processus (`stdio_client`) et y initialise la session
    (`ClientSession.initialize`), puis rend l'adaptateur. Ferme la session
    et le sous-processus à la sortie du bloc `with`, dans tous les cas
    (y compris en cas d'exception).

    Exemple (contre le serveur MCP AgriCam lui-même) :
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "agricam_reliable_agents.mcp_tools.server"],
            env={"PYTHONPATH": "src"},
        )
        with connect_stdio(params, tool_name="check_marketplace_stock",
                           build_arguments=lambda prompt: {"product_id": "PRD-7"}) as adapter:
            reply = adapter.send("Vérifie le stock.", trial_id=0)
    """
    with (
        start_blocking_portal() as portal,
        portal.wrap_async_context_manager(stdio_client(params)) as (read, write),
        portal.wrap_async_context_manager(ClientSession(read, write)) as session,
    ):
        portal.call(session.initialize)
        yield McpClientAdapter(
            session, portal, tool_name=tool_name, build_arguments=build_arguments,
            success_claim_extractor=success_claim_extractor, name=name,
        )
