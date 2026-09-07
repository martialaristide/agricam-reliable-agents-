"""
Tests de l'API JSON de l'interface de vérification (Starlette TestClient).

La base est un fichier SQLite peuplé par deux campagnes simulées : une
parfaite (p = 1) et une dégradée (p = 0,5, avec des dérives hors périmètre
qui produisent des incidents bloqués).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from agricam_reliable_agents.api.app import (
    CampaignLauncher,
    _compare_verdict,
    _diff_rows,
    create_app,
)
from agricam_reliable_agents.reliability.repository import ReliabilityRepository
from agricam_reliable_agents.reliability.simulation import run_simulated_campaign


@pytest.fixture(scope="module")
def db_url(tmp_path_factory: pytest.TempPathFactory) -> str:
    url = f"sqlite:///{(tmp_path_factory.mktemp('api') / 'results.db').as_posix()}"
    repo = ReliabilityRepository.from_url(url)
    run_simulated_campaign(repo, name="parfaite", n_trials=4, p_step=1.0, seed=1)
    run_simulated_campaign(repo, name="dégradée", n_trials=6, p_step=0.5, seed=7)
    repo.engine.dispose()
    return url


@pytest.fixture(scope="module")
def client(db_url: str) -> TestClient:
    return TestClient(create_app(db_url, background_runs=False))


# ---- Lecture ---------------------------------------------------------------

def test_index_serves_the_interface(client: TestClient):
    response = client.get("/")
    assert response.status_code == 200
    assert "Interface de vérification" in response.text or "Vérification" in response.text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/styles.css").status_code == 200


def test_list_campaigns_with_summaries(client: TestClient):
    data = client.get("/api/campaigns").json()
    assert [c["name"] for c in data["campaigns"]] == ["dégradée", "parfaite"]  # plus récente en premier
    parfaite = data["campaigns"][1]
    assert parfaite["status"] == "terminée"
    assert (parfaite["n_tasks"], parfaite["n_trials"], parfaite["n_success"]) == (3, 12, 12)
    assert parfaite["proportion"]["p_hat"] == 1.0
    assert parfaite["proportion"]["ci_low"] < 1.0 <= parfaite["proportion"]["ci_high"]
    assert parfaite["n_overconfident"] == 0
    degradee = data["campaigns"][0]
    assert degradee["n_overconfident"] > 0
    assert degradee["incidents"]["blocked"] == degradee["incidents"]["total"]
    assert degradee["incidents"]["unblocked"] == 0
    assert degradee["total_cost_usd"] > 0


def test_get_campaign_lists_tasks_sorted_by_complexity(client: TestClient):
    data = client.get("/api/campaigns/1").json()
    assert data["campaign"]["name"] == "parfaite"
    assert [t["task_id"] for t in data["tasks"]] == ["T1-treat", "T2-treat-notify", "T3-full-workflow"]
    assert [t["complexity_label"] for t in data["tasks"]] == ["1 à 2 étapes", "3 à 5 étapes", "6 étapes ou plus"]
    t3 = data["tasks"][2]
    assert t3["proportion"]["n"] == 4 and t3["n_verified"] == 4
    assert t3["pass_k"]["10"] == 1.0
    assert "diagnostic.D-42.status" in t3["expected_state_delta"]


def test_get_task_returns_curve_and_trials(client: TestClient):
    data = client.get("/api/campaigns/2/tasks/T3-full-workflow?k_max=5").json()
    assert [p["k"] for p in data["pass_k_curve"]] == [1, 2, 3, 4, 5]
    p_hat = data["task"]["proportion"]["p_hat"]
    assert data["pass_k_curve"][4]["value"] == pytest.approx(p_hat**5)
    point = data["pass_k_curve"][4]
    assert point["low"] <= point["value"] <= point["high"]
    assert point["low"] == pytest.approx(data["task"]["proportion"]["ci_low"] ** 5)
    assert len(data["trials"]) == 6
    states = {t["state"] for t in data["trials"]}
    assert states <= {"verifie", "echec", "surconfiance"}
    assert any(t["state"] == "surconfiance" for t in data["trials"])
    for t in data["trials"]:
        assert t["state_label"] in {"vérifié", "échec avoué", "sur-confiance"}
        assert t["verified_success"] == (t["state"] == "verifie")


def test_get_task_clamps_k_max_and_rejects_garbage(client: TestClient):
    assert len(client.get("/api/campaigns/1/tasks/T1-treat?k_max=999").json()["pass_k_curve"]) == 50
    assert len(client.get("/api/campaigns/1/tasks/T1-treat?k_max=0").json()["pass_k_curve"]) == 1
    assert client.get("/api/campaigns/1/tasks/T1-treat?k_max=abc").status_code == 400


def test_get_trial_detail_with_diff_and_verdict(client: TestClient):
    listing = client.get("/api/campaigns/2/tasks/T2-treat-notify").json()
    over = next(t for t in listing["trials"] if t["state"] == "surconfiance")
    data = client.get(f"/api/campaigns/2/trials/T2-treat-notify/{over['trial_id']}").json()
    assert data["verdict"]["label"] == "sur-confiance"
    assert data["verdict"]["declared_success"] is True and data["verdict"]["matches_expected"] is False
    assert data["trial"]["final_answer"].startswith("Traitement appliqué")
    assert {d["status"] for d in data["diff"]} & {"manquant", "different"}
    assert all(isinstance(c["arguments"], dict) for c in data["trial"]["tool_calls"])
    assert {c["status"] for c in data["trial"]["tool_calls"]} <= {"ok", "error", "refused"}
    assert data["trial"]["n_tool_calls_failed"] >= 1

    ok = client.get("/api/campaigns/1/trials/T2-treat-notify/0").json()
    assert ok["verdict"]["state"] == "verifie"
    assert all(d["status"] == "ok" for d in ok["diff"])
    assert [c["tool_name"] for c in ok["trial"]["tool_calls"]] == ["get_sensor_data", "recommend_treatment", "notify_farmer"]


def test_incidents_unblocked_first_and_labelled(client: TestClient):
    data = client.get("/api/campaigns/2/incidents").json()
    assert data["total"] == len(data["incidents"]) > 0
    assert data["unblocked"] == 0
    assert all(i["category_label"] == "Action hors périmètre (LLM08)" for i in data["incidents"])
    assert all(i["task_id"] in {"T2-treat-notify", "T3-full-workflow"} for i in data["incidents"])
    assert client.get("/api/campaigns/1/incidents").json()["total"] == 0


def test_costs_by_task_and_over_time(client: TestClient):
    data = client.get("/api/campaigns/1/costs").json()
    assert data["n_trials"] == 12
    assert {t["task_id"] for t in data["by_task"]} == {"T1-treat", "T2-treat-notify", "T3-full-workflow"}
    assert len(data["over_time"]) == 12
    assert [p["index"] for p in data["over_time"]] == list(range(12))
    assert data["total_cost_usd"] == pytest.approx(sum(p["cost_usd"] for p in data["over_time"]))
    by = {t["task_id"]: t for t in data["by_task"]}
    assert by["T3-full-workflow"]["mean_cost_usd"] > by["T1-treat"]["mean_cost_usd"]


def test_compare_flags_regressions(client: TestClient):
    data = client.get("/api/compare?before=1&after=2").json()
    assert data["before"]["name"] == "parfaite" and data["after"]["name"] == "dégradée"
    verdicts = {t["task_id"]: t["verdict"] for t in data["tasks"]}
    assert verdicts["T3-full-workflow"] == "regression"
    assert data["n_regressions"] >= 1
    assert data["tasks"][0]["verdict"] == "regression"  # régressions en tête
    assert all(t["delta"] is not None for t in data["tasks"])

    reverse = client.get("/api/compare?before=2&after=1").json()
    assert reverse["n_regressions"] == 0
    assert {t["verdict"] for t in reverse["tasks"]} <= {"progres", "stable"}


def test_not_found_and_bad_requests_are_plain_sentences(client: TestClient):
    assert client.get("/api/campaigns/999").json() == {"error": "Cette campagne n'existe pas."}
    assert client.get("/api/campaigns/999").status_code == 404
    assert client.get("/api/campaigns/1/tasks/T-NOPE").status_code == 404
    assert client.get("/api/campaigns/1/trials/T1-treat/99").status_code == 404
    assert client.get("/api/campaigns/abc").status_code == 400
    assert client.get("/api/compare?before=1").status_code == 400
    assert client.get("/api/compare?before=1&after=999").status_code == 404


# ---- Lancement d'une campagne depuis l'interface -----------------------------

def test_create_campaign_validation(client: TestClient):
    assert client.post("/api/campaigns", content=b"not json", headers={"Content-Type": "application/json"}).status_code == 400
    assert client.post("/api/campaigns", json=[1, 2]).status_code == 400
    assert client.post("/api/campaigns", json={"n_trials": 0}).status_code == 400
    assert client.post("/api/campaigns", json={"n_trials": True}).status_code == 400
    assert client.post("/api/campaigns", json={"p_step": 1.5}).status_code == 400
    assert client.post("/api/campaigns", json={"seed": "x"}).status_code == 400
    assert client.post("/api/campaigns", json={"name": "   "}).status_code == 400
    assert "essais" in client.post("/api/campaigns", json={"n_trials": 0}).json()["error"]


def test_create_campaign_runs_synchronously_and_appears_in_listing(tmp_path: Path):
    url = f"sqlite:///{(tmp_path / 'launch.db').as_posix()}"
    local = TestClient(create_app(url, background_runs=False))
    response = local.post("/api/campaigns", json={"name": "depuis l'interface", "n_trials": 2, "p_step": 1.0, "seed": 3})
    assert response.status_code == 202
    created = response.json()
    assert created["status"] == "terminée"
    listing = local.get("/api/campaigns").json()["campaigns"]
    assert listing[0]["id"] == created["id"]
    assert listing[0]["status"] == "terminée"
    assert listing[0]["n_trials"] == 6
    assert listing[0]["model"] == "simulated-p1.0"


def test_create_campaign_in_background_thread(tmp_path: Path):
    url = f"sqlite:///{(tmp_path / 'bg.db').as_posix()}"
    app = create_app(url, background_runs=True)
    local = TestClient(app)
    created = local.post("/api/campaigns", json={"n_trials": 1, "p_step": 0.5}).json()
    launcher: CampaignLauncher = app.state.launcher
    launcher.wait(created["id"], timeout=60)
    assert launcher.status_of(created["id"]).status == "terminée"
    campaign = local.get(f"/api/campaigns/{created['id']}").json()
    assert campaign["campaign"]["status"] == "terminée"
    assert len(campaign["tasks"]) == 3


def test_launcher_records_failures(tmp_path: Path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'fail.db').as_posix()}"
    repo = ReliabilityRepository.from_url(url)
    launcher = CampaignLauncher(url, background=False)
    from agricam_reliable_agents.api import app as app_module

    def boom(*args, **kwargs):
        raise RuntimeError("panne simulée")

    monkeypatch.setattr(app_module, "run_simulated_campaign", boom)
    campaign_id = launcher.launch(repo, name="x", n_trials=1, p_step=1.0, seed=0)
    status = launcher.status_of(campaign_id)
    assert status.status == "échouée" and "panne simulée" in status.error
    assert launcher.status_of(999) is None
    launcher.wait(999)  # aucun thread : ne lève pas
    repo.engine.dispose()


def test_memory_database_forces_synchronous_runs():
    launcher = CampaignLauncher("sqlite://", background=True)
    assert launcher._background is False


# ---- Fonctions pures ------------------------------------------------------------

def test_diff_rows_statuses():
    rows = _diff_rows({"a": 1, "b": 2, "c": 3}, {"a": 1, "b": 5, "d": 4})
    assert {r["key"]: r["status"] for r in rows} == {"a": "ok", "b": "different", "c": "manquant", "d": "inattendu"}


def test_compare_verdicts():
    before = {"p_hat": 0.9, "ci_low": 0.7, "ci_high": 0.98}
    assert _compare_verdict(before, {"p_hat": 0.5, "ci_low": 0.3, "ci_high": 0.7}) == "regression"
    assert _compare_verdict(before, {"p_hat": 0.85, "ci_low": 0.65, "ci_high": 0.95}) == "baisse"
    assert _compare_verdict(before, {"p_hat": 0.9, "ci_low": 0.7, "ci_high": 0.98}) == "stable"
    assert _compare_verdict({"p_hat": 0.5, "ci_low": 0.3, "ci_high": 0.7}, {"p_hat": 0.95, "ci_low": 0.8, "ci_high": 0.99}) == "progres"
    assert _compare_verdict(None, before) == "incomparable"
