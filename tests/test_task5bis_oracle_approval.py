"""
Tâche 5bis — workflow d'oracle par approbation de diff (approval testing).

Le test le plus important de ce fichier est
`test_unexpected_side_effect_slips_past_old_verify_but_is_caught_by_the_new_oracle`,
qui appelle les DEUX mécanismes (`verifier.success_verifier.verify`,
inchangé, et `verify_against_approved_oracle`, nouveau) sur EXACTEMENT
les mêmes données et montre qu'ils rendent des verdicts différents —
la preuve la plus directe possible que le nouveau mécanisme corrige un
angle mort réel du mécanisme historique, pas supposé.

Note de conception sur le scénario choisi : `fuzzy_match` (mécanisme
historique) ignore, PAR CONCEPTION, tout champ du diff observé qui n'est
pas dans `expected_state_delta` (cf. sa docstring : « actual_delta peut
contenir des changements supplémentaires non prévus... sans que cela
invalide le succès »). L'angle mort qu'`unexpected_field_policy="flag"`
corrige est donc précisément un EFFET DE BORD SUPPLÉMENTAIRE non prévu
(ex. une notification envoyée alors qu'elle n'aurait pas dû l'être), pas
un effet manquant — un champ manquant est déjà détecté à l'identique par
les deux mécanismes (`fuzzy_match` et la comparaison aux champs approuvés
ci-dessous), donc un scénario de champ manquant ne différencierait pas
les deux mécanismes et ne prouverait rien de nouveau.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agricam_reliable_agents.connector.oracle_approval import (
    BaselineCapture,
    approve_baseline,
    capture_baseline,
    verify_against_approved_oracle,
)
from agricam_reliable_agents.connector.oracle_repository import (
    OracleRepository,
    TaskOracle,
)
from agricam_reliable_agents.models.data_models import AgentResult, Task, TaskComplexity
from agricam_reliable_agents.reliability.repository import ReliabilityRepository
from agricam_reliable_agents.verifier.success_verifier import verify


def make_task(task_id: str = "T-APPROVAL") -> Task:
    return Task(
        id=task_id, prompt="Traite le diagnostic D-42 et notifie l'exploitant.",
        complexity=TaskComplexity.LEVEL_2, category="treatment",
        expected_state_delta={"diagnostic.D-42.status": "treated"}, verification_query="q",
    )


def make_result(task_id: str, trial_id: int = 0, *, declared: bool = True) -> AgentResult:
    return AgentResult(task_id=task_id, trial_id=trial_id, final_answer="Fait.", tool_calls=(),
                       declared_success=declared, latency_ms=1.0, cost_usd=0.0)


@pytest.fixture
def oracle_repo(tmp_path) -> OracleRepository:
    reliability = ReliabilityRepository.from_url(f"sqlite:///{(tmp_path / 'o.db').as_posix()}")
    reliability.record_task(make_task())
    return OracleRepository(reliability.engine)


# ---------------------------------------------------------------------------
# capture_baseline
# ---------------------------------------------------------------------------


def test_capture_baseline_runs_the_reference_agent_once_and_computes_the_full_diff():
    state = {"diagnostic.D-42.status": "pending", "farmer.F-001.notified_count": 0}
    call_count = {"n": 0}

    def reference_runner(task: Task, trial_id: int) -> AgentResult:
        call_count["n"] += 1
        state["diagnostic.D-42.status"] = "treated"
        state["farmer.F-001.notified_count"] = 1
        return make_result(task.id, trial_id)

    baseline = capture_baseline(make_task(), reference_runner, lambda t: dict(state))

    assert call_count["n"] == 1  # UNE seule exécution
    assert baseline.task_id == "T-APPROVAL"
    assert baseline.diff == {"diagnostic.D-42.status": "treated", "farmer.F-001.notified_count": 1}
    assert baseline.state_before == {"diagnostic.D-42.status": "pending", "farmer.F-001.notified_count": 0}
    assert baseline.reference_result.declared_success is True


def test_capture_baseline_never_persists_anything_by_itself(oracle_repo: OracleRepository):
    """Capturer n'est pas approuver : aucun TaskOracle ne doit exister
    tant que approve_baseline n'a pas été appelée explicitement."""
    state = {"x": 0}
    capture_baseline(make_task(), lambda t, i: (state.__setitem__("x", 1), make_result(t.id, i))[1],
                     lambda t: dict(state))
    assert oracle_repo.get_oracle("T-APPROVAL") is None


