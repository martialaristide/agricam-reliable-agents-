"""
Harnais de fiabilité — exécute chaque tâche N fois et calcule pass^k.

Ce module est délibérément générique : il ne connaît rien d'AgriCam. Il
dépend uniquement de deux fonctions injectées par l'appelant :

- `agent_runner(task, trial_id) -> AgentResult` : exécute l'agent à tester.
- `state_snapshot_fn(task) -> dict` : capture l'état actuel du système,
  restreint aux champs pertinents pour la tâche.

Cette généricité permet de réutiliser le harnais sur n'importe quel agent
(pas seulement celui d'AgriCam) — c'est ce qui en fait un projet open
source autonome et valorisable indépendamment d'AgriCam.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol

from agricam_reliable_agents.models.data_models import (
    AgentResult,
    ReliabilityReport,
    Task,
    VerificationResult,
)
from agricam_reliable_agents.reliability.stats import pass_k, wilson_score_interval
from agricam_reliable_agents.verifier.success_verifier import verify

DEFAULT_K_VALUES: tuple[int, ...] = (1, 3, 5, 10)
_DEFAULT_CONFIG_SINGLETON: HarnessConfig | None = None


def _default_config() -> HarnessConfig:
    """Retourne un singleton HarnessConfig() par défaut, pour éviter un
    appel de constructeur dans une valeur par défaut d'argument (piège
    classique en Python : les valeurs par défaut ne sont évaluées qu'une
    fois à la définition de la fonction, ce qui est correct ici car
    HarnessConfig est immuable, mais explicite volontairement ce choix)."""
    global _DEFAULT_CONFIG_SINGLETON
    if _DEFAULT_CONFIG_SINGLETON is None:
        _DEFAULT_CONFIG_SINGLETON = HarnessConfig()
    return _DEFAULT_CONFIG_SINGLETON


class AgentRunner(Protocol):
    """Contrat minimal qu'un agent doit respecter pour être évalué."""

    def __call__(self, task: Task, trial_id: int) -> AgentResult: ...


class StateSnapshotFn(Protocol):
    """Capture l'état du système pertinent pour une tâche donnée."""

    def __call__(self, task: Task) -> dict[str, Any]: ...


class TrialObserver(Protocol):
    """Callback optionnel appelé après chaque essai (ex. persistance en base)."""

    def __call__(self, result: AgentResult, verification: VerificationResult) -> None: ...


@dataclass(frozen=True, slots=True)
class HarnessConfig:
    """Paramètres d'exécution d'une campagne d'évaluation."""

    n_trials: int = 20
    k_values: tuple[int, ...] = DEFAULT_K_VALUES

    def __post_init__(self) -> None:
        if self.n_trials <= 0:
            raise ValueError("n_trials doit être strictement positif.")
        if any(k < 1 for k in self.k_values):
            raise ValueError("Toutes les valeurs de k doivent être >= 1.")


def evaluate_task(
    task: Task,
    agent_runner: AgentRunner,
    state_snapshot_fn: StateSnapshotFn,
    config: HarnessConfig | None = None,
    on_trial: TrialObserver | None = None,
) -> ReliabilityReport:
    """
    Exécute une tâche `config.n_trials` fois et calcule son rapport de
    fiabilité (p̂, intervalle de Wilson, pass^k pour chaque k demandé).

    Chaque essai est indépendant : un instantané de l'état est pris juste
    avant et juste après l'exécution de l'agent, puis comparé par le
    Success Verifier — jamais sur la seule base de ce que l'agent déclare.
    """
    config = config or _default_config()
    successes = 0
    for trial_id in range(config.n_trials):
        state_before = state_snapshot_fn(task)
        result = agent_runner(task, trial_id)
        state_after = state_snapshot_fn(task)

        verification = verify(task, result, state_before, state_after)
        if verification.matches_expected and not verification.overconfidence_detected:
            successes += 1

        if on_trial is not None:
            on_trial(result, verification)

    p_hat = successes / config.n_trials
    ci = wilson_score_interval(successes, config.n_trials)
    pk = {k: pass_k(p_hat, k) for k in config.k_values}

    return ReliabilityReport(
        task_id=task.id,
        n_trials=config.n_trials,
        n_success=successes,
        p_hat=p_hat,
        wilson_ci=ci,
        pass_k=pk,
    )


def evaluate_reliability(
    tasks: Iterable[Task],
    agent_runner: AgentRunner,
    state_snapshot_fn: StateSnapshotFn,
    config: HarnessConfig | None = None,
    on_trial: TrialObserver | None = None,
) -> list[ReliabilityReport]:
    """Exécute `evaluate_task` pour chaque tâche de `tasks` et agrège les rapports."""
    config = config or _default_config()
    return [
        evaluate_task(task, agent_runner, state_snapshot_fn, config, on_trial)
        for task in tasks
    ]
