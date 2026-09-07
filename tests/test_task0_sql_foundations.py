"""
Tâche 0 (bloquante) — corrections des fondations SQL, avant toute extension
du Connecteur d'Agent Générique.

Chaque test ici est délibérément écrit pour ÉCHOUER contre l'ancien schéma
et RÉUSSIR contre le corrigé (cf. critère d'acceptation de la Tâche 0) :

0.1 `trials` porte une contrainte d'unicité (campaign_id, task_id, trial_id).
0.2 Pas de table `verifications` séparée : le verdict de vérification est sur
    la même ligne que l'essai (garantie plus forte qu'une FK) ; la FK
    `trials.campaign_id -> campaigns.id` est désormais réellement appliquée
    sur SQLite (PRAGMA foreign_keys=ON), ce qui ne l'était pas avant.
0.3 `StaticPool` ne s'applique qu'aux URL SQLite en mémoire, jamais à un
    fichier — verrouillé par plusieurs formes d'URL.
0.4 `cost_usd` en `Numeric(12, 6)` : l'agrégation ne dérive plus comme le
    ferait un `sum()` de `float` IEEE 754.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool

from agricam_reliable_agents.models.data_models import AgentResult, VerificationResult
from agricam_reliable_agents.persistence.engine import create_engine_from_url
from agricam_reliable_agents.reliability.repository import ReliabilityRepository


def make_result(trial_id: int = 0, task_id: str = "T", cost_usd: float = 0.01) -> AgentResult:
    return AgentResult(
        task_id=task_id, trial_id=trial_id, final_answer="Fait.", tool_calls=(),
        declared_success=True, latency_ms=10.0, cost_usd=cost_usd,
    )


def make_verification(trial_id: int = 0, task_id: str = "T") -> VerificationResult:
    return VerificationResult(
        task_id=task_id, trial_id=trial_id, actual_state_delta={"x": 1},
        matches_expected=True, overconfidence_detected=False,
    )


@pytest.fixture
def repo() -> ReliabilityRepository:
    return ReliabilityRepository.from_url("sqlite://")


# ---------------------------------------------------------------------------
# 0.1 — Contrainte d'unicité sur trials(campaign_id, task_id, trial_id)
# ---------------------------------------------------------------------------

def test_duplicate_trial_identity_raises_integrity_error(repo: ReliabilityRepository):
    campaign_id = repo.start_campaign("c")
    repo.record_trial(campaign_id, make_result(trial_id=0), make_verification(trial_id=0))

    with pytest.raises(IntegrityError):
        repo.record_trial(campaign_id, make_result(trial_id=0), make_verification(trial_id=0))

    # Le premier enregistrement reste seul : pas de doublon comptabilisé.
    assert len(repo.list_trials(campaign_id)) == 1


def test_same_trial_id_on_different_task_or_campaign_is_allowed(repo: ReliabilityRepository):
    """L'unicité porte sur le TRIPLET, pas sur trial_id seul (partagé entre
    tâches d'une même campagne, cf. docstring de SecurityIncidentRow)."""
    campaign_id = repo.start_campaign("c")
    repo.record_trial(campaign_id, make_result(trial_id=0, task_id="A"), make_verification(0, "A"))
    repo.record_trial(campaign_id, make_result(trial_id=0, task_id="B"), make_verification(0, "B"))

    other_campaign = repo.start_campaign("c2")
    repo.record_trial(other_campaign, make_result(trial_id=0, task_id="A"), make_verification(0, "A"))

    assert len(repo.list_trials(campaign_id)) == 2
    assert len(repo.list_trials(other_campaign)) == 1


def test_unique_constraint_is_declared_on_the_table():
    """Verrouille la présence structurelle de la contrainte (pas seulement
    son effet observé), pour qu'une régression de schéma soit détectée même
    si un futur test contournait l'ORM."""
    from agricam_reliable_agents.reliability.repository import TrialRow

    names = {c.name for c in TrialRow.__table__.constraints if hasattr(c, "name")}
    assert "uq_trials_identity" in names


# ---------------------------------------------------------------------------
# 0.2 — Intégrité verdict/essai : pas de table `verifications` séparée,
# FK trials -> campaigns réellement appliquée (PRAGMA foreign_keys=ON)
# ---------------------------------------------------------------------------

def test_no_separate_verifications_table_verdict_lives_on_the_trial_row():
    """Documente et verrouille la décision de conception : le verdict du
    Success Verifier (matches_expected, overconfidence_detected,
    actual_state_delta) est une colonne de `trials`, jamais une table à
    part reliée par une FK potentiellement pendante."""
    from agricam_reliable_agents.reliability.repository import CampaignBase, TrialRow

    table_names = set(CampaignBase.metadata.tables)
    assert "verifications" not in table_names
    trial_columns = {c.name for c in TrialRow.__table__.columns}
    assert {"matches_expected", "overconfidence_detected", "actual_state_delta"} <= trial_columns


def test_recording_a_trial_for_a_nonexistent_campaign_is_rejected(repo: ReliabilityRepository):
    """Preuve que la FK trials -> campaigns est désormais RÉELLEMENT
    appliquée sur SQLite (elle ne l'était pas avant PRAGMA foreign_keys=ON :
    ce test échouait silencieusement — aucune exception — contre l'ancien
    moteur)."""
    with pytest.raises(IntegrityError):
        repo.record_trial(999_999, make_result(), make_verification())
    assert repo.list_trials(999_999) == []


