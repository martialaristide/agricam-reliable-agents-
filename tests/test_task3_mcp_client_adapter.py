"""
Tâche 3 (Phase B) — adaptateur client MCP.

Deux niveaux, comme `tests/test_mcp_server.py` :
1. En mémoire (`_connect_in_memory`) : le connecteur pilote le VRAI serveur
   MCP AgriCam (`mcp_tools.server.build_server`), relié par des flux
   mémoire — rapide, déterministe, exerce le vrai protocole MCP.
2. En sous-processus (`connect_stdio`) : le vrai point d'entrée
   `python -m ...mcp_tools.server` est lancé sur stdio, comme en
   production — preuve que l'adaptateur fonctionne contre le serveur
   MCP réel du projet, pas seulement une simulation en mémoire.

Aucun test n'a besoin d'un agent MCP tiers réel : les deux niveaux
utilisent le serveur AgriCam déjà présent dans le dépôt, conformément à
l'autorisation explicite de la Tâche 3.2 du prompt de référence.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from anyio.from_thread import start_blocking_portal
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters
from mcp.shared.memory import create_client_server_memory_streams

from agricam_reliable_agents.connector.base import ConnectorMetadata
from agricam_reliable_agents.connector.bridge import agent_connector_as_runner
from agricam_reliable_agents.connector.mcp_client_adapter import (
    CONNECTOR_TYPE,
    McpClientAdapter,
    connect_stdio,
    default_build_arguments,
)
from agricam_reliable_agents.mcp_tools.server import build_server, default_tools
from agricam_reliable_agents.models.data_models import Task, TaskComplexity
from agricam_reliable_agents.reliability.harness import HarnessConfig, evaluate_task

SRC_DIR = str(Path(__file__).resolve().parents[1] / "src")


@contextmanager
def _connect_in_memory(**adapter_kwargs) -> Iterator[McpClientAdapter]:
    """Comme `connect_stdio`, mais contre le vrai serveur AgriCam relié en
    mémoire (pas de sous-processus) — même construction que `_InMemoryMCP`
    de `test_mcp_server.py`, pilotée depuis un thread synchrone via un
    `BlockingPortal` plutôt que depuis un test `async def`, pour pouvoir
    appeler `adapter.send()` (synchrone) sans jamais bloquer la boucle
    qu'il sollicite depuis le même thread.

    `portal.wrap_async_context_manager` (pas un couple `__aenter__`
    manuel/`__aexit__` séparé en deux `portal.call()`) est essentiel ici :
    un gestionnaire de contexte async qui ouvre en interne un groupe de
    tâches (`ClientSession`, très probablement) lie son étendue
    d'annulation à la tâche qui l'a ouvert. Deux `portal.call()` distincts
    exécutent chacun une tâche anyio DIFFÉRENTE (`start_task_soon(...).
    result()` en interne) : entrer dans l'un et sortir dans l'autre viole
    la règle d'appariement tâche/étendue d'anyio (`RuntimeError: Attempted
    to exit a cancel scope that isn't the current task's current cancel
    scope`) — `wrap_async_context_manager` garde une unique tâche vivante,
    en coulisses, entre l'entrée et la sortie synchrones qu'il expose."""
    tools = default_tools()

    with start_blocking_portal() as portal, portal.wrap_async_context_manager(
        create_client_server_memory_streams()
    ) as (client_streams, server_streams):
        server = build_server(tools)
        server_future = portal.start_task_soon(
            server.run, server_streams[0], server_streams[1],
            server.create_initialization_options(),
        )
        with portal.wrap_async_context_manager(
            ClientSession(client_streams[0], client_streams[1])
        ) as session:
            portal.call(session.initialize)
            try:
                yield McpClientAdapter(session, portal, **adapter_kwargs)
            finally:
                server_future.cancel()


def make_task() -> Task:
    return Task(
        id="T-MCP", prompt="Vérifie le stock du produit PRD-7.", complexity=TaskComplexity.LEVEL_1,
        category="generic", expected_state_delta={}, verification_query="q",
    )


# ---------------------------------------------------------------------------
# Fonctions pures
# ---------------------------------------------------------------------------


def test_default_build_arguments_shape():
    assert default_build_arguments("Vérifie le stock.") == {"prompt": "Vérifie le stock."}


# ---------------------------------------------------------------------------
# En mémoire, contre le vrai serveur MCP AgriCam
# ---------------------------------------------------------------------------


def test_send_calls_a_real_tool_on_the_real_agricam_server_and_gets_structured_reply():
    """`check_marketplace_stock` n'est pas un outil « conversationnel »,
    mais c'est un VRAI outil du VRAI serveur AgriCam : la requête/réponse
    JSON qu'il renvoie est exactement celle de `mcp_tools/tools.py`,
    preuve que le protocole MCP est parlé pour de vrai, pas simulé."""
    with _connect_in_memory(
        tool_name="check_marketplace_stock",
        build_arguments=lambda prompt: {"product_id": "PRD-7"},
        success_claim_extractor=lambda text: '"stock_qty"' in text,
    ) as adapter:
        reply = adapter.send("Vérifie le stock du produit PRD-7.", trial_id=0)

    assert reply.error is None
    assert '"product_id": "PRD-7"' in reply.text
    assert '"stock_qty": 25' in reply.text
    assert reply.declared_success is True
    assert reply.latency_ms >= 0.0
    assert reply.raw is not None


def test_send_reports_mcp_tool_error_without_raising():
    """`get_sensor_data` sur une parcelle inconnue est une VRAIE erreur
    métier renvoyée par le serveur (`is_error=True`), pas une panne de
    transport — l'adaptateur doit la traduire en `RawAgentReply.error`
    sans jamais laisser une exception se propager."""
    with _connect_in_memory(
        tool_name="get_sensor_data", build_arguments=lambda prompt: {"parcel_id": "P-INCONNUE"},
    ) as adapter:
        reply = adapter.send("Lis les capteurs de P-INCONNUE.", trial_id=0)

    assert reply.declared_success is False
    assert reply.error is not None
    assert "Erreur outil" in reply.error


def test_send_reports_unknown_tool_error_without_raising():
    with _connect_in_memory(tool_name="outil_qui_nexiste_pas") as adapter:
        reply = adapter.send("x", trial_id=0)
    assert reply.error is not None
    assert reply.declared_success is False


def test_describe_reports_connector_type_and_tool_name():
    with _connect_in_memory(tool_name="check_marketplace_stock", name="agricam-via-mcp") as adapter:
        meta = adapter.describe()
    assert isinstance(meta, ConnectorMetadata)
    assert meta.connector_type == CONNECTOR_TYPE == "mcp"
    assert meta.name == "agricam-via-mcp"
    assert meta.capabilities == ("check_marketplace_stock",)


def test_send_after_session_closed_reports_transport_error_without_raising():
    """Ferme la session AVANT d'appeler `send()` : simule une vraie panne
    de transport (pas une erreur métier de l'outil) — doit être capturée
    comme les autres, jamais laissée se propager."""
    with _connect_in_memory(tool_name="check_marketplace_stock",
                            build_arguments=lambda prompt: {"product_id": "PRD-7"}) as adapter:
        pass  # la session est fermée ici, à la sortie du bloc `with`

    reply = adapter.send("x", trial_id=0)
    assert reply.error is not None
    assert reply.declared_success is False


def test_first_text_returns_empty_string_when_no_text_block():
    """`get_diagnostic_history` sur une parcelle sans historique renvoie un
    résultat structuré dont le texte JSON est `{"diagnostics": []}` — un
    TextContent existe donc toujours ici (le serveur en émet un
    systématiquement, cf. `mcp_tools/server.py::_text_result`) ; ce test
    vérifie plutôt que ce texte se lit correctement même vide de contenu
    utile, verrouillant le comportement de `_first_text` sur un cas réel
    plutôt que sur un résultat MCP construit à la main (hors du protocole)."""
    with _connect_in_memory(
        tool_name="get_diagnostic_history", build_arguments=lambda prompt: {"parcel_id": "P-404"},
    ) as adapter:
        reply = adapter.send("x", trial_id=0)
    assert reply.error is None
    assert reply.text == '{"diagnostics": []}'


def test_send_uses_custom_success_claim_extractor_on_the_tool_text():
    with _connect_in_memory(
        tool_name="check_marketplace_stock",
        build_arguments=lambda prompt: {"product_id": "PRD-7"},
        success_claim_extractor=lambda text: False,  # jamais un succès, quel que soit le texte
    ) as adapter:
        reply = adapter.send("x", trial_id=0)
    assert reply.error is None  # l'appel a réussi côté outil...
    assert reply.declared_success is False  # ...mais l'extracteur personnalisé dit non


# ---------------------------------------------------------------------------
# Intégration bout en bout : MCP + harnais EXISTANT (non modifié)
# ---------------------------------------------------------------------------


def test_full_campaign_through_the_existing_harness_with_an_mcp_connected_agent():
    with _connect_in_memory(
        tool_name="check_marketplace_stock",
        build_arguments=lambda prompt: {"product_id": "PRD-7"},
        success_claim_extractor=lambda text: '"stock_qty"' in text,
    ) as adapter:
        runner = agent_connector_as_runner(adapter)
        report = evaluate_task(
            make_task(), runner, state_snapshot_fn=lambda t: {},
            config=HarnessConfig(n_trials=3, k_values=(1,)),
        )

    assert report.n_trials == 3
    assert report.n_success == 3
    assert report.p_hat == 1.0


# ---------------------------------------------------------------------------
# En sous-processus, contre le vrai point d'entrée `python -m ...server`
# ---------------------------------------------------------------------------


def test_connect_stdio_against_the_real_server_subprocess():
    """Lance le vrai module serveur en sous-processus (transport stdio
    réel), exactement comme le fait déjà `test_mcp_server.py` pour le
    serveur seul — ici c'est `connect_stdio` (production) qui pilote la
    connexion, pas un client MCP construit à la main dans le test."""
    import os

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "agricam_reliable_agents.mcp_tools.server"],
        env={**os.environ, "PYTHONPATH": SRC_DIR, "PYTHONIOENCODING": "utf-8"},
    )
    with connect_stdio(
        params, tool_name="check_marketplace_stock",
        build_arguments=lambda prompt: {"product_id": "PRD-7"},
        success_claim_extractor=lambda text: '"stock_qty"' in text,
    ) as adapter:
        reply = adapter.send("Vérifie le stock.", trial_id=0)

    assert reply.error is None
    assert '"stock_qty": 25' in reply.text
    assert reply.declared_success is True


def test_connect_stdio_cleans_up_the_subprocess_on_exit():
    """Après la sortie du bloc `with`, le sous-processus est terminé —
    vérifié indirectement : un second `connect_stdio` peut se reconnecter
    sans conflit (preuve qu'aucune ressource stdio n'est restée ouverte)."""
    import os

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "agricam_reliable_agents.mcp_tools.server"],
        env={**os.environ, "PYTHONPATH": SRC_DIR, "PYTHONIOENCODING": "utf-8"},
    )
    with connect_stdio(params, tool_name="check_marketplace_stock",
                       build_arguments=lambda prompt: {"product_id": "PRD-7"}) as adapter:
        adapter.send("x", 0)

    with connect_stdio(params, tool_name="check_marketplace_stock",
                       build_arguments=lambda prompt: {"product_id": "PRD-7"}) as adapter:
        reply = adapter.send("x", 0)
    assert reply.error is None
