from agricam_reliable_agents.models.data_models import AgentResult, Task, TaskComplexity
from agricam_reliable_agents.reliability.harness import HarnessConfig, evaluate_task


def make_task() -> Task:
    return Task(
        id="T-DEMO",
        prompt="Marquer la parcelle comme traitée.",
        complexity=TaskComplexity.LEVEL_1,
        category="treatment",
        expected_state_delta={"status": "treated"},
        verification_query="q-demo",
    )


def test_evaluate_task_perfect_agent_yields_full_reliability():
    """Un agent qui réussit systématiquement doit produire p_hat=1.0
    et pass^k=1.0 pour tout k."""

    def perfect_agent(task: Task, trial_id: int) -> AgentResult:
        return AgentResult(
            task_id=task.id, trial_id=trial_id, final_answer="Fait.",
            tool_calls=(), declared_success=True,
            latency_ms=50.0, cost_usd=0.001,
        )

    # Simule un état qui passe de "pending" à "treated" à chaque essai
    call_count = {"n": 0}

    def snapshot(task: Task) -> dict:
        call_count["n"] += 1
        # Avant l'appel agent (n impair) -> pending ; après (n pair) -> treated
        return {"status": "pending" if call_count["n"] % 2 == 1 else "treated"}

    config = HarnessConfig(n_trials=10, k_values=(1, 5, 10))
    report = evaluate_task(make_task(), perfect_agent, snapshot, config)

    assert report.n_trials == 10
    assert report.n_success == 10
    assert report.p_hat == 1.0
    assert report.pass_k[10] == 1.0


def test_evaluate_task_flaky_agent_reflects_partial_reliability():
    """Un agent qui échoue réellement un essai sur deux (mais déclare
    toujours un succès) doit voir sa fiabilité s'effondrer fortement pour
    les k élevés — c'est le phénomène central que le harnais doit capturer."""

    state = {"status": "pending"}

    def snapshot(task: Task) -> dict:
        return dict(state)

    trial_tracker = {"n": -1}

    def flaky_agent_with_side_effect(task: Task, trial_id: int) -> AgentResult:
        trial_tracker["n"] += 1
        if trial_tracker["n"] % 2 == 0:
            state["status"] = "treated"
        else:
            state["status"] = "pending"
        return AgentResult(
            task_id=task.id, trial_id=trial_id, final_answer="Fait.",
            tool_calls=(), declared_success=True,
            latency_ms=50.0, cost_usd=0.001,
        )

    config = HarnessConfig(n_trials=20, k_values=(1, 10))
    report = evaluate_task(make_task(), flaky_agent_with_side_effect, snapshot, config)

    assert report.n_success == 10  # la moitié des essais réussissent réellement
    assert report.p_hat == 0.5
    # pass^10 doit être très inférieur à p_hat : c'est exactement le
    # phénomène d'effondrement de fiabilité documenté dans le projet.
    assert report.pass_k[10] < 0.01
    assert report.pass_k[10] == report.p_hat ** 10


def test_on_trial_callback_is_invoked_for_every_trial():
    calls = []

    def agent(task: Task, trial_id: int) -> AgentResult:
        return AgentResult(
            task_id=task.id, trial_id=trial_id, final_answer="ok",
            tool_calls=(), declared_success=True, latency_ms=1.0, cost_usd=0.0,
        )

    def snapshot(task: Task) -> dict:
        return {"status": "treated"}

    config = HarnessConfig(n_trials=5)
    evaluate_task(
        make_task(), agent, snapshot, config,
        on_trial=lambda result, verification: calls.append((result, verification)),
    )
    assert len(calls) == 5