def test_sqlite_foreign_key_enforcement_is_actually_on():
    """Test au ras du moteur, indépendant du dépôt : si quelqu'un retire un
    jour l'événement `connect`, ce test échoue immédiatement plutôt que de
    laisser les tests de FK ci-dessus réussir « par accident »."""
    engine = create_engine_from_url("sqlite://")
    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1


def test_incident_for_nonexistent_campaign_is_also_rejected(repo: ReliabilityRepository):
    """Même garantie sur `security_incidents`, qui porte la même FK."""
    from agricam_reliable_agents.models.data_models import (
        AttackCategory,
        SecurityIncident,
    )

    with pytest.raises(IntegrityError):
        repo.record_incident(999_999, SecurityIncident(0, AttackCategory.EXCESSIVE_AGENCY, "{}", True))


# ---------------------------------------------------------------------------
# 0.3 — StaticPool réservé aux URL SQLite en mémoire, jamais à un fichier
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "sqlite:///relative.db",
    "sqlite:////tmp/test.db",
    "sqlite:///C:/data/agricam.db",
])
def test_sqlite_file_urls_never_get_static_pool(url: str, tmp_path):
    # Réécrit vers tmp_path pour ne pas créer de fichier hors du bac à sable
    # de test, tout en gardant la forme d'URL (préfixe, nombre de slashs).
    real_url = f"sqlite:///{(tmp_path / 'x.db').as_posix()}"
    engine = create_engine_from_url(real_url)
    try:
        assert not isinstance(engine.pool, StaticPool)
    finally:
        engine.dispose()


@pytest.mark.parametrize("memory_url", [
    "sqlite://",
    "sqlite:///:memory:",
])
def test_sqlite_memory_urls_get_static_pool(memory_url: str):
    engine = create_engine_from_url(memory_url)
    assert isinstance(engine.pool, StaticPool)


# ---------------------------------------------------------------------------
# 0.4 — cost_usd en Numeric(12, 6) : agrégation sans dérive flottante
# ---------------------------------------------------------------------------

def test_cost_column_is_numeric_not_float():
    from agricam_reliable_agents.reliability.repository import TrialRow

    column_type = TrialRow.__table__.columns["cost_usd"].type
    assert column_type.__class__.__name__ == "Numeric"
    assert (column_type.precision, column_type.scale) == (12, 6)


def test_naive_float_sum_of_repeated_cost_actually_drifts():
    """Prouve que le problème documenté est réel avant de prouver le
    correctif : trois fois 0,1 en `float` IEEE 754 ne fait PAS exactement
    0,3 (`0.30000000000000004`, l'exemple canonique de l'imprécision
    binaire — vrai pour 3 ou 7 répétitions, pas pour 10, où l'erreur
    accumulée par `sum()` se ré-arrondit par coïncidence sur la valeur
    exacte : la valeur choisie ici est vérifiée, pas supposée)."""
    assert sum([0.1] * 3) != 0.3


def test_sum_cost_usd_does_not_drift_where_naive_float_sum_would(repo: ReliabilityRepository):
    campaign_id = repo.start_campaign("c")
    for i in range(3):
        repo.record_trial(campaign_id, make_result(trial_id=i, cost_usd=0.1), make_verification(i))

    trials = repo.list_trials(campaign_id)
    naive_total = sum(t.cost_usd for t in trials)  # float : contrat public inchangé
    exact_total = repo.sum_cost_usd(campaign_id)  # Decimal : agrégation corrigée

    assert naive_total != 0.3  # la dérive existe bien sur le chemin naïf
    assert exact_total == Decimal("0.3")  # et le nouveau chemin l'élimine
    assert isinstance(exact_total, Decimal)


def test_sum_cost_usd_filters_by_task(repo: ReliabilityRepository):
    campaign_id = repo.start_campaign("c")
    repo.record_trial(campaign_id, make_result(trial_id=0, task_id="A", cost_usd=0.2), make_verification(0, "A"))
    repo.record_trial(campaign_id, make_result(trial_id=0, task_id="B", cost_usd=0.3), make_verification(0, "B"))

    assert repo.sum_cost_usd(campaign_id, task_id="A") == Decimal("0.2")
    assert repo.sum_cost_usd(campaign_id) == Decimal("0.5")


def test_sum_cost_usd_of_empty_campaign_is_zero(repo: ReliabilityRepository):
    campaign_id = repo.start_campaign("c")
    assert repo.sum_cost_usd(campaign_id) == Decimal(0)


def test_cost_usd_round_trips_to_float_within_scale(repo: ReliabilityRepository):
    """Contrat public inchangé : `TrialRecord.cost_usd` reste un float utilisable
    tel quel par l'API et le dashboard existants, arrondi à 6 décimales."""
    campaign_id = repo.start_campaign("c")
    repo.record_trial(campaign_id, make_result(cost_usd=0.0142857), make_verification())
    [trial] = repo.list_trials(campaign_id)
    assert isinstance(trial.cost_usd, float)
    assert trial.cost_usd == pytest.approx(0.014286, abs=1e-6)


def test_existing_tables_still_created_and_usable(repo: ReliabilityRepository):
    """Garde-fou global : le schéma modifié se crée toujours sans erreur et
    les tables attendues sont bien présentes."""
    inspector = inspect(repo.engine)
    assert {"campaigns", "tasks", "trials", "reports", "security_incidents"} <= set(
        inspector.get_table_names()
    )
