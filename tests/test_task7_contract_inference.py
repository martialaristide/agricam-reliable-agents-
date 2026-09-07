"""
Tâche 7 (Phase E) — inférence de contrat (mode assisté).

Le test du cas ambigu (`test_ambiguous_schema_returns_none_rather_than_guessing`)
est aussi important que le cas qui réussit : une fonction qui « devine
toujours quelque chose » est le bug le plus dangereux ici. Le test le
plus important du fichier est
`test_inferred_oracle_still_requires_separate_human_validation`, qui relie
la Tâche 7 à la contrainte de la Tâche 5.3 : le chemin d'inférence ne
contourne rien.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agricam_reliable_agents.connector.contract_inference import (
    OracleCandidate,
    candidate_to_unvalidated_oracle,
    infer_oracle_candidate,
)
from agricam_reliable_agents.connector.oracle_repository import (
    OracleRepository,
    TaskOracle,
)
from agricam_reliable_agents.connector.orchestration import (
    OracleNotValidatedError,
    require_validated_oracle,
)
from agricam_reliable_agents.models.data_models import Task, TaskComplexity
from agricam_reliable_agents.reliability.repository import ReliabilityRepository

# ---------------------------------------------------------------------------
# Cas où l'inférence réussit clairement
# ---------------------------------------------------------------------------


def test_openapi_like_schema_with_status_field_is_detected():
    schema = {
        "type": "object",
        "properties": {
            "diagnostic_id": {"type": "string"},
            "status": {"type": "string", "enum": ["pending", "treated"]},
            "product_used": {"type": ["string", "null"]},
        },
    }
    candidate = infer_oracle_candidate(schema)
    assert candidate is not None
    assert candidate.suggested_fields == ("status",)
    assert "status" in candidate.rationale


def test_suffixed_status_field_name_is_detected():
    schema = {"type": "object", "properties": {"treatment_status": {"type": "string"}}}
    candidate = infer_oracle_candidate(schema)
    assert candidate is not None
    assert candidate.suggested_fields == ("treatment_status",)


def test_state_field_is_detected_case_insensitively():
    schema = {"type": "object", "properties": {"Diagnostic_State": {"type": "string"}}}
    candidate = infer_oracle_candidate(schema)
    assert candidate is not None
    assert candidate.suggested_fields == ("Diagnostic_State",)


# ---------------------------------------------------------------------------
# Cas ambigu : aucun signal exploitable -> None, jamais une supposition
# ---------------------------------------------------------------------------


def test_ambiguous_schema_returns_none_rather_than_guessing():
    """Un champ 'code' ou 'result' n'est PAS assez spécifique pour être
    proposé comme indicateur de succès — deviner ici serait le bug que
    cette fonction doit éviter."""
    schema = {
        "type": "object",
        "properties": {"result": {"type": "string"}, "code": {"type": "integer"}, "message": {"type": "string"}},
    }
    assert infer_oracle_candidate(schema) is None


def test_schema_with_no_properties_returns_none():
    assert infer_oracle_candidate({"type": "object", "properties": {}}) is None


# ---------------------------------------------------------------------------
# Cas malformé : échoue proprement (None), ne plante jamais
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("malformed_schema", [
    None,
    "juste une chaîne",
    42,
    [],
    {},
    {"type": "object"},  # pas de "properties" du tout
    {"properties": "pas un dict"},
    {"properties": ["status"]},
    {"properties": {123: {"type": "string"}}},  # nom de propriété non textuel
])
def test_malformed_schema_returns_none_without_raising(malformed_schema):
    assert infer_oracle_candidate(malformed_schema) is None


# ---------------------------------------------------------------------------
# candidate_to_unvalidated_oracle
# ---------------------------------------------------------------------------


def test_candidate_to_unvalidated_oracle_is_never_validated():
    candidate = OracleCandidate(suggested_fields=("status",), rationale="test")
    oracle = candidate_to_unvalidated_oracle(candidate, "T-INFER")

    assert oracle.inferred is True
    assert oracle.validated_by_human is False
    assert oracle.validated_at is None
    assert oracle.approved_fields == ("status",)
    assert oracle.verification_method == "manual"


def test_candidate_to_unvalidated_oracle_has_no_way_to_force_validation():
    """La fonction n'expose aucun paramètre permettant de produire
    validated_by_human=True — vérifié en introspectant sa signature, pour
    qu'un futur ajout de paramètre imprudent casse ce test plutôt que de
    passer inaperçu."""
    import inspect

    signature = inspect.signature(candidate_to_unvalidated_oracle)
    assert "validated_by_human" not in signature.parameters
    assert "validated_at" not in signature.parameters


# ---------------------------------------------------------------------------
# LE test critique : le chemin d'inférence ne contourne pas la Tâche 5.3
# ---------------------------------------------------------------------------


def make_task(task_id: str = "T-INFER") -> Task:
    return Task(
        id=task_id, prompt="Traite le diagnostic.", complexity=TaskComplexity.LEVEL_1,
        category="generic", expected_state_delta={"status": "treated"}, verification_query="q",
    )


def test_inferred_oracle_still_requires_separate_human_validation(tmp_path):
    """Un oracle produit par l'inférence de contrat, persisté tel quel,
    est REJETÉ par la même garde que n'importe quel autre oracle non
    validé (Tâche 5.3) — l'inférence n'ouvre aucun raccourci."""
    reliability = ReliabilityRepository.from_url(f"sqlite:///{(tmp_path / 'o.db').as_posix()}")
    reliability.record_task(make_task())
    oracle_repo = OracleRepository(reliability.engine)

    candidate = infer_oracle_candidate({"type": "object", "properties": {"status": {"type": "string"}}})
    assert candidate is not None
    inferred_oracle = candidate_to_unvalidated_oracle(candidate, "T-INFER")
    oracle_repo.save_oracle(inferred_oracle)

    with pytest.raises(OracleNotValidatedError, match="validated_by_human"):
        require_validated_oracle(oracle_repo, "T-INFER")

    # L'approbation humaine reste un acte SÉPARÉ et explicite : une fois
    # faite, la même tâche passe la garde — l'inférence n'a fait
    # qu'accélérer la PROPOSITION, jamais l'approbation elle-même.
    oracle_repo.save_oracle(TaskOracle(
        task_id="T-INFER", verification_method="manual", approved_fields=inferred_oracle.approved_fields,
        inferred=True, validated_by_human=True, validated_at=datetime.now(timezone.utc),
    ))
    validated = require_validated_oracle(oracle_repo, "T-INFER")
    assert validated.validated_by_human is True
    assert validated.inferred is True  # la provenance "inférée" reste tracée même une fois approuvé
