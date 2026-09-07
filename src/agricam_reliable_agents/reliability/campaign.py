"""
Campagne persistée : le harnais (`evaluate_task`) branché sur le dépôt
(`ReliabilityRepository`).

`run_persisted_campaign` est la seule fonction du projet qui écrit dans
les deux mondes à la fois : pour une campagne déjà ouverte (l'appelant
fait `repository.start_campaign(...)` lui-même, ce qui lui permet de
construire d'autres callbacks liés à la campagne, comme
`repository.incident_sink(campaign_id)`), elle enregistre les définitions
de tâches, laisse le harnais faire son travail en lui passant
l'observateur du dépôt, puis archive chaque rapport. Elle ne calcule rien
elle-même : les statistiques restent dans `harness.py`/`stats.py`.
"""

from __future__ import annotations

from collections.abc import Iterable

from agricam_reliable_agents.models.data_models import ReliabilityReport, Task
from agricam_reliable_agents.reliability.harness import (
    AgentRunner,
    BeforeTrialHook,
    HarnessConfig,
    StateSnapshotFn,
    evaluate_task,
)
from agricam_reliable_agents.reliability.repository import ReliabilityRepository


def run_persisted_campaign(
    repository: ReliabilityRepository,
    campaign_id: int,
    tasks: Iterable[Task],
    agent_runner: AgentRunner,
    state_snapshot_fn: StateSnapshotFn,
    config: HarnessConfig | None = None,
    *,
    before_trial: BeforeTrialHook | None = None,
) -> list[ReliabilityReport]:
    """
    Exécute toutes les tâches sur la campagne `campaign_id` et persiste
    essais et rapports. Retourne les rapports dans l'ordre des tâches.

    `before_trial(task, trial_id)` est appelé avant chaque essai (typiquement
    pour remettre le data store dans un état connu) — voir `evaluate_task`.
    """
    observer = repository.trial_observer(campaign_id)
    reports: list[ReliabilityReport] = []
    for task in tasks:
        repository.record_task(task)
        report = evaluate_task(
            task, agent_runner, state_snapshot_fn, config,
            on_trial=observer, before_trial=before_trial,
        )
        repository.record_report(campaign_id, report)
        reports.append(report)
    return reports
