"""
Tests du dépôt de campagnes (`ReliabilityRepository`) et de la campagne
persistée (`run_persisted_campaign`).
"""

from __future__ import annotations

import pytest

from agricam_reliable_agents.models.data_models import (
    AgentResult,
    AttackCategory,
    ReliabilityReport,
    SecurityIncident,
    Task,
    TaskComplexity,
    ToolCall,
    VerificationResult,
)
from agricam_reliable_agents.reliability.campaign import run_persisted_campaign
from agricam_reliable_agents.reliability.harness import HarnessConfig, evaluate_task
from agricam_reliable_agents.reliability.repository import ReliabilityRepository


@pytest.fixture
def repo() -> ReliabilityRepository:
    return ReliabilityRepository.from_url("sqlite://")


def make_task(task_id: str = "T-REPO", complexity: TaskComplexity = TaskComplexity.LEVEL_2) -> Task:
    return Task(
        id=task_id, prompt="Traiter D-42 et notifier.", complexity=complexity,
        category="treatment", expected_state_delta={"diagnostic.D-42.status": "treated"},
        verification_query="q-repo",
    )


def make_result(trial_id: int = 0, *, declared: bool = True, task_id: str = "T-REPO") -> AgentResult:
    return AgentResult(
        task_id=task_id, trial_id=trial_id, final_answer="Traitement appliqué.",
        tool_calls=(ToolCall("recommend_treatment", {"diagnostic_id": "D-42"}),),
        declared_success=declared, latency_ms=12.5, cost_usd=0.002,
    )


def make_verification(trial_id: int = 0, *, matches: bool = True, task_id: str = "T-REPO") -> VerificationResult:
    return VerificationResult(
        task_id=task_id, trial_id=trial_id,
        actual_state_delta={"diagnostic.D-42.status": "treated"} if matches else {},
        matches_expected=matches, overconfidence_detected=not matches,
    )


# ---- Campagnes et tâches ---------------------------------------------------

def test_start_campaign_and_list(repo: ReliabilityRepository):
    first = repo.start_campaign("v1", model="claude-opus-5", notes="baseline")
    second = repo.start_campaign("v2")
    campaigns = repo.list_campaigns()
    assert [c.id for c in campaigns] == [second, first]  # plus récente en premier
    assert campaigns[1].model == "claude-opus-5"
    assert campaigns[1].notes == "baseline"
    assert campaigns[0].model is None
    assert campaigns[0].started_at.tzinfo is not None


def test_start_campaign_requires_name(repo: ReliabilityRepository):
    with pytest.raises(ValueError):
        repo.start_campaign("")


def test_record_task_upserts_and_round_trips(repo: ReliabilityRepository):
    task = make_task()
    repo.record_task(task)
    repo.record_task(task)  # second enregistrement : mise à jour, pas de doublon
    assert repo.get_task("T-REPO") == task
    assert repo.get_task("T-404") is None
    assert repo.list_tasks() == [task]


# ---- Essais ------------------------------------------------------------------

def test_record_trial_round_trip(repo: ReliabilityRepository):
    campaign_id = repo.start_campaign("c")
    repo.record_trial(campaign_id, make_result(0), make_verification(0))
    repo.record_trial(campaign_id, make_result(1, declared=True), make_verification(1, matches=False))

    trials = repo.list_trials(campaign_id)
    assert [t.trial_id for t in trials] == [0, 1]
    assert trials[0].tool_call_names == ("recommend_treatment",)
    assert trials[0].actual_state_delta == {"diagnostic.D-42.status": "treated"}
    assert trials[0].verified_success is True
    assert trials[1].overconfidence_detected is True
    assert trials[1].verified_success is False
    assert trials[0].recorded_at.tzinfo is not None
    assert repo.list_trials(campaign_id, task_id="T-AUTRE") == []


def test_record_trial_rejects_mismatched_pair(repo: ReliabilityRepository):
    campaign_id = repo.start_campaign("c")
    with pytest.raises(ValueError):
        repo.record_trial(campaign_id, make_result(0), make_verification(1))
    with pytest.raises(ValueError):
        repo.record_trial(campaign_id, make_result(0, task_id="A"), make_verification(0, task_id="B"))


