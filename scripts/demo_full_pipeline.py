"""
Démonstration bout-en-bout : agent simulé + harnais de fiabilité complet.

Ce script ne dépend d'aucune clé API : il utilise un agent simulé dont le
taux de succès par étape est paramétrable, pour illustrer numériquement
l'effondrement de pass^k documenté dans la spécification théorique.
Exécution : PYTHONPATH=src python3 scripts/demo_full_pipeline.py
"""

from __future__ import annotations

import random

from agricam_reliable_agents.models.data_models import AgentResult, Task, TaskComplexity
from agricam_reliable_agents.reliability.harness import HarnessConfig, evaluate_task

random.seed(42)  # reproductibilité de la démonstration


def run_demo_for_probability(p: float) -> None:
    # `state` représente l'état persistant du système (la base AgriCam).
    # `snapshot_call_index` distingue l'appel "avant" (pair) de l'appel
    # "après" (impair) : le harnais appelle snapshot_fn deux fois par
    # essai (avant puis après l'exécution de l'agent). Seul l'appel
    # "avant" doit réinitialiser l'état pour garantir l'indépendance
    # des essais ; l'appel "après" doit se contenter de LIRE l'état
    # laissé par l'agent, jamais le modifier.
    state = {"diagnostic.D-42.status": "pending"}
    call_index = {"n": 0}

    def snapshot(task: Task):
        is_before_call = (call_index["n"] % 2 == 0)
        call_index["n"] += 1
        if is_before_call:
            state["diagnostic.D-42.status"] = "pending"  # réinitialisation entre essais
        return dict(state)

    def agent(task: Task, trial_id: int) -> AgentResult:
        actually_succeeds = random.random() < p
        if actually_succeeds:
            state["diagnostic.D-42.status"] = "treated"
        # Sinon, l'état reste "pending" : l'agent n'a rien changé en réalité.
        return AgentResult(
            task_id=task.id, trial_id=trial_id,
            final_answer="Traitement appliqué avec succès.",
            tool_calls=(), declared_success=True,
            latency_ms=80.0, cost_usd=0.0015,
        )

    task = Task(
        id=f"DEMO-p{p}",
        prompt="Traiter le diagnostic D-42.",
        complexity=TaskComplexity.LEVEL_1,
        category="treatment",
        expected_state_delta={"diagnostic.D-42.status": "treated"},
        verification_query="q-demo",
    )

    config = HarnessConfig(n_trials=50, k_values=(1, 3, 5, 10))
    report = evaluate_task(task, agent, snapshot, config)

    print(f"\n=== Agent simulé avec probabilité de succès réelle p = {p} ===")
    print(f"n_trials         = {report.n_trials}")
    print(f"n_success        = {report.n_success}")
    print(f"p_hat            = {report.p_hat:.3f}")
    print(f"IC de Wilson 95% = [{report.wilson_ci[0]:.3f}, {report.wilson_ci[1]:.3f}]")
    for k, val in report.pass_k.items():
        print(f"pass^{k:<2}          = {val:.3f}")


if __name__ == "__main__":
    for probability in (0.95, 0.90, 0.80):
        run_demo_for_probability(probability)