# ---------------------------------------------------------------------------
# approve_baseline
# ---------------------------------------------------------------------------


def make_baseline(diff: dict, task_id: str = "T-APPROVAL") -> BaselineCapture:
    return BaselineCapture(task_id=task_id, trial_id=0, state_before={}, state_after=dict(diff),
                           diff=dict(diff), reference_result=make_result(task_id))


def test_approve_baseline_persists_a_validated_oracle(oracle_repo: OracleRepository):
    baseline = make_baseline({"diagnostic.D-42.status": "treated", "farmer.F-001.notified_count": 1})
    oracle = approve_baseline(oracle_repo, baseline, {"diagnostic.D-42.status", "farmer.F-001.notified_count"})

    assert oracle.validated_by_human is True
    assert oracle.validated_at is not None
    assert oracle.verification_method == "approved_diff"
    assert oracle.inferred is False
    assert set(oracle.approved_fields) == {"diagnostic.D-42.status", "farmer.F-001.notified_count"}
    assert oracle.baseline_state_diff == baseline.diff

    fetched = oracle_repo.get_oracle("T-APPROVAL")
    assert fetched is not None and fetched.validated_by_human is True


def test_approve_baseline_rejects_a_field_absent_from_the_captured_diff(oracle_repo: OracleRepository):
    baseline = make_baseline({"diagnostic.D-42.status": "treated"})
    with pytest.raises(ValueError, match="absents du diff capturé"):
        approve_baseline(oracle_repo, baseline, {"diagnostic.D-42.status", "un_champ_jamais_observe"})


def test_approve_baseline_accepts_a_strict_subset_of_the_diff(oracle_repo: OracleRepository):
    """Un humain peut choisir de n'approuver qu'une PARTIE du diff observé
    (ex. ignorer un timestamp technique) — seuls les champs absents du
    diff, jamais un sous-ensemble, sont rejetés."""
    baseline = make_baseline({"diagnostic.D-42.status": "treated", "internal.updated_at": "2026-01-01"})
    oracle = approve_baseline(oracle_repo, baseline, {"diagnostic.D-42.status"})
    assert oracle.approved_fields == ("diagnostic.D-42.status",)


def test_approve_baseline_defaults_unexpected_field_policy_to_flag(oracle_repo: OracleRepository):
    baseline = make_baseline({"x": 1})
    oracle = approve_baseline(oracle_repo, baseline, {"x"})
    assert oracle.unexpected_field_policy == "flag"


# ---------------------------------------------------------------------------
# verify_against_approved_oracle
# ---------------------------------------------------------------------------


def approved_oracle(approved_fields: tuple, baseline: dict, policy: str = "flag") -> TaskOracle:
    return TaskOracle(
        task_id="T-APPROVAL", verification_method="approved_diff", baseline_state_diff=baseline,
        approved_fields=approved_fields, unexpected_field_policy=policy,
        validated_by_human=True, validated_at=datetime.now(timezone.utc),
    )


def test_matching_run_is_verified_successful():
    oracle = approved_oracle(("diagnostic.D-42.status",), {"diagnostic.D-42.status": "treated"})
    verdict = verify_against_approved_oracle(
        oracle, make_result("T-APPROVAL"),
        state_before={"diagnostic.D-42.status": "pending"}, state_after={"diagnostic.D-42.status": "treated"},
    )
    assert verdict.matches_expected is True
    assert verdict.overconfidence_detected is False
    assert verdict.unexpected_fields == ()


def test_wrong_value_for_an_approved_field_fails_verification():
    oracle = approved_oracle(("diagnostic.D-42.status",), {"diagnostic.D-42.status": "treated"})
    verdict = verify_against_approved_oracle(
        oracle, make_result("T-APPROVAL"),
        state_before={"diagnostic.D-42.status": "pending"}, state_after={"diagnostic.D-42.status": "failed"},
    )
    assert verdict.matches_expected is False
    assert verdict.overconfidence_detected is True  # declared_success=True par défaut dans make_result


