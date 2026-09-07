"""
Tâche 6 (Phase D) — environnements et mode dry-run.

Critère d'acceptation le plus important : un test prouve qu'une action
sensible en mode dry-run ne modifie AUCUN état réel, avec une assertion
explicite sur l'état avant/après (`test_dry_run_campaign_never_changes_real_state_end_to_end`,
qui réutilise le harnais et le Success Verifier existants, non modifiés).
"""

from __future__ import annotations

import pytest

from agricam_reliable_agents.agent.llm_client import LLMResponse, LLMToolCallRequest
from agricam_reliable_agents.connector.environment import (
    DryRunAgriCamTools,
    ProductionConfirmationRequiredError,
    agricam_runner_for_environment,
    require_production_confirmation,
)
from agricam_reliable_agents.mcp_tools.data_store import AgriCamDataStore
from agricam_reliable_agents.mcp_tools.sql_store import SqlAlchemyDataStore
from agricam_reliable_agents.mcp_tools.tools import AgriCamTools
from agricam_reliable_agents.models.data_models import Task, TaskComplexity
from agricam_reliable_agents.reliability.harness import HarnessConfig, evaluate_task
from agricam_reliable_agents.security.policy import ActionPolicy


class ScriptedLLMClient:
    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self.call_count = 0

    def generate(self, messages, tool_schemas) -> LLMResponse:
        self.call_count += 1
        return self._responses.pop(0)


def treat_and_notify_task() -> Task:
    return Task(
        id="T-ENV", prompt="Traite D-42 et notifie F-001.", complexity=TaskComplexity.LEVEL_2,
        category="treatment", expected_state_delta={"diagnostic.D-42.status": "treated"}, verification_query="q",
    )


def scripted_treat_and_notify() -> ScriptedLLMClient:
    return ScriptedLLMClient([
        LLMResponse(tool_call=LLMToolCallRequest("c1", "recommend_treatment", {"diagnostic_id": "D-42"}),
                    final_text=None),
        LLMResponse(tool_call=LLMToolCallRequest("c2", "notify_farmer",
                                                 {"farmer_id": "F-001", "message": "Traité."}),
                    final_text=None),
        LLMResponse(tool_call=None, final_text="Traitement appliqué et notification envoyée avec succès."),
    ])


def make_tools() -> AgriCamTools:
    store = AgriCamDataStore()
    store.seed_demo_data()
    return AgriCamTools(store)


def full_scope_policy() -> ActionPolicy:
    return ActionPolicy(allowed_farmer_ids=frozenset({"F-001"}), allowed_parcel_ids=frozenset({"P-003"}))


# ---------------------------------------------------------------------------
# require_production_confirmation
# ---------------------------------------------------------------------------


def test_production_without_confirmation_is_blocked():
    with pytest.raises(ProductionConfirmationRequiredError, match="production"):
        require_production_confirmation("production", confirmed=False)


def test_production_with_confirmation_passes():
    require_production_confirmation("production", confirmed=True)  # ne lève pas


@pytest.mark.parametrize("environment", ["test", "staging"])
@pytest.mark.parametrize("confirmed", [True, False])
def test_non_production_environments_never_block(environment: str, confirmed: bool):
    require_production_confirmation(environment, confirmed=confirmed)  # ne lève jamais


# ---------------------------------------------------------------------------
# DryRunAgriCamTools
# ---------------------------------------------------------------------------


def test_only_memory_store_is_accepted():
    sql_tools = AgriCamTools(SqlAlchemyDataStore.from_url("sqlite://"))
    with pytest.raises(TypeError, match="store en mémoire"):
        DryRunAgriCamTools(sql_tools)


def test_sensitive_tool_is_intercepted_state_unaffected_result_plausible():
    tools = make_tools()
    dry_run = DryRunAgriCamTools(tools)

    result = dry_run.dispatch("notify_farmer", {"farmer_id": "F-001", "message": "Traité."})

    assert result == {"farmer_id": "F-001", "notified": True}
    assert tools.store.snapshot()["farmer.F-001.notified_count"] == 0
    assert len(dry_run.log) == 1
    assert dry_run.log[0].tool_name == "notify_farmer"
    assert dry_run.log[0].arguments == {"farmer_id": "F-001", "message": "Traité."}


def test_sensitive_tool_with_two_effects_leaves_both_unaffected():
    tools = make_tools()
    dry_run = DryRunAgriCamTools(tools)
    dry_run.dispatch("recommend_treatment", {"diagnostic_id": "D-42"})

    snapshot = tools.store.snapshot()
    assert snapshot["diagnostic.D-42.status"] == "pending"
    assert snapshot["product.PRD-7.stock_qty"] == 25


