"""Hook `before_trial` du harnais : appelé avant l'instantané « avant » de chaque essai."""

from __future__ import annotations

from agricam_reliable_agents.models.data_models import AgentResult, Task, TaskComplexity
from agricam_reliable_agents.reliability.harness import (
    HarnessConfig,
    evaluate_reliability,
    evaluate_task,
)


def make_task(task_id: str = "T-HOOK") -> Task:
    return Task(
        id=task_id, prompt="Traiter.", complexity=TaskComplexity.LEVEL_1, category="treatment",
        expected_state_delta={"status": "treated"}, verification_query="q",
    )


def test_before_trial_resets_state_so_every_trial_is_independent():
    """Sans réinitialisation, seul le premier essai verrait un changement
    d'état (pending → treated) ; avec `before_trial`, tous le voient."""
    state = {"status": "pending"}
    events: list[str] = []

    def before_trial(task: Task, trial_id: int) -> None:
        events.append(f"reset:{trial_id}")
        state["status"] = "pending"

    def snapshot(task: Task) -> dict:
        events.append(f"snapshot:{state['status']}")
        return dict(state)

    def agent(task: Task, trial_id: int) -> AgentResult:
        events.append("agent")
        state["status"] = "treated"
        return AgentResult(task_id=task.id, trial_id=trial_id, final_answer="Fait.",
                           tool_calls=(), declared_success=True, latency_ms=1.0, cost_usd=0.0)

    report = evaluate_task(make_task(), agent, snapshot, HarnessConfig(n_trials=3, k_values=(1,)),
                           before_trial=before_trial)

    assert report.n_success == 3
    assert events[:4] == ["reset:0", "snapshot:pending", "agent", "snapshot:treated"]
    assert events[4] == "reset:1"


def test_without_before_trial_only_first_trial_changes_state():
    state = {"status": "pending"}

    def agent(task: Task, trial_id: int) -> AgentResult:
        state["status"] = "treated"
        return AgentResult(task_id=task.id, trial_id=trial_id, final_answer="Fait.",
                           tool_calls=(), declared_success=True, latency_ms=1.0, cost_usd=0.0)

    report = evaluate_task(make_task(), agent, lambda task: dict(state),
                           HarnessConfig(n_trials=3, k_values=(1,)))
    assert report.n_success == 1


def test_evaluate_reliability_forwards_hook_to_every_task():
    seen: list[tuple[str, int]] = []

    def agent(task: Task, trial_id: int) -> AgentResult:
        return AgentResult(task_id=task.id, trial_id=trial_id, final_answer="Fait.",
                           tool_calls=(), declared_success=True, latency_ms=1.0, cost_usd=0.0)

    reports = evaluate_reliability(
        [make_task("A"), make_task("B")], agent, lambda task: {"status": "treated"},
        HarnessConfig(n_trials=2, k_values=(1,)),
        before_trial=lambda task, trial_id: seen.append((task.id, trial_id)),
    )
    assert [r.task_id for r in reports] == ["A", "B"]
    assert seen == [("A", 0), ("A", 1), ("B", 0), ("B", 1)]


def test_evaluate_reliability_uses_default_config_when_none():
    def agent(task: Task, trial_id: int) -> AgentResult:
        return AgentResult(task_id=task.id, trial_id=trial_id, final_answer="Fait.",
                           tool_calls=(), declared_success=True, latency_ms=1.0, cost_usd=0.0)

    [report] = evaluate_reliability([make_task()], agent, lambda task: {"status": "treated"})
    assert report.n_trials == HarnessConfig().n_trials
