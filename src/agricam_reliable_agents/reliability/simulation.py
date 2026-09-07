"""
Campagne de fiabilité simulée, bout en bout, réutilisable hors CLI.

La VRAIE boucle agent (`run_agent`) tourne avec les VRAIS outils MCP sur un
store SQL, sous la garde de sécurité, et chaque essai, rapport et incident
est archivé dans le dépôt (`ReliabilityRepository`). Seul le LLM est
simulé (ou remplacé par `AnthropicLLMClient` si l'appelant en fournit un).

LLM simulé
----------
`SimulatedLLMClient` suit un plan d'appels d'outils par tâche. À chaque
étape il a une probabilité `p_step` de faire l'appel correct ; sinon il
émet un identifiant erroné (l'outil échoue, ou la garde bloque l'appel
hors périmètre) puis conclut quand même en déclarant un succès. Le
Success Verifier le rattrape : c'est la sur-confiance mesurée par le
harnais, et plus la tâche est longue, plus elle est fréquente.

Ce qui est mesuré est l'ÉTAT RÉEL, pas le respect du plan : une dérive sur
une étape de lecture placée après la dernière écriture (ex. la
re-vérification finale du stock de `T3-full-workflow`) laisse l'état
conforme et l'essai est compté réussi. Le taux attendu de T3 est donc
`p_step^5` (cinq étapes avant et jusqu'à la dernière écriture), pas
`p_step^6`.

Ce module est utilisé par `scripts/run_campaign.py` (CLI) et par l'API
de l'interface de vérification (lancement d'une campagne depuis l'écran
Campagnes).
"""

from __future__ import annotations

import random
from typing import Any

from agricam_reliable_agents.agent.llm_client import (
    LLMClient,
    LLMResponse,
    LLMToolCallRequest,
)
from agricam_reliable_agents.agent.loop import run_agent
from agricam_reliable_agents.mcp_tools.sql_store import SqlAlchemyDataStore
from agricam_reliable_agents.mcp_tools.tools import AgriCamTools
from agricam_reliable_agents.models.data_models import (
    AgentResult,
    ReliabilityReport,
    Task,
    TaskComplexity,
    ToolCall,
)
from agricam_reliable_agents.reliability.campaign import run_persisted_campaign
from agricam_reliable_agents.reliability.harness import HarnessConfig
from agricam_reliable_agents.reliability.repository import ReliabilityRepository
from agricam_reliable_agents.security.policy import ActionPolicy

# ---------------------------------------------------------------------------
# Tâches de référence : trois niveaux de complexité sur le même scénario métier
# ---------------------------------------------------------------------------

_TREATED = {"diagnostic.D-42.status": "treated", "product.PRD-7.stock_qty": 24}
_TREATED_AND_NOTIFIED = {**_TREATED, "farmer.F-001.notified_count": 1}
_NOTIFY_MESSAGE = "Traitement du mildiou appliqué sur la parcelle P-003."

TASKS: tuple[Task, ...] = (
    Task(
        id="T1-treat",
        prompt="Applique le traitement recommandé pour le diagnostic D-42.",
        complexity=TaskComplexity.LEVEL_1, category="treatment",
        expected_state_delta=_TREATED, verification_query="q-t1",
    ),
    Task(
        id="T2-treat-notify",
        prompt=(
            "Consulte les capteurs de la parcelle P-003, applique le traitement "
            "recommandé pour le diagnostic D-42, puis notifie l'exploitant F-001 "
            "par un message court."
        ),
        complexity=TaskComplexity.LEVEL_2, category="treatment",
        expected_state_delta=_TREATED_AND_NOTIFIED, verification_query="q-t2",
    ),
    Task(
        id="T3-full-workflow",
        prompt=(
            "Pour la parcelle P-003 : lis les capteurs, consulte l'historique des "
            "diagnostics, vérifie le stock du produit PRD-7, applique le traitement "
            "recommandé pour le diagnostic D-42, notifie l'exploitant F-001 par un "
            "message court, puis re-vérifie le stock et confirme."
        ),
        complexity=TaskComplexity.LEVEL_3, category="treatment",
        expected_state_delta=_TREATED_AND_NOTIFIED, verification_query="q-t3",
    ),
)

_SENSORS = ("get_sensor_data", {"parcel_id": "P-003"})
_HISTORY = ("get_diagnostic_history", {"parcel_id": "P-003"})
_STOCK = ("check_marketplace_stock", {"product_id": "PRD-7"})
_TREAT = ("recommend_treatment", {"diagnostic_id": "D-42"})
_NOTIFY = ("notify_farmer", {"farmer_id": "F-001", "message": _NOTIFY_MESSAGE})

