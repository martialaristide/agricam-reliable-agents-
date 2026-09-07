"""
Environnements et mode dry-run — Tâche 6 (Phase D, section 4 du document
de référence : `agent_connections.environment`, déjà dans le schéma de la
Tâche 5, propagée ici jusqu'à la boucle d'exécution).

Deux garde-fous INDÉPENDANTS et composables (l'un n'implique pas l'autre :
on peut vouloir un dry-run en 'staging', ou une exécution réelle en 'test'
contre un tenant sandboxé) :

1. `require_production_confirmation` : une campagne visant une connexion
   étiquetée `production` sans confirmation explicite est bloquée par une
   exception claire — jamais une exécution silencieuse avec un simple
   avertissement dans les journaux (section 4.1).
2. `DryRunAgriCamTools` : les outils À EFFET DE BORD (`SENSITIVE_TOOLS`,
   `security/policy.py`, étendu si besoin pour un agent tiers via
   `sensitive_tools=`) sont interceptés — journalisés dans `.log`, jamais
   appliqués — et une réponse simulée PLAUSIBLE (celle du vrai appel) est
   retournée à l'agent (section 4.2). Réutilise le mécanisme de
   `oracle_mutation.py` (`SilentNoOpMutation` + `write_snapshot_field`) :
   « répondre succès sans rien faire » est exactement le même mécanisme
   qu'une neutralisation de mutation, appliqué ici par choix délibéré
   plutôt que pour auditer un oracle — aucune réimplémentation.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from agricam_reliable_agents.agent.llm_client import LLMClient
from agricam_reliable_agents.agent.loop import (
    DEFAULT_SUCCESS_CLAIM_EXTRACTOR,
    SuccessClaimExtractor,
    run_agent,
)
from agricam_reliable_agents.connector.oracle_mutation import (
    SilentNoOpMutation,
    write_snapshot_field,
)
from agricam_reliable_agents.mcp_tools.data_store import AgriCamDataStore
from agricam_reliable_agents.mcp_tools.tools import AgriCamTools
from agricam_reliable_agents.models.data_models import Task
from agricam_reliable_agents.reliability.harness import AgentRunner, StateSnapshotFn
from agricam_reliable_agents.security.guard import ConfirmationProvider
from agricam_reliable_agents.security.policy import SENSITIVE_TOOLS, ActionPolicy

_KNOWN_ENVIRONMENTS = ("test", "staging", "production")


class ProductionConfirmationRequiredError(RuntimeError):
    """Levée quand une campagne vise une connexion `production` sans confirmation explicite."""


def require_production_confirmation(environment: str, *, confirmed: bool) -> None:
    """
    Garde-fou pour toute campagne visant une connexion `production` :
    lève une exception CLAIRE si `confirmed` n'est pas explicitement vrai.

    `test` et `staging` ne sont jamais bloqués par cette fonction, quelle
    que soit `confirmed` — seule `production` déclenche l'exigence.
    """
    if environment == "production" and not confirmed:
        raise ProductionConfirmationRequiredError(
            "Cette campagne vise une connexion étiquetée 'production' : une confirmation "
            "explicite est requise (confirmed=True) avant de lancer le moindre essai — "
            "des effets de bord réels sont possibles."
        )


@dataclass(frozen=True, slots=True)
class DryRunLogEntry:
    """Une action à effet de bord interceptée en mode dry-run : journalisée, jamais appliquée pour de vrai."""

    tool_name: str
    arguments: dict[str, Any]
    result_returned_to_agent: dict[str, Any]


class DryRunAgriCamTools:
    """
    Enveloppe `AgriCamTools` (store `AgriCamDataStore` en mémoire) : les
    outils de `sensitive_tools` sont exécutés une fois pour obtenir un
    résultat authentique, puis leur effet réel est défait avant que
    l'appelant ne reprenne la main — l'agent voit un succès plausible,
    l'état réel ne bouge jamais. Chaque interception est journalisée dans
    `.log`, dans l'ordre où elle a eu lieu.
    """

    def __init__(self, tools: AgriCamTools, sensitive_tools: frozenset[str] = SENSITIVE_TOOLS) -> None:
        if not isinstance(tools.store, AgriCamDataStore):
            raise TypeError(
                "DryRunAgriCamTools ne fonctionne qu'avec le store en mémoire "
                "(AgriCamDataStore) : la neutralisation manipule ses attributs internes."
            )
        self._tools = tools
        self._sensitive_tools = sensitive_tools
        self.log: list[DryRunLogEntry] = []

    @property
    def store(self) -> AgriCamDataStore:
        return self._tools.store  # type: ignore[return-value]

    def dispatch(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if tool_name not in self._sensitive_tools:
            return self._tools.dispatch(tool_name, arguments)

        before = self.store.snapshot()
        result = self._tools.dispatch(tool_name, arguments)
        no_op = SilentNoOpMutation(tool_name=tool_name)
        after = self.store.snapshot()
        for key, value in before.items():
            if after.get(key) != value:
                write_snapshot_field(self.store, key, value)
        result = no_op.apply(tool_name, result)  # laisse un point d'extension cohérent avec oracle_mutation

        self.log.append(DryRunLogEntry(tool_name=tool_name, arguments=dict(arguments), result_returned_to_agent=result))
        return result


def agricam_runner_for_environment(
    llm_client: LLMClient,
    policy: ActionPolicy,
    environment: str,
    *,
    confirmed: bool = False,
    dry_run: bool = False,
    sensitive_tools: frozenset[str] = SENSITIVE_TOOLS,
    store_factory: Callable[[], AgriCamDataStore] | None = None,
    success_claim_extractor: SuccessClaimExtractor = DEFAULT_SUCCESS_CLAIM_EXTRACTOR,
    confirmation_provider: ConfirmationProvider | None = None,
) -> tuple[AgentRunner, StateSnapshotFn, DryRunAgriCamTools | AgriCamTools]:
    """
    Construit `(agent_runner, state_snapshot_fn, tools)` pour l'agent
    AgriCam de référence, en appliquant les deux garde-fous de ce module.

    `require_production_confirmation` est vérifiée IMMÉDIATEMENT, à la
    construction — avant même de créer le store ou le premier essai —
    pour qu'une tentative bloquée n'ait AUCUN effet de bord, pas même
    l'ouverture d'une session.

    `confirmation_provider` est un garde-fou DISTINCT (`security/guard.py`,
    non modifié) : `notify_farmer` exige déjà une confirmation humaine par
    défaut (`ActionPolicy.require_confirmation_for`), refusée par défaut
    (`_deny_all_confirmation`, fail-safe) si aucun fournisseur n'est
    injecté — indépendant du mode dry-run, qui neutralise l'EFFET d'un
    appel déjà AUTORISÉ, pas la décision de l'autoriser.

    Retourne aussi `tools` (le troisième élément) pour que l'appelant
    puisse inspecter `tools.log` après une campagne en mode dry-run.
    """
    if environment not in _KNOWN_ENVIRONMENTS:
        raise ValueError(f"environment doit être l'un de {_KNOWN_ENVIRONMENTS}, reçu {environment!r}.")
    require_production_confirmation(environment, confirmed=confirmed)

    if store_factory is None:
        def store_factory() -> AgriCamDataStore:
            store = AgriCamDataStore()
            store.seed_demo_data()
            return store

    store = store_factory()
    base_tools = AgriCamTools(store)
    tools: DryRunAgriCamTools | AgriCamTools = (
        DryRunAgriCamTools(base_tools, sensitive_tools) if dry_run else base_tools
    )

    kwargs: dict[str, Any] = {"success_claim_extractor": success_claim_extractor}
    if confirmation_provider is not None:
        kwargs["confirmation_provider"] = confirmation_provider

    def runner(task: Task, trial_id: int) -> Any:
        return run_agent(task, trial_id, llm_client, tools, policy, **kwargs)  # type: ignore[arg-type]

    return runner, lambda _task: store.snapshot(), tools