def test_non_sensitive_tool_passes_through_and_is_not_logged():
    tools = make_tools()
    dry_run = DryRunAgriCamTools(tools)

    result = dry_run.dispatch("check_marketplace_stock", {"product_id": "PRD-7"})

    assert result == {"product_id": "PRD-7", "name": "Fongicide bio FB-12", "stock_qty": 25}
    assert dry_run.log == []


def test_custom_sensitive_tools_set_can_narrow_or_widen_interception():
    tools = make_tools()
    dry_run = DryRunAgriCamTools(tools, sensitive_tools=frozenset({"check_marketplace_stock"}))

    dry_run.dispatch("check_marketplace_stock", {"product_id": "PRD-7"})  # désormais "sensible"
    dry_run.dispatch("notify_farmer", {"farmer_id": "F-001", "message": "x"})  # plus considéré sensible ici

    assert len(dry_run.log) == 1 and dry_run.log[0].tool_name == "check_marketplace_stock"
    assert tools.store.snapshot()["farmer.F-001.notified_count"] == 1  # exécuté pour de vrai


# ---------------------------------------------------------------------------
# agricam_runner_for_environment
# ---------------------------------------------------------------------------


def test_unknown_environment_is_rejected():
    with pytest.raises(ValueError, match="environment"):
        agricam_runner_for_environment(scripted_treat_and_notify(), full_scope_policy(), "prod")


def test_production_without_confirmation_blocks_before_anything_runs():
    llm = scripted_treat_and_notify()
    with pytest.raises(ProductionConfirmationRequiredError):
        agricam_runner_for_environment(llm, full_scope_policy(), "production", confirmed=False)
    assert llm.call_count == 0  # aucun appel au modèle : bloqué avant le premier essai


def test_production_with_confirmation_runs_normally():
    runner, snapshot_fn, _tools = agricam_runner_for_environment(
        scripted_treat_and_notify(), full_scope_policy(), "production", confirmed=True,
    )
    task = treat_and_notify_task()
    result = runner(task, trial_id=0)
    assert result.declared_success is True
    assert snapshot_fn(task)["diagnostic.D-42.status"] == "treated"  # effet réel : confirmé, pas dry-run


@pytest.mark.parametrize("environment", ["test", "staging"])
def test_non_production_environments_never_require_confirmation(environment: str):
    runner, snapshot_fn, _ = agricam_runner_for_environment(
        scripted_treat_and_notify(), full_scope_policy(), environment, confirmed=False,
    )
    task = treat_and_notify_task()
    runner(task, trial_id=0)
    assert snapshot_fn(task)["diagnostic.D-42.status"] == "treated"


def test_dry_run_combines_with_any_environment():
    runner, snapshot_fn, tools = agricam_runner_for_environment(
        scripted_treat_and_notify(), full_scope_policy(), "staging", dry_run=True,
        confirmation_provider=lambda call: True,  # autorise notify_farmer à être tenté (guard distinct du dry-run)
    )
    task = treat_and_notify_task()
    result = runner(task, trial_id=0)

    assert result.declared_success is True  # l'agent croit avoir réussi...
    assert snapshot_fn(task)["diagnostic.D-42.status"] == "pending"  # ...mais rien n'a réellement changé
    assert len(tools.log) == 2  # recommend_treatment + notify_farmer, tous deux interceptés


# ---------------------------------------------------------------------------
# Intégration bout en bout : le critère d'acceptation de la Tâche 6
# ---------------------------------------------------------------------------


def test_dry_run_campaign_never_changes_real_state_end_to_end():
    """LE test le plus important : une action sensible en mode dry-run ne
    modifie AUCUN état réel, prouvé par le harnais ET par une assertion
    directe avant/après sur l'état — même principe que le Success Verifier."""
    runner, snapshot_fn, tools = agricam_runner_for_environment(
        scripted_treat_and_notify(), full_scope_policy(), "production", confirmed=True, dry_run=True,
        confirmation_provider=lambda call: True,
    )
    task = treat_and_notify_task()

    state_before = snapshot_fn(task)
    result = runner(task, trial_id=0)
    state_after = snapshot_fn(task)

    assert result.declared_success is True  # sur-confiance : l'agent croit avoir réussi
    assert state_before == state_after  # AUCUN état n'a bougé, assertion directe avant/après
    assert len(tools.log) == 2

    # Et via le harnais existant (non modifié), sur une instance fraîche (le
    # LLM scripté ci-dessus est déjà épuisé après son unique essai direct) :
    # le Success Verifier détecte la sur-confiance sans qu'aucun état réel n'ait bougé.
    harness_runner, harness_snapshot_fn, _ = agricam_runner_for_environment(
        scripted_treat_and_notify(), full_scope_policy(), "production", confirmed=True, dry_run=True,
        confirmation_provider=lambda call: True,
    )
    report = evaluate_task(
        task, harness_runner, harness_snapshot_fn, HarnessConfig(n_trials=1, k_values=(1,)),
    )
    assert report.n_success == 0
    assert report.p_hat == 0.0
