"""
Requêtes du dashboard : du dépôt de campagnes aux tableaux affichables.

Toutes les fonctions sont pures vis-à-vis de l'interface : elles prennent
un `ReliabilityRepository` et renvoient des DataFrames pandas ou des
dataclasses, sans jamais importer Streamlit — ce qui les rend testables
avec pytest et réutilisables (export CSV, notebook, rapport PDF).

Les colonnes de complexité sont ordonnées selon `TaskComplexity` pour que
les graphiques lisent naturellement « tâches courtes → tâches longues ».
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import pandas as pd

from agricam_reliable_agents.models.data_models import ReliabilityReport, TaskComplexity
from agricam_reliable_agents.reliability.repository import ReliabilityRepository
from agricam_reliable_agents.reliability.stats import pass_k

COMPLEXITY_ORDER: tuple[str, ...] = tuple(level.value for level in TaskComplexity)
UNKNOWN_COMPLEXITY = "inconnue"
DEFAULT_K_MAX = 10


@dataclass(frozen=True, slots=True)
class CampaignOverview:
    """Indicateurs de tête d'une campagne (ligne de KPI du dashboard)."""

    n_tasks: int
    n_trials: int
    n_success: int
    p_hat: float
    overconfidence_rate: float
    n_incidents: int
    n_blocked: int
    total_cost_usd: float
    mean_latency_ms: float


def _complexity_by_task(repository: ReliabilityRepository) -> dict[str, str]:
    return {task.id: task.complexity.value for task in repository.list_tasks()}


def _ordered_complexity(values: Iterable[str]) -> pd.Categorical:
    return pd.Categorical(list(values), categories=[*COMPLEXITY_ORDER, UNKNOWN_COMPLEXITY], ordered=True)


def campaign_overview(repository: ReliabilityRepository, campaign_id: int) -> CampaignOverview:
    trials = repository.list_trials(campaign_id)
    incidents = repository.list_incidents(campaign_id)
    n_trials = len(trials)
    n_success = sum(t.verified_success for t in trials)
    return CampaignOverview(
        n_tasks=len({t.task_id for t in trials}),
        n_trials=n_trials,
        n_success=n_success,
        p_hat=n_success / n_trials if n_trials else 0.0,
        overconfidence_rate=(sum(t.overconfidence_detected for t in trials) / n_trials) if n_trials else 0.0,
        n_incidents=len(incidents),
        n_blocked=sum(i.blocked for i in incidents),
        total_cost_usd=sum(t.cost_usd for t in trials),
        mean_latency_ms=(sum(t.latency_ms for t in trials) / n_trials) if n_trials else 0.0,
    )


def reports_frame(repository: ReliabilityRepository, campaign_id: int) -> pd.DataFrame:
    """Une ligne par tâche : p̂, IC de Wilson, pass^k archivés et taux de sur-confiance."""
    complexity = _complexity_by_task(repository)
    trials = repository.list_trials(campaign_id)
    overconfident: dict[str, int] = {}
    counted: dict[str, int] = {}
    for t in trials:
        overconfident[t.task_id] = overconfident.get(t.task_id, 0) + int(t.overconfidence_detected)
        counted[t.task_id] = counted.get(t.task_id, 0) + 1
        if t.complexity:
            complexity.setdefault(t.task_id, t.complexity)

    rows = []
    for report in repository.list_reports(campaign_id):
        n_observed = counted.get(report.task_id, 0) or report.n_trials
        row = {
            "task_id": report.task_id,
            "complexity": complexity.get(report.task_id, UNKNOWN_COMPLEXITY),
            "n_trials": report.n_trials,
            "n_success": report.n_success,
            "p_hat": report.p_hat,
            "ci_low": report.wilson_ci[0],
            "ci_high": report.wilson_ci[1],
            # rapporté au nombre d'essais réellement archivés : reste dans [0, 1]
            # même si une campagne a été rejouée sur le même identifiant
            "overconfidence_rate": overconfident.get(report.task_id, 0) / n_observed,
        }
        for k, value in sorted(report.pass_k.items()):
            row[f"pass_{k}"] = value
        rows.append(row)

    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    frame["complexity"] = _ordered_complexity(frame["complexity"])
    return frame.sort_values(["complexity", "task_id"]).reset_index(drop=True)


