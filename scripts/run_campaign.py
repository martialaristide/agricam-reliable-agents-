"""
Campagne de fiabilité persistée, bout en bout.

Contrairement à `demo_full_pipeline.py` (agent simulé « à plat », sans
outils), ce script fait tourner la VRAIE boucle agent (`run_agent`) avec
les VRAIS outils MCP sur un store SQL, sous la garde de sécurité, et
archive chaque essai, chaque rapport et chaque incident dans le dépôt
(`ReliabilityRepository`) — la source de données du dashboard.

Deux modes :

- `--llm simulated` (défaut, aucune clé API) : un LLM simulé suit un plan
  d'appels d'outils par tâche. À chaque étape il a une probabilité
  `--p-step` de faire l'appel correct ; sinon il émet un identifiant erroné
  (l'outil échoue, ou la garde bloque l'appel hors périmètre) puis conclut
  quand même en déclarant un succès. Le Success Verifier le rattrape :
  c'est la sur-confiance mesurée par le harnais, et plus la tâche est
  longue, plus elle est fréquente.
- `--llm anthropic` : `AnthropicLLMClient` réel (`ANTHROPIC_API_KEY`,
  modèle `AGRICAM_MODEL`). Chaque essai coûte de vrais appels API.

Le store métier est réinitialisé avant chaque essai (`before_trial`), il
vit donc par défaut dans une base SQLite en mémoire (`--store-url`),
distincte de la base des résultats (`--database-url`, par défaut
`AGRICAM_DATABASE_URL` ou `sqlite:///agricam.db`).

Exécution :
    PYTHONPATH=src python scripts/run_campaign.py --n-trials 30 --p-step 0.9
"""

from __future__ import annotations

import argparse
import os
import random
from typing import Any

from agricam_reliable_agents.agent.llm_client import (
    DEFAULT_MODEL,
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
from agricam_reliable_agents.persistence.engine import database_url_from_env
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


# ---------------------------------------------------------------------------
# Campagne
# ---------------------------------------------------------------------------

def build_llm(kind: str, p_step: float, seed: int, model: str) -> LLMClient:
    if kind == "simulated":
        return SimulatedLLMClient(p_step, random.Random(seed))
    from agricam_reliable_agents.agent.llm_client import AnthropicLLMClient

    return AnthropicLLMClient(model=model, system_prompt=SYSTEM_PROMPT)


def run_campaign(args: argparse.Namespace) -> tuple[int, list[ReliabilityReport], ReliabilityRepository]:
    store = SqlAlchemyDataStore.from_url(args.store_url)
    tools = AgriCamTools(store)
    repository = ReliabilityRepository.from_url(args.database_url)
    llm = build_llm(args.llm, args.p_step, args.seed, args.model)

    model_label = args.model if args.llm == "anthropic" else f"simulated-p{args.p_step}"
    campaign_id = repository.start_campaign(args.name, model=model_label, notes=args.notes)
    incident_sink = repository.incident_sink(campaign_id)
    policy = ActionPolicy(allowed_farmer_ids=frozenset({"F-001"}), allowed_parcel_ids=frozenset({"P-003"}))

    def confirm(call: ToolCall) -> bool:
        return True  # confirmation humaine simulée : toujours accordée

    def before_trial(task: Task, trial_id: int) -> None:
        store.reset()
        store.seed_demo_data()

    def agent_runner(task: Task, trial_id: int) -> AgentResult:
        if isinstance(llm, SimulatedLLMClient):
            llm.start_trial(task)
        return run_agent(
            task, trial_id, llm, tools, policy,
            confirmation_provider=confirm, incident_sink=incident_sink,
        )

    config = HarnessConfig(n_trials=args.n_trials, k_values=(1, 3, 5, 10))
    reports = run_persisted_campaign(
        repository, campaign_id, TASKS, agent_runner, lambda task: store.snapshot(),
        config, before_trial=before_trial,
    )
    return campaign_id, reports, repository


def print_summary(campaign_id: int, reports: list[ReliabilityReport], repository: ReliabilityRepository,
                  database_url: str) -> None:
    tasks_by_id = {t.id: t for t in TASKS}
    print(f"\nCampagne #{campaign_id} archivée dans {database_url}")
    header = (f"{'Tâche':<18} {'Complexité':<10} {'n':>3} {'succès':>6} {'p̂':>6} "
              f"{'IC Wilson 95 %':>16} {'pass^1':>7} {'pass^3':>7} {'pass^5':>7} {'pass^10':>8} {'sur-conf.':>9}")
    print(header)
    print("-" * len(header))
    total_cost = 0.0
    for report in reports:
        trials = repository.list_trials(campaign_id, report.task_id)
        overconfident = sum(t.overconfidence_detected for t in trials)
        total_cost += sum(t.cost_usd for t in trials)
        low, high = report.wilson_ci
        print(
            f"{report.task_id:<18} {tasks_by_id[report.task_id].complexity.value:<10} "
            f"{report.n_trials:>3} {report.n_success:>6} {report.p_hat:>6.3f} "
            f"{f'[{low:.3f}, {high:.3f}]':>16} "
            f"{report.pass_k[1]:>7.3f} {report.pass_k[3]:>7.3f} {report.pass_k[5]:>7.3f} "
            f"{report.pass_k[10]:>8.3f} {f'{overconfident}/{report.n_trials}':>9}"
        )
    incidents = repository.list_incidents(campaign_id)
    print(f"\nIncidents de sécurité journalisés : {len(incidents)} "
          f"(bloqués : {sum(i.blocked for i in incidents)})")
    print(f"Coût estimé de la campagne : {total_cost:.4f} USD")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Campagne de fiabilité persistée AgriCam.")
    parser.add_argument("--llm", choices=("simulated", "anthropic"), default="simulated")
    parser.add_argument("--n-trials", type=int, default=30)
    parser.add_argument("--p-step", type=float, default=0.9,
                        help="probabilité de réussite de chaque étape (LLM simulé)")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--model", default=os.environ.get("AGRICAM_MODEL", DEFAULT_MODEL))
    parser.add_argument("--name", default=None, help="nom de campagne (défaut : dérivé des options)")
    parser.add_argument("--notes", default=None)
    parser.add_argument("--database-url", default=database_url_from_env(),
                        help="base des résultats (défaut : AGRICAM_DATABASE_URL ou sqlite:///agricam.db)")
    parser.add_argument("--store-url", default="sqlite://",
                        help="base métier AgriCam, réinitialisée à chaque essai (défaut : mémoire)")
    args = parser.parse_args(argv)
    if args.n_trials < 1:
        parser.error("--n-trials doit être >= 1.")
    if args.name is None:
        args.name = (f"{args.llm}-p{args.p_step}-n{args.n_trials}" if args.llm == "simulated"
                     else f"{args.model}-n{args.n_trials}")
    return args


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    campaign_id, reports, repository = run_campaign(args)
    print_summary(campaign_id, reports, repository, args.database_url)


if __name__ == "__main__":
    main()
