from agricam_reliable_agents.models.data_models import AgentResult, Task, TaskComplexity
from agricam_reliable_agents.verifier.success_verifier import (
    compute_diff,
    fuzzy_match,
    verify,
)


def make_task(expected_delta: dict) -> Task:
    return Task(
        id="T1",
        prompt="Traiter la parcelle P-003",
        complexity=TaskComplexity.LEVEL_2,
        category="treatment",
        expected_state_delta=expected_delta,
        verification_query="q-T1",
    )


def make_result(declared_success: bool) -> AgentResult:
    return AgentResult(
        task_id="T1",
        trial_id=0,
        final_answer="Traitement appliqué.",
        tool_calls=(),
        declared_success=declared_success,
        latency_ms=120.0,
        cost_usd=0.002,
    )


def test_compute_diff_detects_changed_field():
    before = {"diagnostic_42.status": "pending", "stock.qty": 10}
    after = {"diagnostic_42.status": "treated", "stock.qty": 9}
    diff = compute_diff(before, after)
    assert diff == {"diagnostic_42.status": "treated", "stock.qty": 9}


def test_compute_diff_ignores_unchanged_fields():
    before = {"a": 1, "b": 2}
    after = {"a": 1, "b": 2}
    assert compute_diff(before, after) == {}


def test_compute_diff_tolerates_float_rounding():
    before = {"stock.qty": 9.0}
    after = {"stock.qty": 9.0000001}
    assert compute_diff(before, after) == {}


def test_fuzzy_match_true_when_all_expected_fields_present():
    actual = {"diagnostic_42.status": "treated", "extra_field": "irrelevant"}
    expected = {"diagnostic_42.status": "treated"}
    assert fuzzy_match(actual, expected) is True


def test_fuzzy_match_false_when_expected_field_missing():
    actual = {"other_field": "x"}
    expected = {"diagnostic_42.status": "treated"}
    assert fuzzy_match(actual, expected) is False


def test_fuzzy_match_true_for_empty_expectation():
    assert fuzzy_match({}, {}) is True


def test_verify_detects_true_success():
    task = make_task({"diagnostic_42.status": "treated"})
    result = make_result(declared_success=True)
    before = {"diagnostic_42.status": "pending"}
    after = {"diagnostic_42.status": "treated"}
    v = verify(task, result, before, after)
    assert v.matches_expected is True
    assert v.overconfidence_detected is False


def test_verify_detects_overconfidence():
    """Le cas central du projet : l'agent DIT avoir réussi, mais l'état
    réel ne montre aucun changement correspondant."""
    task = make_task({"diagnostic_42.status": "treated"})
    result = make_result(declared_success=True)
    before = {"diagnostic_42.status": "pending"}
    after = {"diagnostic_42.status": "pending"}  # rien n'a changé en réalité
    v = verify(task, result, before, after)
    assert v.matches_expected is False
    assert v.overconfidence_detected is True


def test_verify_honest_failure_is_not_overconfidence():
    """Si l'agent échoue ET le déclare, ce n'est pas de la sur-confiance -
    c'est un échec honnête, une catégorie de défaillance différente."""
    task = make_task({"diagnostic_42.status": "treated"})
    result = make_result(declared_success=False)
    before = {"diagnostic_42.status": "pending"}
    after = {"diagnostic_42.status": "pending"}
    v = verify(task, result, before, after)
    assert v.matches_expected is False
    assert v.overconfidence_detected is False
