"""
Couche d'orchestration — Tâche 5.3 : une campagne ne peut démarrer sur une
tâche dont l'oracle n'a pas `validated_by_human = True`.

Cette contrainte vit ICI, dans le paquet qui SAIT ce qu'est un oracle —
jamais dans `reliability/harness.py`, qui reste générique et n'a AUCUNE
notion d'oracle (le principe même du connecteur : s'adapter au harnais,
jamais le modifier). Elle vit aussi ICI plutôt que seulement dans une
interface (dashboard, CLI) précisément pour qu'un appel direct à
`run_gated_campaign` — en contournant tout dashboard, par un script ou un
test — la respecte quand même : « appliqué au niveau du harnais ou d'une
couche d'orchestration, jamais seulement dans l'interface ».
"""

from __future__ import annotations

from collections.abc import Iterable

from agricam_reliable_agents.connector.oracle_repository import (
    OracleRepository,
    TaskOracle,
)
from agricam_reliable_agents.models.data_models import ReliabilityReport, Task
from agricam_reliable_agents.reliability.campaign import run_persisted_campaign
from agricam_reliable_agents.reliability.harness import (
    AgentRunner,
    BeforeTrialHook,
    HarnessConfig,
    StateSnapshotFn,
)
from agricam_reliable_agents.reliability.repository import ReliabilityRepository


class OracleNotValidatedError(RuntimeError):
    """Levée quand une tâche n'a pas d'oracle validé par un humain."""


def require_validated_oracle(oracle_repository: OracleRepository, task_id: str) -> TaskOracle:
    """
    Vérifie que la tâche `task_id` a un oracle et qu'il est validé par un
    humain. Relit systématiquement l'état ACTUEL en base (jamais un
    indicateur mis en cache côté appelant) : rien ne permet de « déjà
    savoir » que c'est bon sans repasser par cette vérification.

    Raises:
        OracleNotValidatedError: aucun oracle pour cette tâche, ou un
            oracle existe mais `validated_by_human` est faux.
    """
    oracle = oracle_repository.get_oracle(task_id)
    if oracle is None:
        raise OracleNotValidatedError(f"Aucun oracle défini pour la tâche {task_id!r}.")
    if not oracle.validated_by_human:
        raise OracleNotValidatedError(
            f"L'oracle de la tâche {task_id!r} n'est pas validé par un humain "
            "(validated_by_human=False) : une campagne ne peut pas s'en servir."
        )
    return oracle


def run_gated_campaign(
    oracle_repository: OracleRepository,
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
    Point d'entrée RECOMMANDÉ pour lancer une campagne via le Connecteur
    d'Agent Générique : applique `require_validated_oracle` à CHAQUE tâche
    avant d'appeler `run_persisted_campaign` (`reliability/campaign.py`,
    non modifié).

    Si UNE seule tâche de la liste n'a pas d'oracle validé, AUCUN essai
    n'est exécuté pour aucune tâche — échec net avant le premier essai,
    jamais une exécution partielle qui laisserait croire que « le reste
    est bon ».
    """
    tasks = tuple(tasks)
    for task in tasks:
        require_validated_oracle(oracle_repository, task.id)
    return run_persisted_campaign(
        repository, campaign_id, tasks, agent_runner, state_snapshot_fn, config, before_trial=before_trial,
    )
