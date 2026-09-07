"""
Campagne de fiabilité persistée, bout en bout (interface en ligne de commande).

La logique vit dans `agricam_reliable_agents.reliability.simulation`
(partagée avec l'API de l'interface de vérification) ; ce script ne fait
que lire les options, lancer la campagne et imprimer le résumé.

Deux modes :

- `--llm simulated` (défaut, aucune clé API) : LLM simulé qui suit un plan
  d'appels d'outils par tâche et se trompe d'identifiant avec probabilité
  `1 − p-step` à chaque étape, puis déclare quand même avoir réussi.
- `--llm anthropic` : `AnthropicLLMClient` réel (`ANTHROPIC_API_KEY`,
  modèle `AGRICAM_MODEL`). Chaque essai coûte de vrais appels API.

Le store métier est réinitialisé avant chaque essai, il vit donc par
défaut dans une base SQLite en mémoire (`--store-url`), distincte de la
base des résultats (`--database-url`, par défaut `AGRICAM_DATABASE_URL`
ou `sqlite:///agricam.db`).

Exécution :
    PYTHONPATH=src python scripts/run_campaign.py --n-trials 30 --p-step 0.9
"""

from __future__ import annotations

import argparse
import os

from agricam_reliable_agents.agent.llm_client import DEFAULT_MODEL, LLMClient
from agricam_reliable_agents.models.data_models import ReliabilityReport
from agricam_reliable_agents.persistence.engine import database_url_from_env
from agricam_reliable_agents.reliability.repository import ReliabilityRepository
from agricam_reliable_agents.reliability.simulation import (
    FINAL_CLAIM,
    PLANS,
    SYSTEM_PROMPT,
    TASKS,
    SimulatedLLMClient,
    run_simulated_campaign,
)

__all__ = ["FINAL_CLAIM", "PLANS", "TASKS", "SimulatedLLMClient", "main", "parse_args", "run_campaign"]


def build_anthropic_llm(model: str) -> LLMClient:
    from agricam_reliable_agents.agent.llm_client import AnthropicLLMClient

    return AnthropicLLMClient(model=model, system_prompt=SYSTEM_PROMPT)


def run_campaign(args: argparse.Namespace) -> tuple[int, list[ReliabilityReport], ReliabilityRepository]:
    repository = ReliabilityRepository.from_url(args.database_url)
    llm = build_anthropic_llm(args.model) if args.llm == "anthropic" else None
    campaign_id, reports = run_simulated_campaign(
        repository, name=args.name, n_trials=args.n_trials, p_step=args.p_step, seed=args.seed,
        store_url=args.store_url, llm=llm,
        model_label=args.model if llm is not None else None, notes=args.notes,
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
    if not (0.0 <= args.p_step <= 1.0):
        parser.error("--p-step doit être compris entre 0 et 1.")
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