def test_trials_are_isolated_per_campaign(repo: ReliabilityRepository):
    first = repo.start_campaign("a")
    second = repo.start_campaign("b")
    repo.record_trial(first, make_result(0), make_verification(0))
    assert len(repo.list_trials(first)) == 1
    assert repo.list_trials(second) == []


# ---- Rapports et incidents -----------------------------------------------------

def test_record_report_round_trip_with_int_keys(repo: ReliabilityRepository):
    campaign_id = repo.start_campaign("c")
    report = ReliabilityReport(
        task_id="T-REPO", n_trials=20, n_success=18, p_hat=0.9,
        wilson_ci=(0.699, 0.972), pass_k={1: 0.9, 5: 0.9**5, 10: 0.9**10},
    )
    repo.record_report(campaign_id, report)
    [read_back] = repo.list_reports(campaign_id)
    assert read_back == report  # les clés JSON (str) redeviennent des int


def test_record_incident_round_trip(repo: ReliabilityRepository):
    campaign_id = repo.start_campaign("c")
    incident = SecurityIncident(
        trial_id=3, attack_category=AttackCategory.EXCESSIVE_AGENCY,
        payload="{'farmer_id': 'F-999'}", blocked=True,
    )
    repo.incident_sink(campaign_id)(incident)
    [read_back] = repo.list_incidents(campaign_id)
    assert read_back.attack_category is AttackCategory.EXCESSIVE_AGENCY
    assert read_back.trial_id == 3
    assert read_back.blocked is True
    assert read_back.detected_at == incident.detected_at


# ---- Intégration avec le harnais ------------------------------------------------

def test_trial_observer_persists_every_trial_of_evaluate_task(repo: ReliabilityRepository):
    campaign_id = repo.start_campaign("c")
    state = {"diagnostic.D-42.status": "pending"}

    def agent(task: Task, trial_id: int) -> AgentResult:
        state["diagnostic.D-42.status"] = "treated"
        return make_result(trial_id)

    def snapshot(task: Task) -> dict:
        return dict(state)

    def before_trial(task: Task, trial_id: int) -> None:
        state["diagnostic.D-42.status"] = "pending"

    report = evaluate_task(
        make_task(), agent, snapshot, HarnessConfig(n_trials=4, k_values=(1,)),
        on_trial=repo.trial_observer(campaign_id), before_trial=before_trial,
    )
    assert report.n_success == 4
    assert [t.trial_id for t in repo.list_trials(campaign_id)] == [0, 1, 2, 3]


def test_run_persisted_campaign_records_tasks_trials_and_reports(repo: ReliabilityRepository):
    state = {"diagnostic.D-42.status": "pending"}
    resets: list[tuple[str, int]] = []

    def before_trial(task: Task, trial_id: int) -> None:
        resets.append((task.id, trial_id))
        state["diagnostic.D-42.status"] = "pending"

    def agent(task: Task, trial_id: int) -> AgentResult:
        # La tâche « longue » échoue un essai sur deux, mais déclare toujours réussir.
        if task.id == "T-SHORT" or trial_id % 2 == 0:
            state["diagnostic.D-42.status"] = "treated"
        return make_result(trial_id, task_id=task.id)

    tasks = [make_task("T-SHORT", TaskComplexity.LEVEL_1), make_task("T-LONG", TaskComplexity.LEVEL_3)]
    campaign_id = repo.start_campaign("sim", model="simulated")
    reports = run_persisted_campaign(
        repo, campaign_id, tasks, agent, lambda task: dict(state),
        HarnessConfig(n_trials=6, k_values=(1, 3)), before_trial=before_trial,
    )

    assert [r.task_id for r in reports] == ["T-SHORT", "T-LONG"]
    assert reports[0].p_hat == 1.0
    assert reports[1].p_hat == 0.5
    assert len(resets) == 12
    assert repo.list_reports(campaign_id) == reports
    assert [t.complexity for t in repo.list_tasks()] == [TaskComplexity.LEVEL_3, TaskComplexity.LEVEL_1]
    trials = repo.list_trials(campaign_id, task_id="T-LONG")
    assert len(trials) == 6
    assert sum(t.overconfidence_detected for t in trials) == 3
    [campaign] = repo.list_campaigns()
    assert (campaign.id, campaign.name, campaign.model) == (campaign_id, "sim", "simulated")
