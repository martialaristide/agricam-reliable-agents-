"""
Tâche 3 (Phase B) — adaptateur REST.

Aucun appel réseau réel : chaque test injecte un `httpx.Client` construit
sur un `httpx.MockTransport`, qui intercepte la requête au niveau du
transport avant qu'elle n'atteigne un quelconque socket.
"""

from __future__ import annotations

import json

import httpx
import pytest

from agricam_reliable_agents.connector.base import ConnectorMetadata
from agricam_reliable_agents.connector.bridge import agent_connector_as_runner
from agricam_reliable_agents.connector.rest_adapter import (
    CONNECTOR_TYPE,
    RestAdapter,
    default_request_builder,
    default_response_parser,
)
from agricam_reliable_agents.models.data_models import Task, TaskComplexity
from agricam_reliable_agents.reliability.harness import HarnessConfig, evaluate_task


def make_task() -> Task:
    return Task(
        id="T-REST", prompt="Traite le diagnostic D-42.", complexity=TaskComplexity.LEVEL_1,
        category="generic", expected_state_delta={}, verification_query="q",
    )


def client_with_handler(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------------------
# Format par défaut
# ---------------------------------------------------------------------------


def test_default_request_builder_shape():
    assert default_request_builder("Traite D-42.", 3) == {"prompt": "Traite D-42.", "trial_id": 3}


def test_default_response_parser_shape():
    assert default_response_parser({"text": "Fait.", "declared_success": True}) == ("Fait.", True)


def test_default_response_parser_missing_fields_defaults_safely():
    assert default_response_parser({}) == ("", False)


def test_default_response_parser_rejects_non_object_payload():
    with pytest.raises(TypeError, match="objet"):
        default_response_parser([1, 2, 3])


def test_send_happy_path_with_default_format():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"text": "Traitement appliqué.", "declared_success": True})

    adapter = RestAdapter("http://agent.example/chat", client=client_with_handler(handler))
    reply = adapter.send("Traite D-42.", trial_id=7)

    assert reply.text == "Traitement appliqué."
    assert reply.declared_success is True
    assert reply.error is None
    assert reply.latency_ms >= 0.0
    assert json.loads(captured[0].content) == {"prompt": "Traite D-42.", "trial_id": 7}
    assert str(captured[0].url) == "http://agent.example/chat"


# ---------------------------------------------------------------------------
# Authentification
# ---------------------------------------------------------------------------


def test_auth_header_is_actually_sent():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"text": "ok", "declared_success": False})

    adapter = RestAdapter(
        "http://agent.example/chat",
        client=httpx.Client(
            transport=httpx.MockTransport(handler), headers={"Authorization": "Bearer sk-test-123"},
        ),
    )
    adapter.send("x", 0)
    assert captured[0].headers["authorization"] == "Bearer sk-test-123"


def test_constructor_headers_are_forwarded_when_no_client_given():
    """Ne déclenche jamais de vraie requête (aucun appel à `send()`) :
    inspecte directement le client construit par défaut par `RestAdapter`
    pour vérifier que `headers=` y a bien été transmis."""
    adapter = RestAdapter("http://agent.example/chat", headers={"X-Api-Key": "abc"})
    try:
        assert adapter._client.headers["x-api-key"] == "abc"
    finally:
        adapter.close()


# ---------------------------------------------------------------------------
# Mapping requête/réponse personnalisé
# ---------------------------------------------------------------------------


def test_custom_request_builder_and_response_parser():
    def build(prompt: str, trial_id: int) -> dict:
        return {"input": {"message": prompt}, "run": trial_id}

    def parse(payload: dict) -> tuple[str, bool]:
        return payload["output"]["message"], payload["output"]["ok"]

    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"output": {"message": "Réponse tierce.", "ok": True}})

    adapter = RestAdapter(
        "http://tiers.example/api", client=client_with_handler(handler),
        request_builder=build, response_parser=parse,
    )
    reply = adapter.send("Question ?", trial_id=2)

    assert json.loads(captured[0].content) == {"input": {"message": "Question ?"}, "run": 2}
    assert reply.text == "Réponse tierce."
    assert reply.declared_success is True


# ---------------------------------------------------------------------------
# Pannes : statut HTTP, transport, JSON invalide, forme inattendue
# ---------------------------------------------------------------------------


def test_http_error_status_is_captured_not_raised():
    adapter = RestAdapter(
        "http://agent.example/chat",
        client=client_with_handler(lambda r: httpx.Response(500, text="internal error")),
    )
    reply = adapter.send("x", 0)
    assert reply.error is not None
    assert "500" in reply.error
    assert reply.declared_success is False
    assert reply.text == ""


def test_transport_connection_error_is_captured_not_raised():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connexion refusée", request=request)

    adapter = RestAdapter("http://agent.example/chat", client=client_with_handler(handler))
    reply = adapter.send("x", 0)
    assert reply.error is not None
    assert "connexion refusée" in reply.error


def test_malformed_json_body_is_captured_not_raised():
    adapter = RestAdapter(
        "http://agent.example/chat",
        client=client_with_handler(lambda r: httpx.Response(200, text="{ceci n'est pas du JSON")),
    )
    reply = adapter.send("x", 0)
    assert reply.error is not None
    assert reply.declared_success is False


def test_response_parser_value_error_is_captured_not_raised():
    adapter = RestAdapter(
        "http://agent.example/chat",
        client=client_with_handler(lambda r: httpx.Response(200, json=[1, 2, 3])),
    )
    reply = adapter.send("x", 0)
    assert reply.error is not None
    assert "invalide" in reply.error.lower() or "objet" in reply.error.lower()


# ---------------------------------------------------------------------------
# describe() et cycle de vie du client
# ---------------------------------------------------------------------------


def test_describe_reports_connector_type_and_name():
    adapter = RestAdapter(
        "http://agent.example/chat",
        client=client_with_handler(lambda r: httpx.Response(200, json={})),
        name="agent-tiers-v2",
    )
    meta = adapter.describe()
    assert isinstance(meta, ConnectorMetadata)
    assert meta.connector_type == CONNECTOR_TYPE == "rest"
    assert meta.name == "agent-tiers-v2"
    assert "http" in meta.capabilities


def test_close_closes_owned_client_but_not_injected_one():
    injected = client_with_handler(lambda r: httpx.Response(200, json={}))
    adapter = RestAdapter("http://x", client=injected)
    adapter.close()
    assert not injected.is_closed  # client fourni par l'appelant : jamais fermé par le connecteur

    owned_adapter = RestAdapter("http://x")
    with owned_adapter as a:
        pass
    assert a is owned_adapter
    assert owned_adapter._client.is_closed  # client créé par défaut : fermé par le context manager


# ---------------------------------------------------------------------------
# Intégration bout en bout : REST + harnais EXISTANT (non modifié)
# ---------------------------------------------------------------------------


def test_full_campaign_through_the_existing_harness_with_a_rest_connected_agent():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"text": "Traitement effectué.", "declared_success": True})

    adapter = RestAdapter("http://agent.example/chat", client=client_with_handler(handler))
    runner = agent_connector_as_runner(adapter)

    report = evaluate_task(
        make_task(), runner, state_snapshot_fn=lambda t: {},
        config=HarnessConfig(n_trials=4, k_values=(1,)),
    )

    assert report.n_trials == 4
    assert report.n_success == 4
    assert report.p_hat == 1.0
