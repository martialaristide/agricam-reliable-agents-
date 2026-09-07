"""
Tests de l'écran « Connecter un agent » côté API (section 5 du document de
référence) : connexions d'agent et oracles de tâche, au-dessus de
`connector/oracle_repository.py` (non modifié) et de
`connector/contract_inference.py` (non modifié).

Portée assumée pour cette interface (à ne pas dépasser sans le dire) :
l'API expose la définition d'une connexion, la définition d'une tâche,
l'assistance d'inférence de contrat et l'approbation humaine explicite
d'un oracle — mais ne lance PAS de campagne réelle contre un connecteur
tiers depuis l'interface web (ce qui exigerait d'exposer de vrais appels
réseau/sous-processus au serveur HTTP, hors périmètre raisonnable de
cette passe). Les campagnes affichées ailleurs dans l'interface restent
celles lancées par `CampaignLauncher` (agent simulé), inchangé.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from agricam_reliable_agents.api.app import create_app
from agricam_reliable_agents.connector.oracle_repository import OracleRepository


@pytest.fixture()
def db_url(tmp_path: Path) -> str:
    return f"sqlite:///{(tmp_path / 'connector.db').as_posix()}"


@pytest.fixture()
def client(db_url: str) -> TestClient:
    return TestClient(create_app(db_url, background_runs=False))


# ---- Connexions d'agent -----------------------------------------------------

def test_list_connections_starts_empty(client: TestClient):
    assert client.get("/api/connections").json() == {"connections": []}


def test_create_and_list_connection(client: TestClient):
    body = {
        "id": "conn-rest-1",
        "connector_type": "rest",
        "environment": "test",
        "config": {"endpoint": "https://mon-agent.example/chat"},
        "credential_ref": "vault://agricam/mon-agent",
    }
    response = client.post("/api/connections", json=body)
    assert response.status_code == 201
    created = response.json()
    assert created["id"] == "conn-rest-1"
    assert created["credential_ref"] == "vault://agricam/mon-agent"

    listing = client.get("/api/connections").json()["connections"]
    assert [c["id"] for c in listing] == ["conn-rest-1"]
    assert listing[0]["connector_type"] == "rest"
    assert listing[0]["environment"] == "test"
    assert listing[0]["config"] == {"endpoint": "https://mon-agent.example/chat"}


def test_create_connection_rejects_raw_secret_credential(client: TestClient):
    body = {
        "id": "conn-bad",
        "connector_type": "rest",
        "environment": "test",
        "config": {},
        "credential_ref": "sk-abcdefghijklmnopqrstuvwx",
    }
    response = client.post("/api/connections", json=body)
    assert response.status_code == 400
    assert "secret" in response.json()["error"]
    assert client.get("/api/connections").json()["connections"] == []


@pytest.mark.parametrize("bad_body", [
    {},
    {"id": ""},
    {"id": "x", "connector_type": "carrier-pigeon", "environment": "test", "config": {}},
    {"id": "x", "connector_type": "rest", "environment": "moon", "config": {}},
    {"id": "x", "connector_type": "rest", "environment": "test", "config": "not-a-dict"},
])
def test_create_connection_validation(client: TestClient, bad_body: dict):
    assert client.post("/api/connections", json=bad_body).status_code == 400


def test_create_connection_replaces_existing_id(client: TestClient):
    base = {"id": "conn-1", "connector_type": "cli", "environment": "test", "config": {"a": 1}}
    client.post("/api/connections", json=base)
    updated = {**base, "config": {"a": 2}}
    client.post("/api/connections", json=updated)
    listing = client.get("/api/connections").json()["connections"]
    assert len(listing) == 1
    assert listing[0]["config"] == {"a": 2}


# ---- Tâches -------------------------------------------------------------------

def make_task_body(task_id: str = "T-EXT-1") -> dict:
    return {
        "id": task_id,
        "prompt": "Traite le diagnostic du champ nord.",
        "complexity": "1-2_steps",
        "category": "externe",
        "expected_state_delta": {"diagnostic.D-1.status": "treated"},
        "verification_query": "q-ext-1",
    }


def test_create_task_and_list_with_no_oracle_yet(client: TestClient):
    response = client.post("/api/tasks", json=make_task_body())
    assert response.status_code == 201
    assert response.json()["id"] == "T-EXT-1"

    listing = client.get("/api/tasks").json()["tasks"]
    assert len(listing) == 1
    assert listing[0]["task_id"] == "T-EXT-1"
    assert listing[0]["complexity_label"] == "1 à 2 étapes"
    assert listing[0]["oracle"] is None


@pytest.mark.parametrize("bad_body", [
    {},
    {"id": ""},
    {"id": "x", "prompt": "p", "complexity": "bogus", "category": "c",
     "expected_state_delta": {}, "verification_query": "q"},
    {"id": "x", "prompt": "p", "complexity": "1-2_steps", "category": "c",
     "expected_state_delta": "not-a-dict", "verification_query": "q"},
])
def test_create_task_validation(client: TestClient, bad_body: dict):
    assert client.post("/api/tasks", json=bad_body).status_code == 400


# ---- Inférence de contrat (assistance, jamais une validation) -----------------

def test_infer_oracle_endpoint_returns_candidate_or_none(client: TestClient):
    schema_ok = {"type": "object", "properties": {"treatment_status": {"type": "string"}}}
    response = client.post("/api/oracles/infer", json={"tool_schema": schema_ok})
    assert response.status_code == 200
    candidate = response.json()["candidate"]
    assert candidate is not None
    assert candidate["suggested_fields"] == ["treatment_status"]
    assert "treatment_status" in candidate["rationale"]

    schema_ambiguous = {"type": "object", "properties": {"result": {"type": "string"}}}
    response2 = client.post("/api/oracles/infer", json={"tool_schema": schema_ambiguous})
    assert response2.status_code == 200
    assert response2.json()["candidate"] is None


def test_infer_oracle_endpoint_never_persists_anything(client: TestClient):
    client.post("/api/tasks", json=make_task_body())
    schema_ok = {"type": "object", "properties": {"status": {"type": "string"}}}
    client.post("/api/oracles/infer", json={"tool_schema": schema_ok})
    listing = client.get("/api/tasks").json()["tasks"]
    assert listing[0]["oracle"] is None  # l'inférence seule ne crée aucun oracle


# ---- Approbation humaine explicite d'un oracle ---------------------------------

def test_approve_oracle_requires_existing_task(client: TestClient):
    response = client.post(
        "/api/oracles/T-NOPE/approve",
        json={"approved_fields": ["diagnostic.D-1.status"]},
    )
    assert response.status_code == 404


def test_approve_oracle_marks_validated_by_human(client: TestClient):
    client.post("/api/tasks", json=make_task_body())
    response = client.post(
        "/api/oracles/T-EXT-1/approve",
        json={"approved_fields": ["diagnostic.D-1.status"], "inferred": True},
    )
    assert response.status_code == 201
    oracle = response.json()
    assert oracle["validated_by_human"] is True
    assert oracle["validated_at"] is not None
    assert oracle["approved_fields"] == ["diagnostic.D-1.status"]
    assert oracle["inferred"] is True

    listing = client.get("/api/tasks").json()["tasks"]
    assert listing[0]["oracle"]["validated_by_human"] is True
    assert listing[0]["oracle"]["approved_fields"] == ["diagnostic.D-1.status"]


@pytest.mark.parametrize("bad_body", [
    {},
    {"approved_fields": []},
    {"approved_fields": "not-a-list"},
    {"approved_fields": ["ok"], "unexpected_field_policy": "shrug"},
])
def test_approve_oracle_validation(client: TestClient, bad_body: dict):
    client.post("/api/tasks", json=make_task_body())
    assert client.post("/api/oracles/T-EXT-1/approve", json=bad_body).status_code == 400


def test_oracle_repository_shares_engine_with_reliability_repository(db_url: str):
    """La table task_oracles doit vivre dans la MÊME base que campaigns/tasks
    (Tâche 0 : FK réelle task_oracles.task_id -> tasks.id), pas dans un
    fichier séparé — sinon la FK ne pourrait pas s'appliquer."""
    app = create_app(db_url, background_runs=False)
    local = TestClient(app)
    local.post("/api/tasks", json=make_task_body())
    local.post("/api/oracles/T-EXT-1/approve", json={"approved_fields": ["a"]})
    direct = OracleRepository(app.state.repository.engine)
    assert direct.get_oracle("T-EXT-1") is not None