def test_missing_approved_field_fails_verification():
    oracle = approved_oracle(
        ("diagnostic.D-42.status", "farmer.F-001.notified_count"),
        {"diagnostic.D-42.status": "treated", "farmer.F-001.notified_count": 1},
    )
    # Seul le diagnostic change ; la notification, pourtant approuvée, n'a pas lieu.
    verdict = verify_against_approved_oracle(
        oracle, make_result("T-APPROVAL"),
        state_before={"diagnostic.D-42.status": "pending"}, state_after={"diagnostic.D-42.status": "treated"},
    )
    assert verdict.matches_expected is False


def test_task_id_mismatch_is_rejected():
    oracle = approved_oracle(("x",), {"x": 1})
    with pytest.raises(ValueError, match="concerne la tâche"):
        verify_against_approved_oracle(oracle, make_result("AUTRE-TACHE"), {}, {})


def test_as_verification_result_conversion():
    oracle = approved_oracle(("x",), {"x": 1})
    verdict = verify_against_approved_oracle(oracle, make_result("T-APPROVAL"), {"x": 0}, {"x": 1})
    plain = verdict.as_verification_result()
    assert plain.task_id == verdict.task_id
    assert plain.trial_id == verdict.trial_id
    assert plain.matches_expected == verdict.matches_expected
    assert plain.overconfidence_detected == verdict.overconfidence_detected
    assert plain.actual_state_delta == verdict.actual_state_delta


# ---------------------------------------------------------------------------
# 5bis.4 — LE test critique : un effet de bord non prévu passe l'ancien
# mécanisme silencieusement, le nouveau le détecte explicitement.
# ---------------------------------------------------------------------------


def test_unexpected_side_effect_slips_past_old_verify_but_is_caught_by_the_new_oracle():
    """Un agent traite bien le diagnostic (l'effet attendu) MAIS envoie EN
    PLUS une notification que personne n'a demandée (un effet de bord non
    prévu, ex. bug qui déclenche notify_farmer par erreur). L'agent
    déclare un succès."""
    task = make_task()
    result = make_result(task.id, declared=True)
    state_before = {"diagnostic.D-42.status": "pending", "farmer.F-001.notified_count": 0}
    state_after = {"diagnostic.D-42.status": "treated", "farmer.F-001.notified_count": 1}  # effet en trop

    # --- Ancien mécanisme : verify() + expected_state_delta, INCHANGÉ ---
    old_verdict = verify(task, result, state_before, state_after)
    assert old_verdict.matches_expected is True  # l'effet en trop est ignoré SILENCIEUSEMENT
    assert old_verdict.overconfidence_detected is False  # aucune alerte : c'est le bug qu'on corrige

    # --- Nouveau mécanisme : oracle approuvé, notification NON approuvée ---
    oracle = approved_oracle(("diagnostic.D-42.status",), {"diagnostic.D-42.status": "treated"})
    new_verdict = verify_against_approved_oracle(oracle, result, state_before, state_after)

    assert new_verdict.unexpected_fields == ("farmer.F-001.notified_count",)
    assert new_verdict.matches_expected is False  # cette fois, c'est détecté
    assert new_verdict.overconfidence_detected is True  # et signalé comme sur-confiance


def test_unexpected_field_under_ignore_policy_is_reported_but_does_not_fail():
    """Sous unexpected_field_policy='ignore', le signal reste visible
    (transparence) mais n'invalide pas la vérification — usage légitime
    pour un champ technique connu et sans risque (ex. un timestamp)."""
    task = make_task()
    result = make_result(task.id)
    oracle = approved_oracle(("diagnostic.D-42.status",), {"diagnostic.D-42.status": "treated"}, policy="ignore")

    verdict = verify_against_approved_oracle(
        oracle, result,
        state_before={"diagnostic.D-42.status": "pending", "internal.touched_at": "t0"},
        state_after={"diagnostic.D-42.status": "treated", "internal.touched_at": "t1"},
    )
    assert verdict.unexpected_fields == ("internal.touched_at",)
    assert verdict.matches_expected is True
    assert verdict.overconfidence_detected is False
