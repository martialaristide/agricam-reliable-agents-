"""
Tâche 5ter — audit de l'oracle par mutation ciblée.

Le test le plus important de ce fichier est
`test_incomplete_oracle_lets_the_silent_no_op_mutant_survive` : un audit
de mutation qui ne peut JAMAIS faire échouer un mutant ne prouve rien et
donnerait une fausse confiance — exactement le défaut que ce module doit
éliminer. Le scénario reproduit celui donné en exemple par le document de
référence (section 3.3) : un oracle qui ne vérifie que le statut du
diagnostic laisse survivre une mutation qui neutralise silencieusement
`notify_farmer`.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agricam_reliable_agents.agent.llm_client import LLMResponse, LLMToolCallRequest
from agricam_reliable_agents.connector.oracle_mutation import (
    MutatedAgriCamTools,
    PartialEffectMutation,
    SilentNoOpMutation,
    WrongValueMutation,
    mutated_agricam_runner,
    run_mutation_audit,
)
from agricam_reliable_agents.connector.oracle_repository import TaskOracle
from agricam_reliable_agents.mcp_tools.data_store import AgriCamDataStore
from agricam_reliable_agents.mcp_tools.sql_store import SqlAlchemyDataStore
from agricam_reliable_agents.mcp_tools.tools import AgriCamTools
from agricam_reliable_agents.models.data_models import Task, TaskComplexity
from agricam_reliable_agents.security.policy import ActionPolicy


class ScriptedLLMClient:
    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)

    def generate(self, messages, tool_schemas) -> LLMResponse:
        return self._responses.pop(0)


def treat_and_notify_task() -> Task:
    return Task(
        id="T-MUTATION", prompt="Traite D-42 et notifie F-001.", complexity=TaskComplexity.LEVEL_2,
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
# MutatedAgriCamTools : les trois mutations, au niveau du store
# ---------------------------------------------------------------------------


def test_only_sql_store_is_rejected():
    sql_tools = AgriCamTools(SqlAlchemyDataStore.from_url("sqlite://"))
    with pytest.raises(TypeError, match="store en mémoire"):
        MutatedAgriCamTools(sql_tools, [SilentNoOpMutation(tool_name="notify_farmer")])


def test_silent_no_op_neutralizes_the_real_effect_but_keeps_the_plausible_result():
    tools = make_tools()
    mutated = MutatedAgriCamTools(tools, [SilentNoOpMutation(tool_name="notify_farmer")])

    result = mutated.dispatch("notify_farmer", {"farmer_id": "F-001", "message": "Traité."})

    assert result == {"farmer_id": "F-001", "notified": True}  # texte de succès plausible, inchangé
    assert tools.store.snapshot()["farmer.F-001.notified_count"] == 0  # ...mais AUCUN effet réel


def test_silent_no_op_does_not_affect_unrelated_tools():
    tools = make_tools()
    mutated = MutatedAgriCamTools(tools, [SilentNoOpMutation(tool_name="notify_farmer")])

    mutated.dispatch("recommend_treatment", {"diagnostic_id": "D-42"})

    assert tools.store.snapshot()["diagnostic.D-42.status"] == "treated"  # effet réel, non ciblé


def test_partial_effect_neutralizes_every_other_call():
    tools = make_tools()
    mutation = PartialEffectMutation(tool_name="notify_farmer")
    mutated = MutatedAgriCamTools(tools, [mutation])

    mutated.dispatch("notify_farmer", {"farmer_id": "F-001", "message": "1"})  # rang 0 : neutralisé
    mutated.dispatch("notify_farmer", {"farmer_id": "F-001", "message": "2"})  # rang 1 : réel
    mutated.dispatch("notify_farmer", {"farmer_id": "F-001", "message": "3"})  # rang 2 : neutralisé

    assert tools.store.snapshot()["farmer.F-001.notified_count"] == 1  # un seul des trois a vraiment eu lieu


def test_wrong_value_writes_an_incorrect_but_plausible_value():
    tools = make_tools()
    mutation = WrongValueMutation(tool_name="recommend_treatment", field_key="diagnostic.D-42.status",
                                  wrong_value="failed")
    mutated = MutatedAgriCamTools(tools, [mutation])

    mutated.dispatch("recommend_treatment", {"diagnostic_id": "D-42"})

    assert tools.store.snapshot()["diagnostic.D-42.status"] == "failed"  # écrasé, pas "treated"
    # L'effet secondaire réel (décrément de stock) A bien eu lieu : seule LA VALEUR ciblée est fausse.
    assert tools.store.snapshot()["product.PRD-7.stock_qty"] == 24


def test_silent_no_op_on_recommend_treatment_reverts_both_its_real_effects():
    """recommend_treatment change DEUX champs en une fois (statut du
    diagnostic ET stock) : neutraliser l'outil doit défaire les deux,
    couvrant la branche 'product' de `_write_snapshot_field`."""
    tools = make_tools()
    mutated = MutatedAgriCamTools(tools, [SilentNoOpMutation(tool_name="recommend_treatment")])

    mutated.dispatch("recommend_treatment", {"diagnostic_id": "D-42"})

    snapshot = tools.store.snapshot()
    assert snapshot["diagnostic.D-42.status"] == "pending"
    assert snapshot["product.PRD-7.stock_qty"] == 25


def test_wrong_value_can_increase_farmer_notified_count():
    """Couvre la branche « agrandir » de `_write_snapshot_field` pour un
    compteur de notifications (valeur cible supérieure au nombre actuel
    de messages)."""
    tools = make_tools()
    mutation = WrongValueMutation(tool_name="recommend_treatment", field_key="farmer.F-001.notified_count",
                                  wrong_value=3)
    mutated = MutatedAgriCamTools(tools, [mutation])

    mutated.dispatch("recommend_treatment", {"diagnostic_id": "D-42"})

    assert tools.store.snapshot()["farmer.F-001.notified_count"] == 3


def test_unmatched_mutation_leaves_dispatch_unaffected():
    tools = make_tools()
    mutated = MutatedAgriCamTools(tools, [SilentNoOpMutation(tool_name="un_autre_outil")])
    mutated.dispatch("notify_farmer", {"farmer_id": "F-001", "message": "x"})
    assert tools.store.snapshot()["farmer.F-001.notified_count"] == 1


# ---------------------------------------------------------------------------
# mutated_agricam_runner : intégration avec la vraie boucle agent
# ---------------------------------------------------------------------------


def test_mutated_agricam_runner_produces_a_working_runner_and_snapshot():
    runner, snapshot_fn = mutated_agricam_runner(
        scripted_treat_and_notify(), full_scope_policy(), [SilentNoOpMutation(tool_name="notify_farmer")],
    )
    task = treat_and_notify_task()
    before = snapshot_fn(task)
    result = runner(task, trial_id=0)
    after = snapshot_fn(task)

    assert result.declared_success is True
    assert before["diagnostic.D-42.status"] == "pending"
    assert after["diagnostic.D-42.status"] == "treated"  # le traitement, non ciblé, a bien eu lieu
    assert after["farmer.F-001.notified_count"] == 0  # la notification, ciblée, a été neutralisée


# ---------------------------------------------------------------------------
# run_mutation_audit — LE test critique de la Tâche 5ter
# ---------------------------------------------------------------------------


def make_complete_oracle() -> TaskOracle:
    """Oracle COMPLET : couvre les TROIS effets réellement produits
    (statut, décrément de stock, notification)."""
    return TaskOracle(
        task_id="T-MUTATION", verification_method="approved_diff",
        baseline_state_diff={"diagnostic.D-42.status": "treated", "farmer.F-001.notified_count": 1,
                             "product.PRD-7.stock_qty": 24},
        approved_fields=("diagnostic.D-42.status", "farmer.F-001.notified_count", "product.PRD-7.stock_qty"),
        validated_by_human=True, validated_at=datetime.now(timezone.utc),
    )


def make_incomplete_oracle() -> TaskOracle:
    """Oracle DÉLIBÉRÉMENT incomplet : couvre les effets NORMAUX du
    traitement (statut + décrément de stock, l'un ET l'autre réellement
    produits par recommend_treatment) mais OMET la notification — comme
    dans l'exemple du document de référence. N'omettre QUE le statut
    aurait aussi fait remonter le décrément de stock comme un champ
    "inattendu" (unexpected_field_policy="flag" par défaut), ce qui
    masquerait la vraie omission recherchée ici derrière un signal sans
    rapport avec la mutation testée."""
    return TaskOracle(
        task_id="T-MUTATION", verification_method="approved_diff",
        baseline_state_diff={"diagnostic.D-42.status": "treated", "product.PRD-7.stock_qty": 24},
        approved_fields=("diagnostic.D-42.status", "product.PRD-7.stock_qty"),
        validated_by_human=True, validated_at=datetime.now(timezone.utc),
    )


def _factory(m):
    return mutated_agricam_runner(scripted_treat_and_notify(), full_scope_policy(), [m])


def test_complete_oracle_detects_the_silent_no_op_mutant():
    """Contrôle négatif : avec un oracle COMPLET, la même mutation est
    bien détectée (aucun mutant ne survit) — preuve que l'audit ne fait
    pas que « toujours dire que ça survit »."""
    oracle = make_complete_oracle()
    mutation = SilentNoOpMutation(tool_name="notify_farmer")

    report = run_mutation_audit(oracle, treat_and_notify_task(), _factory, [mutation])

    assert report.all_mutants_detected is True
    assert report.surviving_mutants == ()


def test_incomplete_oracle_lets_the_silent_no_op_mutant_survive():
    """LE test critique : preuve que l'audit fonctionne, pas seulement
    qu'il tourne sans erreur."""
    oracle = make_incomplete_oracle()
    mutation = SilentNoOpMutation(tool_name="notify_farmer")

    report = run_mutation_audit(oracle, treat_and_notify_task(), _factory, [mutation])

    assert report.all_mutants_detected is False
    assert len(report.surviving_mutants) == 1
    survivor = report.surviving_mutants[0]
    assert survivor.mutation_name == "SilentNoOpMutation"
    assert survivor.mutation is mutation
    assert survivor.trial_id == 0
    assert "farmer.F-001.notified_count" not in survivor.actual_state_delta  # l'effet neutralisé est invisible


def test_report_lists_each_surviving_mutant_explicitly_not_only_a_score():
    """Deux mutations, une seule doit survivre — le rapport doit nommer
    LAQUELLE, pas seulement dire « 1 sur 2 »."""
    oracle = make_incomplete_oracle()
    silent = SilentNoOpMutation(tool_name="notify_farmer")
    wrong_value = WrongValueMutation(tool_name="recommend_treatment", field_key="diagnostic.D-42.status",
                                     wrong_value="failed")

    report = run_mutation_audit(oracle, treat_and_notify_task(), _factory, [silent, wrong_value])

    assert report.n_mutations == 2
    assert len(report.surviving_mutants) == 1
    assert report.surviving_mutants[0].mutation_name == "SilentNoOpMutation"
    # WrongValueMutation altère diagnostic.D-42.status, qui EST approuvé : détecté, ne survit pas.