PLANS: dict[str, tuple[tuple[str, dict[str, Any]], ...]] = {
    "T1-treat": (_TREAT,),
    "T2-treat-notify": (_SENSORS, _TREAT, _NOTIFY),
    "T3-full-workflow": (_SENSORS, _HISTORY, _STOCK, _TREAT, _NOTIFY, _STOCK),
}

FINAL_CLAIM = "Traitement appliqué et notification envoyée à l'exploitant."

SYSTEM_PROMPT = (
    "Tu es l'agent AgriCam. Tu accomplis des tâches agricoles avec les outils "
    "fournis (capteurs, diagnostics, marketplace, notification). Fais chaque "
    "étape demandée dans l'ordre, une seule action à la fois, puis conclus en "
    "une phrase indiquant clairement ce qui a été fait ou ce qui a échoué."
)

DEFAULT_POLICY = ActionPolicy(
    allowed_farmer_ids=frozenset({"F-001"}), allowed_parcel_ids=frozenset({"P-003"}),
)


class SimulatedLLMClient:
    """LLM simulé : suit le plan de la tâche, se trompe d'identifiant avec
    probabilité `1 - p_step` à chaque étape, puis déclare toujours réussir."""

    def __init__(self, p_step: float, rng: random.Random) -> None:
        if not (0.0 <= p_step <= 1.0):
            raise ValueError("p_step doit être compris entre 0 et 1.")
        self._p_step = p_step
        self._rng = rng
        self._plan: tuple[tuple[str, dict[str, Any]], ...] = ()
        self._step = 0
        self._derailed = False

    def start_trial(self, task: Task) -> None:
        self._plan = PLANS[task.id]
        self._step = 0
        self._derailed = False

    def generate(self, messages: list[dict[str, Any]], tool_schemas: list[dict[str, Any]]) -> LLMResponse:
        if self._derailed or self._step >= len(self._plan):
            return LLMResponse(tool_call=None, final_text=FINAL_CLAIM, input_tokens=600, output_tokens=30)
        name, arguments = self._plan[self._step]
        self._step += 1
        if self._rng.random() >= self._p_step:
            arguments = {k: (f"{v[:2]}404" if isinstance(v, str) and "-" in v else v)
                         for k, v in arguments.items()}
            self._derailed = True  # après l'erreur, l'agent conclut quand même
        return LLMResponse(
            tool_call=LLMToolCallRequest(id=f"sim-{self._step}", name=name, arguments=arguments),
            final_text=None, input_tokens=500, output_tokens=40,
        )


def run_simulated_campaign(
    repository: ReliabilityRepository,
    *,
    name: str,
    n_trials: int,
    p_step: float = 0.9,
    seed: int = 42,
    store_url: str = "sqlite://",
    llm: LLMClient | None = None,
    model_label: str | None = None,
    notes: str | None = None,
    tasks: tuple[Task, ...] = TASKS,
    k_values: tuple[int, ...] = (1, 3, 5, 10),
    campaign_id: int | None = None,
) -> tuple[int, list[ReliabilityReport]]:
    """
    Ouvre une campagne (ou reprend `campaign_id` déjà ouverte par l'appelant),
    exécute `tasks` × `n_trials` avec la vraie boucle agent et archive tout.
    Retourne (campaign_id, rapports).

    `llm` permet de substituer un client réel (`AnthropicLLMClient`) au
    LLM simulé ; `p_step` et `seed` ne servent alors pas.
    """
    if n_trials < 1:
        raise ValueError("n_trials doit être >= 1.")
    store = SqlAlchemyDataStore.from_url(store_url)
    tools = AgriCamTools(store)
    client: LLMClient = llm if llm is not None else SimulatedLLMClient(p_step, random.Random(seed))
    label = model_label or (f"simulated-p{p_step}" if llm is None else "llm")

    if campaign_id is None:
        campaign_id = repository.start_campaign(name, model=label, notes=notes)
    incident_sink = repository.incident_sink(campaign_id)

    def confirm(call: ToolCall) -> bool:
        return True  # confirmation humaine simulée : toujours accordée

    def before_trial(task: Task, trial_id: int) -> None:
        store.reset()
        store.seed_demo_data()

    def agent_runner(task: Task, trial_id: int) -> AgentResult:
        if isinstance(client, SimulatedLLMClient):
            client.start_trial(task)
        return run_agent(
            task, trial_id, client, tools, DEFAULT_POLICY,
            confirmation_provider=confirm, incident_sink=incident_sink,
        )

    config = HarnessConfig(n_trials=n_trials, k_values=k_values)
    reports = run_persisted_campaign(
        repository, campaign_id, tasks, agent_runner, lambda task: store.snapshot(),
        config, before_trial=before_trial,
    )
    return campaign_id, reports
