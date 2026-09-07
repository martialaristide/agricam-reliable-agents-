"""Requêtes du dashboard (pandas), testées sur un dépôt SQLite en mémoire."""

from __future__ import annotations

import pytest

from agricam_reliable_agents.dashboard import queries
from agricam_reliable_agents.dashboard.theme import (
    DARK,
    LIGHT,
    palette_for,
    series_color,
)
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
from agricam_reliable_agents.reliability.repository import ReliabilityRepository


def _task(task_id: str, complexity: TaskComplexity) -> Task:
    return Task(id=task_id, prompt="p", complexity=complexity, category="c",
                expected_state_delta={"x": 1}, verification_query="q")


def _trial(repo: ReliabilityRepository, campaign_id: int, task_id: str, trial_id: int,
           *, success: bool, declared: bool = True, cost: float = 0.01) -> None:
    result = AgentResult(task_id=task_id, trial_id=trial_id, final_answer="Fait.",
                         tool_calls=(ToolCall("recommend_treatment", {}),) if success else (),
                         declared_success=declared, latency_ms=100.0, cost_usd=cost)
    verification = VerificationResult(task_id=task_id, trial_id=trial_id,
                                      actual_state_delta={"x": 1} if success else {},
                                      matches_expected=success, overconfidence_detected=declared and not success)
    repo.record_trial(campaign_id, result, verification)


@pytest.fixture
def repo() -> ReliabilityRepository:
    repo = ReliabilityRepository.from_url("sqlite://")
    # Tâches enregistrées dans le désordre : les requêtes doivent trier par complexité.
    repo.record_task(_task("LONG", TaskComplexity.LEVEL_3))
    repo.record_task(_task("SHORT", TaskComplexity.LEVEL_1))

    first = repo.start_campaign("v1")
    for i in range(4):
        _trial(repo, first, "SHORT", i, success=True)
    for i in range(4):
        _trial(repo, first, "LONG", i, success=(i % 2 == 0))
    repo.record_report(first, ReliabilityReport("SHORT", 4, 4, 1.0, (0.51, 1.0), {1: 1.0, 5: 1.0}))
    repo.record_report(first, ReliabilityReport("LONG", 4, 2, 0.5, (0.15, 0.85), {1: 0.5, 5: 0.03125}))
    repo.record_incident(first, SecurityIncident(1, AttackCategory.EXCESSIVE_AGENCY, "F-999", True))
    repo.record_incident(first, SecurityIncident(2, AttackCategory.PROMPT_INJECTION_INDIRECT, "x", False))

    second = repo.start_campaign("v2")
    _trial(repo, second, "LONG", 0, success=True)
    repo.record_report(second, ReliabilityReport("LONG", 1, 1, 1.0, (0.2, 1.0), {1: 1.0}))
    return repo


def test_campaign_overview(repo):
    overview = queries.campaign_overview(repo, 1)
    assert (overview.n_tasks, overview.n_trials, overview.n_success) == (2, 8, 6)
    assert overview.p_hat == 0.75
    assert overview.overconfidence_rate == 0.25
    assert (overview.n_incidents, overview.n_blocked) == (2, 1)
    assert overview.total_cost_usd == pytest.approx(0.08)
    assert overview.mean_latency_ms == 100.0


def test_campaign_overview_of_empty_campaign(repo):
    empty = repo.start_campaign("empty")
    overview = queries.campaign_overview(repo, empty)
    assert overview.n_trials == 0 and overview.p_hat == 0.0 and overview.overconfidence_rate == 0.0


def test_reports_frame_is_sorted_by_complexity_and_carries_pass_k(repo):
    frame = queries.reports_frame(repo, 1)
    assert list(frame["task_id"]) == ["SHORT", "LONG"]
    assert list(frame["complexity"].astype(str)) == ["1-2_steps", "6+_steps"]
    assert list(frame["overconfidence_rate"]) == [0.0, 0.5]
    assert frame.loc[1, "pass_5"] == 0.03125
    assert queries.reports_frame(repo, 999).empty


def test_reports_frame_marks_unknown_complexity():
    repo = ReliabilityRepository.from_url("sqlite://")
    cid = repo.start_campaign("c")
    repo.record_report(cid, ReliabilityReport("ORPHAN", 1, 1, 1.0, (0.2, 1.0), {1: 1.0}))
    frame = queries.reports_frame(repo, cid)
    assert frame.loc[0, "complexity"] == queries.UNKNOWN_COMPLEXITY


def test_pass_k_frame_computes_curves(repo):
    curves = queries.pass_k_frame(repo.list_reports(1), k_max=3)
    assert len(curves) == 6
    long_curve = curves[curves["task_id"] == "LONG"]
    assert list(long_curve["pass_k"]) == [0.5, 0.25, 0.125]
    with pytest.raises(ValueError):
        queries.pass_k_frame([], k_max=0)
    assert queries.pass_k_frame([]).empty


def test_trials_and_incidents_frames(repo):
    trials = queries.trials_frame(repo, 1)
    assert len(trials) == 8
    assert trials["overconfidence_detected"].sum() == 2
    assert set(trials.columns) >= {"task_id", "complexity", "tool_calls", "final_answer", "recorded_at"}
    assert trials.loc[0, "tool_calls"] == "recommend_treatment"

    incidents = queries.incidents_frame(repo, 1)
    assert list(incidents["attack_category"]) == ["LLM08", "LLM01_indirect"]
    assert list(incidents["blocked"]) == [True, False]
    assert queries.incidents_frame(repo, 999).empty
    assert queries.trials_frame(repo, 999).empty


def test_comparison_frame_joins_campaign_names(repo):
    frame = queries.comparison_frame(repo, [1, 2])
    assert list(frame["task_id"]) == ["SHORT", "LONG", "LONG"]
    assert list(frame["campaign"]) == ["v1", "v1", "v2"]
    assert list(frame["p_hat"]) == [1.0, 0.5, 1.0]
    assert queries.comparison_frame(repo, []).empty


def test_theme_palette_selection_and_series_overflow():
    assert palette_for("dark") is DARK
    assert palette_for("light") is LIGHT
    assert palette_for(None) is LIGHT
    assert series_color(LIGHT, 0) == LIGHT.series[0]
    assert series_color(LIGHT, 99) == LIGHT.neutral  # jamais de teinte générée
