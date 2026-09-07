"""
Test d'intégration du script `scripts/run_campaign.py` (LLM simulé) :
vraie boucle agent + vrais outils sur store SQL + garde + harnais + dépôt.
"""

from __future__ import annotations

import importlib.util
import random
from pathlib import Path

import pytest

from agricam_reliable_agents.agent.llm_client import LLMResponse, LLMToolCallRequest
from agricam_reliable_agents.models.data_models import AttackCategory
from agricam_reliable_agents.reliability.repository import ReliabilityRepository

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_campaign.py"


@pytest.fixture(scope="module")
def script():
    spec = importlib.util.spec_from_file_location("run_campaign", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_simulated_llm_follows_plan_then_concludes(script):
    llm = script.SimulatedLLMClient(p_step=1.0, rng=random.Random(0))
    task = next(t for t in script.TASKS if t.id == "T2-treat-notify")
    llm.start_trial(task)
    names = []
    for _ in range(3):
        response = llm.generate([], [])
        assert response.tool_call is not None
        names.append(response.tool_call.name)
    final = llm.generate([], [])
    assert names == ["get_sensor_data", "recommend_treatment", "notify_farmer"]
    assert final.tool_call is None and final.final_text == script.FINAL_CLAIM


def test_simulated_llm_derails_then_still_claims_success(script):
    llm = script.SimulatedLLMClient(p_step=0.0, rng=random.Random(0))
    llm.start_trial(script.TASKS[0])
    first = llm.generate([], [])
    assert first.tool_call.arguments == {"diagnostic_id": "D-404"}
    assert llm.generate([], []).final_text == script.FINAL_CLAIM


def test_simulated_llm_rejects_invalid_probability(script):
    with pytest.raises(ValueError):
        script.SimulatedLLMClient(p_step=1.5, rng=random.Random(0))


def test_perfect_simulated_campaign_is_fully_reliable(script, tmp_path: Path, capsys):
    db_url = f"sqlite:///{(tmp_path / 'results.db').as_posix()}"
    script.main(["--n-trials", "3", "--p-step", "1.0", "--database-url", db_url, "--name", "perfect"])

    out = capsys.readouterr().out
    assert "Campagne #1" in out
    assert "Incidents de sécurité journalisés : 0" in out

    repo = ReliabilityRepository.from_url(db_url)
    reports = repo.list_reports(1)
    assert [r.task_id for r in reports] == ["T1-treat", "T2-treat-notify", "T3-full-workflow"]
    assert all(r.p_hat == 1.0 for r in reports)
    trials = repo.list_trials(1, "T3-full-workflow")
    assert len(trials) == 3
    assert all(t.verified_success for t in trials)
    assert trials[0].tool_call_names == (
        "get_sensor_data", "get_diagnostic_history", "check_marketplace_stock",
        "recommend_treatment", "notify_farmer", "check_marketplace_stock",
    )
    [campaign] = repo.list_campaigns()
    assert (campaign.name, campaign.model) == ("perfect", "simulated-p1.0")
    assert {t.id for t in repo.list_tasks()} == {"T1-treat", "T2-treat-notify", "T3-full-workflow"}
    repo.engine.dispose()


def test_always_failing_campaign_is_caught_by_verifier_and_guard(script, tmp_path: Path):
    db_url = f"sqlite:///{(tmp_path / 'results.db').as_posix()}"
    args = script.parse_args(["--n-trials", "2", "--p-step", "0.0", "--database-url", db_url])
    assert args.name == "simulated-p0.0-n2"
    campaign_id, reports, repo = script.run_campaign(args)

    assert all(r.p_hat == 0.0 for r in reports)
    trials = repo.list_trials(campaign_id)
    assert len(trials) == 6
    # L'agent déclare toujours réussir : chaque échec réel est une sur-confiance.
    assert all(t.declared_success and t.overconfidence_detected for t in trials)
    assert all(t.actual_state_delta == {} for t in trials)
    # T1 se trompe d'identifiant dès la première étape : l'appel est archivé en erreur, aucun réussi.
    t1 = [t for t in trials if t.task_id == "T1-treat"]
    assert all(t.successful_tool_call_names == () for t in t1)
    assert all([c.status for c in t.tool_calls] == ["error"] for t in t1)
    assert repo.list_incidents(campaign_id) == []  # jamais atteint notify_farmer
    repo.engine.dispose()


def test_out_of_scope_notification_is_blocked_and_recorded(script, tmp_path: Path, monkeypatch):
    """Quand la dérive touche `notify_farmer` (F-404), la garde bloque
    l'appel hors périmètre et l'incident LLM08 est archivé dans la campagne."""
    db_url = f"sqlite:///{(tmp_path / 'results.db').as_posix()}"

    class DerailOnNotify(script.SimulatedLLMClient):
        def generate(self, messages, tool_schemas):
            response = super().generate(messages, tool_schemas)
            if response.tool_call is not None and response.tool_call.name == "notify_farmer":
                self._derailed = True
                return LLMResponse(
                    tool_call=LLMToolCallRequest(
                        id="x", name="notify_farmer", arguments={"farmer_id": "F-404", "message": "x"},
                    ),
                    final_text=None,
                )
            return response

    from agricam_reliable_agents.reliability import simulation

    monkeypatch.setattr(simulation, "SimulatedLLMClient", DerailOnNotify)
    args = script.parse_args(["--n-trials", "1", "--p-step", "1.0", "--database-url", db_url])
    campaign_id, reports, repo = script.run_campaign(args)

    incidents = repo.list_incidents(campaign_id)
    assert len(incidents) == 2  # T2 et T3 tentent une notification
    assert all(i.attack_category is AttackCategory.EXCESSIVE_AGENCY and i.blocked for i in incidents)
    assert sorted(i.task_id for i in incidents) == ["T2-treat-notify", "T3-full-workflow"]
    by_task = {r.task_id: r.p_hat for r in reports}
    assert by_task == {"T1-treat": 1.0, "T2-treat-notify": 0.0, "T3-full-workflow": 0.0}
    repo.engine.dispose()


def test_parse_args_validates_n_trials_and_p_step(script):
    with pytest.raises(SystemExit):
        script.parse_args(["--n-trials", "0"])
    with pytest.raises(SystemExit):
        script.parse_args(["--p-step", "1.5"])