def pass_k_frame(reports: Sequence[ReliabilityReport], k_max: int = DEFAULT_K_MAX) -> pd.DataFrame:
    """Courbes pass^k = p̂^k pour k = 1..k_max, au format long (task_id, k, pass_k)."""
    if k_max < 1:
        raise ValueError("k_max doit être >= 1.")
    rows = [
        {"task_id": report.task_id, "k": k, "pass_k": pass_k(report.p_hat, k)}
        for report in reports
        for k in range(1, k_max + 1)
    ]
    return pd.DataFrame(rows, columns=["task_id", "k", "pass_k"])


def trials_frame(repository: ReliabilityRepository, campaign_id: int) -> pd.DataFrame:
    """Un essai par ligne, pour le tableau détaillé et les filtres."""
    complexity = _complexity_by_task(repository)
    rows = [
        {
            "task_id": t.task_id,
            "complexity": complexity.get(t.task_id, UNKNOWN_COMPLEXITY),
            "trial_id": t.trial_id,
            "verified_success": t.verified_success,
            "declared_success": t.declared_success,
            "overconfidence_detected": t.overconfidence_detected,
            "max_steps_exceeded": t.max_steps_exceeded,
            "n_tool_calls": len(t.tool_call_names),
            "tool_calls": " → ".join(t.tool_call_names),
            "latency_ms": t.latency_ms,
            "cost_usd": t.cost_usd,
            "error": t.error,
            "final_answer": t.final_answer,
            "recorded_at": t.recorded_at,
        }
        for t in repository.list_trials(campaign_id)
    ]
    frame = pd.DataFrame(rows)
    if not frame.empty:
        frame["complexity"] = _ordered_complexity(frame["complexity"])
    return frame


def incidents_frame(repository: ReliabilityRepository, campaign_id: int) -> pd.DataFrame:
    rows = [
        {
            "task_id": i.task_id,
            "trial_id": i.trial_id,
            "attack_category": i.attack_category.value,
            "blocked": i.blocked,
            "payload": i.payload,
            "detected_at": i.detected_at,
        }
        for i in repository.list_incidents(campaign_id)
    ]
    return pd.DataFrame(rows, columns=["task_id", "trial_id", "attack_category", "blocked", "payload", "detected_at"])


def comparison_frame(repository: ReliabilityRepository, campaign_ids: Sequence[int]) -> pd.DataFrame:
    """p̂ et IC par tâche pour plusieurs campagnes : lecture d'une régression
    (ou d'un progrès) de fiabilité entre deux versions d'agent."""
    names = {c.id: c.name for c in repository.list_campaigns()}
    complexity = _complexity_by_task(repository)
    rows = [
        {
            "campaign_id": campaign_id,
            "campaign": names.get(campaign_id, f"#{campaign_id}"),
            "task_id": report.task_id,
            "complexity": complexity.get(report.task_id, UNKNOWN_COMPLEXITY),
            "p_hat": report.p_hat,
            "ci_low": report.wilson_ci[0],
            "ci_high": report.wilson_ci[1],
        }
        for campaign_id in campaign_ids
        for report in repository.list_reports(campaign_id)
    ]
    frame = pd.DataFrame(
        rows, columns=["campaign_id", "campaign", "task_id", "complexity", "p_hat", "ci_low", "ci_high"],
    )
    if not frame.empty:
        frame["complexity"] = _ordered_complexity(frame["complexity"])
        frame = frame.sort_values(["complexity", "task_id", "campaign_id"]).reset_index(drop=True)
    return frame
