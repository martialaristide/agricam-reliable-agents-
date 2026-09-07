"""
Audit périodique : validation de l'oracle par mutation ciblée — Tâche
5ter (section 3.3 du document de référence).

L'approbation de diff (Tâche 5bis, `oracle_approval.py`) résout
l'omission humaine, mais ne prouve pas que l'oracle approuvé DÉTECTE
réellement une régression. Le mutation testing — introduire délibérément
un défaut connu et vérifier que l'oracle le remarque — apporte cette
preuve. La mutation se fait à la frontière des OUTILS, jamais dans le
code de l'agent testé (qui peut être une boîte noire, cf. `connector/base.py`).

Cet audit tourne PÉRIODIQUEMENT sur les oracles existants (pas à chaque
campagne) : c'est un contrôle de qualité de l'ORACLE lui-même, séparé de
la mesure de fiabilité de l'agent (`reliability/harness.py`, non touché).

Écart assumé par rapport au pseudocode de la section 3.3
-----------------------------------------------------------
Le document esquisse `run_mutation_audit(oracle, agent_runner, mutations)`
avec un SEUL `agent_runner` déjà construit. En pratique, appliquer une
mutation « à la frontière des outils » exige de savoir COMMENT l'agent
appelle ses outils — un détail que le Connecteur d'Agent Générique refuse
précisément de connaître (`AgentConnector` ne voit qu'un prompt et un
texte, jamais un appel d'outil, cf. `connector/bridge.py`). Cette fonction
prend donc une FABRIQUE `agent_runner_factory(mutation) -> (AgentRunner,
StateSnapshotFn)` : c'est l'appelant, qui connaît l'architecture concrète
de l'agent testé, qui sait comment brancher une mutation sur SES outils
et sur QUEL état l'observer ensuite (typiquement le même, frais, à chaque
mutation).
`mutated_agricam_runner` fournit cette fabrique clé en main pour l'agent
AgriCam de référence (boîte BLANCHE, exercée par les tests de ce module).
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from agricam_reliable_agents.agent.llm_client import LLMClient
from agricam_reliable_agents.agent.loop import (
    DEFAULT_SUCCESS_CLAIM_EXTRACTOR,
    SuccessClaimExtractor,
    run_agent,
)
from agricam_reliable_agents.connector.oracle_approval import (
    verify_against_approved_oracle,
)
from agricam_reliable_agents.connector.oracle_repository import TaskOracle
from agricam_reliable_agents.mcp_tools.data_store import AgriCamDataStore
from agricam_reliable_agents.mcp_tools.tools import AgriCamTools
from agricam_reliable_agents.models.data_models import AgentResult, Task
from agricam_reliable_agents.reliability.harness import AgentRunner, StateSnapshotFn
from agricam_reliable_agents.security.policy import ActionPolicy


class ToolMutation(Protocol):
    """
    Modifie délibérément l'effet d'un outil pour simuler un défaut connu,
    sans toucher au code de l'agent testé.
    """

    def apply(self, tool_name: str, result: dict[str, Any]) -> dict[str, Any]: ...


# ---------------------------------------------------------------------------
# Trois mutations concrètes et réutilisables
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class SilentNoOpMutation:
    """
    L'outil `tool_name` répond succès mais n'effectue RIEN : le texte
    renvoyé à l'agent reste celui d'un vrai succès (`apply` ne le
    modifie pas), mais l'effet réel sur l'état est neutralisé par
    `MutatedAgriCamTools` (ci-dessous), qui restaure l'état
    d'avant l'appel après coup. C'est le défaut exact donné en exemple
    par le document de référence : « neutralise notify_farmer sans
    erreur visible ».
    """

    tool_name: str

    def apply(self, tool_name: str, result: dict[str, Any]) -> dict[str, Any]:
        return result


@dataclass(slots=True)
class PartialEffectMutation:
    """
    Neutralise l'effet réel de `tool_name` UN APPEL SUR DEUX (garde un
    compteur interne) — simule une régression INTERMITTENTE plutôt que
    systématique, souvent plus difficile à repérer manuellement qu'un
    échec constant.
    """

    tool_name: str
    _call_count: int = field(default=0, init=False, compare=False)

    def should_neutralize_this_call(self) -> bool:
        """Décide, en avançant le compteur, si CET appel doit être neutralisé
        (un appel sur deux : les appels de rang pair)."""
        neutralize = self._call_count % 2 == 0
        self._call_count += 1
        return neutralize

    def apply(self, tool_name: str, result: dict[str, Any]) -> dict[str, Any]:
        return result


@dataclass(frozen=True, slots=True)
class WrongValueMutation:
    """
    L'outil `tool_name` effectue bien un effet réel (contrairement aux
    deux mutations précédentes), mais `field_key` (au format d'instantané
    AgriCam, ex. `"diagnostic.D-42.status"`) prend `wrong_value` au lieu
    de la valeur correcte — simule un bug de LOGIQUE MÉTIER (mauvais
    statut écrit), pas une simple omission.
    """

    tool_name: str
    field_key: str
    wrong_value: Any

    def apply(self, tool_name: str, result: dict[str, Any]) -> dict[str, Any]:
        return result


# ---------------------------------------------------------------------------
# Application des mutations à AgriCamDataStore (store en mémoire)
# ---------------------------------------------------------------------------

def write_snapshot_field(store: AgriCamDataStore, key: str, value: Any) -> None:
    """
    Écrit directement `value` à l'emplacement désigné par une clé au
    format `AgriCamDataStore.snapshot()` (`"diagnostic.<id>.status"`,
    `"product.<id>.stock_qty"`, `"farmer.<id>.notified_count"`).

    Utilitaire d'AUDIT/DE SIMULATION, volontairement couplé à AgriCam et à
    son store en mémoire (accède à des attributs privés du store) :
    contrairement au reste du projet, ce module (et `connector/environment.py`,
    qui le réutilise pour le mode dry-run de la Tâche 6 — même mécanisme,
    « répondre succès sans rien faire ») a besoin d'un point d'injection
    que l'interface publique de `DataStore` n'a — et ne doit — pas exposer
    (personne ne devrait pouvoir écrire un état arbitraire en production).
    Clé non reconnue : ignorée silencieusement (rien à corrompre).
    """
    parts = key.split(".")
    if len(parts) != 3:
        return
    entity, entity_id, field_name = parts
    if entity == "diagnostic" and field_name == "status" and entity_id in store._diagnostics:
        store._diagnostics[entity_id].status = value
    elif entity == "product" and field_name == "stock_qty" and entity_id in store._products:
        store._products[entity_id].stock_qty = value
    elif entity == "farmer" and field_name == "notified_count" and entity_id in store._farmers:
        messages = store._farmers[entity_id].notified_messages
        if value < len(messages):
            del messages[value:]
        else:
            messages.extend([""] * (value - len(messages)))


class MutatedAgriCamTools:
    """
    Enveloppe `AgriCamTools` (store `AgriCamDataStore` en mémoire
    uniquement) : exécute chaque appel normalement puis, pour les outils
    ciblés par une mutation, corrige l'état réel après coup — le résultat
    JSON renvoyé à l'agent reste celui du VRAI appel (encore retouchable
    par `mutation.apply`), seul l'ÉTAT diverge de ce que l'agent croit
    avoir obtenu.
    """

    def __init__(self, tools: AgriCamTools, mutations: Sequence[ToolMutation]) -> None:
        if not isinstance(tools.store, AgriCamDataStore):
            raise TypeError(
                "MutatedAgriCamTools ne fonctionne qu'avec le store en mémoire "
                "(AgriCamDataStore) : l'injection de mutation manipule ses attributs internes."
            )
        self._tools = tools
        self._mutations = list(mutations)

    @property
    def store(self) -> AgriCamDataStore:
        return self._tools.store  # type: ignore[return-value]

    def dispatch(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        targeting = [m for m in self._mutations if getattr(m, "tool_name", None) == tool_name]
        before = self.store.snapshot()
        result = self._tools.dispatch(tool_name, arguments)

        for mutation in targeting:
            if isinstance(mutation, SilentNoOpMutation) or isinstance(mutation, PartialEffectMutation) and mutation.should_neutralize_this_call():
                after = self.store.snapshot()
                for key, value in before.items():
                    if after.get(key) != value:
                        write_snapshot_field(self.store, key, value)
            elif isinstance(mutation, WrongValueMutation):
                write_snapshot_field(self.store, mutation.field_key, mutation.wrong_value)
            result = mutation.apply(tool_name, result)

        return result


def mutated_agricam_runner(
    task_prompt_llm_client: LLMClient,
    policy: ActionPolicy,
    mutations: Sequence[ToolMutation],
    *,
    store_factory: Callable[[], AgriCamDataStore] | None = None,
    success_claim_extractor: SuccessClaimExtractor = DEFAULT_SUCCESS_CLAIM_EXTRACTOR,
) -> tuple[AgentRunner, StateSnapshotFn]:
    """
    Fabrique clé en main pour auditer l'agent AgriCam de référence : lance
    `run_agent` (agent/loop.py, non modifié) sur des outils enveloppés par
    `MutatedAgriCamTools`. Retourne `(agent_runner, state_snapshot_fn)`
    prêts pour `run_mutation_audit` — une base fraîche (semée) est créée
    à chaque appel, pour que chaque essai reparte d'un état connu.
    """
    if store_factory is None:
        def store_factory() -> AgriCamDataStore:
            store = AgriCamDataStore()
            store.seed_demo_data()
            return store

    store = store_factory()
    mutated_tools = MutatedAgriCamTools(AgriCamTools(store), mutations)

    def runner(task: Task, trial_id: int) -> AgentResult:
        return run_agent(
            task, trial_id, task_prompt_llm_client, mutated_tools, policy,  # type: ignore[arg-type]
            success_claim_extractor=success_claim_extractor,
        )

    return runner, lambda _task: store.snapshot()


# ---------------------------------------------------------------------------
# Rapport d'audit
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class SurvivingMutant:
    """Un mutant NON détecté : révèle un angle mort de l'ORACLE, jamais un
    problème de l'agent (le document de référence est explicite sur ce
    point, cf. docstring de `run_mutation_audit`)."""

    mutation: ToolMutation
    mutation_name: str
    trial_id: int
    actual_state_delta: dict[str, Any]
    unexpected_fields: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MutationAuditReport:
    """
    Résultat de `run_mutation_audit`. Un mutant NON détecté apparaît
    explicitement dans `surviving_mutants` avec la mutation responsable —
    jamais noyé dans un score agrégé (cf. critère d'acceptation de la
    Tâche 5ter).
    """

    task_id: str
    n_mutations: int
    surviving_mutants: tuple[SurvivingMutant, ...]
    audited_at: datetime

    @property
    def all_mutants_detected(self) -> bool:
        return not self.surviving_mutants


def run_mutation_audit(
    oracle: TaskOracle,
    task: Task,
    agent_runner_factory: Callable[[ToolMutation], tuple[AgentRunner, StateSnapshotFn]],
    mutations: Sequence[ToolMutation],
    *,
    trial_id: int = 0,
) -> MutationAuditReport:
    """
    Pour CHAQUE mutation, construit un `(AgentRunner, StateSnapshotFn)` via
    `agent_runner_factory(mutation)`, rejoue `task` une fois, et vérifie
    via `verify_against_approved_oracle` (Tâche 5bis, réutilisé sans
    réimplémentation) si l'oracle détecte l'échec injecté.

    `agent_runner_factory` rend la PAIRE `(runner, state_snapshot_fn)`,
    jamais les deux séparément : chaque mutation a typiquement besoin
    d'un état frais (un nouveau store, ré-ensemencé), et l'instantané doit
    obligatoirement porter sur CE MÊME état, pas sur un store sans rapport
    construit à part — `mutated_agricam_runner` rend déjà cette paire
    couplée, prête à l'emploi.

    Un mutant NON détecté (l'oracle dit `matches_expected=True` alors que
    la mutation a bel et bien altéré l'effet réel) est un angle mort de
    l'ORACLE — jamais un problème de l'agent, qui n'a ici rien fait de
    mal : c'est l'infrastructure de test elle-même qui a triché.
    """
    surviving: list[SurvivingMutant] = []
    for mutation in mutations:
        runner, state_snapshot_fn = agent_runner_factory(mutation)
        state_before = state_snapshot_fn(task)
        result = runner(task, trial_id)
        state_after = state_snapshot_fn(task)

        verdict = verify_against_approved_oracle(oracle, result, state_before, state_after)
        if verdict.matches_expected:
            surviving.append(SurvivingMutant(
                mutation=mutation, mutation_name=type(mutation).__name__, trial_id=trial_id,
                actual_state_delta=copy.deepcopy(verdict.actual_state_delta),
                unexpected_fields=verdict.unexpected_fields,
            ))

    return MutationAuditReport(
        task_id=oracle.task_id, n_mutations=len(mutations),
        surviving_mutants=tuple(surviving), audited_at=datetime.now(timezone.utc),
    )
