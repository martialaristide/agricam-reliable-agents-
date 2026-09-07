"""
Tâche 5 (Phase C) — modèle de données `agent_connections`/`task_oracles`
et contrainte `validated_by_human`.

Le test le plus important de cette tâche est
`test_run_gated_campaign_cannot_be_bypassed_by_calling_it_directly` : s'il
ne passe pas, la garantie de sécurité documentée (« aucun oracle actif
sans validated_by_human = true », appliquée au niveau du harnais/de
l'orchestration, jamais seulement de l'interface) est fausse.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from agricam_reliable_agents.connector.oracle_repository import (
    ConnectorType,
    Environment,
    OracleRepository,
    TaskOracle,
    looks_like_raw_secret,
)
from agricam_reliable_agents.connector.orchestration import (
    OracleNotValidatedError,
    require_validated_oracle,
    run_gated_campaign,
)
from agricam_reliable_agents.models.data_models import (
    AgentResult,
    Task,
    TaskComplexity,
)
from agricam_reliable_agents.reliability.harness import HarnessConfig
from agricam_reliable_agents.reliability.repository import ReliabilityRepository


def make_task(task_id: str = "T-ORACLE") -> Task:
    return Task(
        id=task_id, prompt="Traite le diagnostic D-42.", complexity=TaskComplexity.LEVEL_1,
        category="generic", expected_state_delta={"diagnostic.status": "treated"}, verification_query="q",
    )


def make_validated_oracle(task_id: str = "T-ORACLE") -> TaskOracle:
    return TaskOracle(
        task_id=task_id, verification_method="approved_diff",
        approved_fields=("diagnostic.status",), baseline_state_diff={"diagnostic.status": "treated"},
        validated_by_human=True, validated_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def repos(tmp_path) -> tuple[ReliabilityRepository, OracleRepository]:
    """Même fichier SQLite pour les deux dépôts (même CampaignBase.metadata,
    même moteur) : task_oracles.task_id peut référencer tasks.id pour de vrai."""
    url = f"sqlite:///{(tmp_path / 'oracle.db').as_posix()}"
    reliability = ReliabilityRepository.from_url(url)
    oracle_repo = OracleRepository(reliability.engine)
    return reliability, oracle_repo


# ---------------------------------------------------------------------------
# 5.2 — credential_ref : heuristique de détection de secret en clair
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("secret", [
    "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGH",
    "AKIAIOSFODNN7EXAMPLE1",
    "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
    "a3f5c9e1b7d24680f1a2b3c4d5e6f7081920",  # chaîne aléatoire longue, sans structure
])
def test_looks_like_raw_secret_detects_common_shapes(secret: str):
    assert looks_like_raw_secret(secret) is True


@pytest.mark.parametrize("reference", [
    "vault://secret/data/agricam#api_key",
    "env:AGENT_TIERS_API_KEY",
    "arn:aws:secretsmanager:eu-west-1:123456789012:secret:agricam-agent-key",
    "prod",
    "ma-cle-de-test",  # contient un "-" mais pas de structure ":"/"/" ... courte (< 20)
])
def test_looks_like_raw_secret_accepts_references_and_short_values(reference: str):
    assert looks_like_raw_secret(reference) is False


def test_save_connection_rejects_raw_secret(repos):
    _, oracle_repo = repos
    with pytest.raises(ValueError, match="secret en clair"):
        oracle_repo.save_connection(
            "conn-1", connector_type="rest", environment="test",
            config={"endpoint": "http://x"}, credential_ref="sk-ant-api03-abcdefghijklmnopqrstuvwxyz",
        )


def test_save_connection_accepts_reference_and_round_trips(repos):
    _, oracle_repo = repos
    saved = oracle_repo.save_connection(
        "conn-1", connector_type="rest", environment="staging",
        config={"endpoint": "http://agent.example/chat"}, credential_ref="vault://secret/agricam/agent-1",
    )
    assert saved.credential_ref == "vault://secret/agricam/agent-1"

    fetched = oracle_repo.get_connection("conn-1")
    assert fetched is not None
    assert fetched.connector_type == "rest"
    assert fetched.environment == "staging"
    assert fetched.config == {"endpoint": "http://agent.example/chat"}
    assert fetched.created_at.tzinfo is not None


def test_save_connection_accepts_no_credential(repos):
    _, oracle_repo = repos
    saved = oracle_repo.save_connection("conn-2", connector_type="cli", environment="test", config={})
    assert saved.credential_ref is None


def test_get_connection_unknown_returns_none(repos):
    _, oracle_repo = repos
    assert oracle_repo.get_connection("nope") is None


def test_list_connections_orders_by_creation(repos):
    _, oracle_repo = repos
    oracle_repo.save_connection("a", connector_type="cli", environment="test", config={})
    oracle_repo.save_connection("b", connector_type="mcp", environment="test", config={})
    assert [c.id for c in oracle_repo.list_connections()] == ["a", "b"]


@pytest.mark.parametrize("connector_type", ["rest", "mcp", "direct_model", "cli"])
def test_all_documented_connector_types_are_accepted(repos, connector_type: ConnectorType):
    _, oracle_repo = repos
    saved = oracle_repo.save_connection(f"c-{connector_type}", connector_type=connector_type,
                                        environment="test", config={})
    assert saved.connector_type == connector_type


@pytest.mark.parametrize("environment", ["test", "staging", "production"])
def test_all_documented_environments_are_accepted(repos, environment: Environment):
    _, oracle_repo = repos
    saved = oracle_repo.save_connection(f"e-{environment}", connector_type="cli",
                                        environment=environment, config={})
    assert saved.environment == environment


# ---------------------------------------------------------------------------
# TaskOracle : validation applicative de la dataclasse
# ---------------------------------------------------------------------------


def test_task_oracle_rejects_invalid_unexpected_field_policy():
    with pytest.raises(ValueError, match="unexpected_field_policy"):
        TaskOracle(task_id="T", verification_method="manual", unexpected_field_policy="ask-nicely")


def test_task_oracle_requires_validated_at_when_validated_by_human_true():
    with pytest.raises(ValueError, match="validated_at"):
        TaskOracle(task_id="T", verification_method="manual", validated_by_human=True, validated_at=None)


def test_task_oracle_defaults():
    oracle = TaskOracle(task_id="T", verification_method="manual")
    assert oracle.approved_fields == ()
    assert oracle.unexpected_field_policy == "flag"
    assert oracle.inferred is False
    assert oracle.validated_by_human is False
    assert oracle.last_mutation_audit_passed is None


# ---------------------------------------------------------------------------
# task_oracles : FK réelle vers tasks.id, upsert, tous les champs de la Tâche 5bis
# ---------------------------------------------------------------------------


def test_save_oracle_for_unknown_task_is_rejected_by_the_real_fk(repos):
    """La tâche 'T-INCONNUE' n'a jamais été enregistrée via
    ReliabilityRepository.record_task : la FK task_oracles.task_id ->
    tasks.id (réellement appliquée depuis la Tâche 0) doit rejeter ceci."""
    _, oracle_repo = repos
    with pytest.raises(IntegrityError):
        oracle_repo.save_oracle(TaskOracle(task_id="T-INCONNUE", verification_method="manual"))


def test_save_and_get_oracle_round_trip_all_fields(repos):
    reliability, oracle_repo = repos
    reliability.record_task(make_task())

    oracle = TaskOracle(
        task_id="T-ORACLE", verification_method="approved_diff",
        verification_config={}, baseline_state_diff={"diagnostic.status": "treated", "x": 1},
        approved_fields=("diagnostic.status",), unexpected_field_policy="ignore",
        inferred=False, validated_by_human=True, validated_at=datetime.now(timezone.utc),
        last_mutation_audit_at=datetime.now(timezone.utc), last_mutation_audit_passed=True,
    )
    oracle_repo.save_oracle(oracle)

    fetched = oracle_repo.get_oracle("T-ORACLE")
    assert fetched is not None
    assert fetched.verification_method == "approved_diff"
    assert fetched.baseline_state_diff == {"diagnostic.status": "treated", "x": 1}
    assert fetched.approved_fields == ("diagnostic.status",)
    assert fetched.unexpected_field_policy == "ignore"
    assert fetched.validated_by_human is True
    assert fetched.validated_at is not None and fetched.validated_at.tzinfo is not None
    assert fetched.last_mutation_audit_passed is True


def test_save_oracle_upserts_by_task_id(repos):
    reliability, oracle_repo = repos
    reliability.record_task(make_task())
    oracle_repo.save_oracle(TaskOracle(task_id="T-ORACLE", verification_method="manual"))
    oracle_repo.save_oracle(TaskOracle(
        task_id="T-ORACLE", verification_method="approved_diff",
        validated_by_human=True, validated_at=datetime.now(timezone.utc),
    ))
    assert len(oracle_repo.list_oracles()) == 1
    assert oracle_repo.get_oracle("T-ORACLE").verification_method == "approved_diff"


def test_get_oracle_unknown_returns_none(repos):
    _, oracle_repo = repos
    assert oracle_repo.get_oracle("nope") is None


def test_record_mutation_audit_updates_only_those_two_fields(repos):
    reliability, oracle_repo = repos
    reliability.record_task(make_task())
    oracle_repo.save_oracle(make_validated_oracle())

    updated = oracle_repo.record_mutation_audit("T-ORACLE", passed=False)
    assert updated.last_mutation_audit_passed is False
    assert updated.last_mutation_audit_at is not None
    assert updated.validated_by_human is True  # inchangé
    assert updated.approved_fields == ("diagnostic.status",)  # inchangé


def test_record_mutation_audit_unknown_task_raises(repos):
    _, oracle_repo = repos
    with pytest.raises(ValueError, match="Aucun oracle"):
        oracle_repo.record_mutation_audit("T-INCONNUE", passed=True)


# ---------------------------------------------------------------------------
# 5.3 — le test le plus important : la contrainte ne se contourne pas en
# appelant directement la fonction Python (pas d'interface impliquée ici).
# ---------------------------------------------------------------------------


def test_require_validated_oracle_rejects_missing_oracle(repos):
    reliability, oracle_repo = repos
    reliability.record_task(make_task())
    with pytest.raises(OracleNotValidatedError, match="Aucun oracle"):
        require_validated_oracle(oracle_repo, "T-ORACLE")


def test_require_validated_oracle_rejects_unvalidated_oracle(repos):
    reliability, oracle_repo = repos
    reliability.record_task(make_task())
    oracle_repo.save_oracle(TaskOracle(task_id="T-ORACLE", verification_method="manual"))
    with pytest.raises(OracleNotValidatedError, match="validated_by_human"):
        require_validated_oracle(oracle_repo, "T-ORACLE")


def test_require_validated_oracle_accepts_validated_oracle(repos):
    reliability, oracle_repo = repos
    reliability.record_task(make_task())
    oracle_repo.save_oracle(make_validated_oracle())
    oracle = require_validated_oracle(oracle_repo, "T-ORACLE")
    assert oracle.validated_by_human is True


def test_run_gated_campaign_cannot_be_bypassed_by_calling_it_directly(repos):
    """LE test critique de la Tâche 5 : appel DIRECT de la fonction Python
    `run_gated_campaign` — aucune interface, aucun dashboard, aucune
    couche intermédiaire à contourner — avec un oracle non validé. Le
    rejet doit avoir lieu quand même, et AUCUN essai ne doit être exécuté
    ni persisté pour AUCUNE tâche de la liste (pas d'exécution partielle)."""
    reliability, oracle_repo = repos
    task = make_task()
    reliability.record_task(task)
    oracle_repo.save_oracle(TaskOracle(task_id="T-ORACLE", verification_method="manual"))  # jamais validé

    calls = []

    def spy_runner(t: Task, trial_id: int) -> AgentResult:
        calls.append((t.id, trial_id))
        return AgentResult(task_id=t.id, trial_id=trial_id, final_answer="Fait.", tool_calls=(),
                           declared_success=True, latency_ms=1.0, cost_usd=0.0)

    campaign_id = reliability.start_campaign("tentative-de-contournement")

    with pytest.raises(OracleNotValidatedError):
        run_gated_campaign(
            oracle_repo, reliability, campaign_id, [task], spy_runner, lambda t: {},
            HarnessConfig(n_trials=5, k_values=(1,)),
        )

    assert calls == []  # l'agent n'a JAMAIS été appelé
    assert reliability.list_trials(campaign_id) == []  # rien n'a été persisté


def test_run_gated_campaign_rejects_all_tasks_if_any_single_one_is_unvalidated(repos):
    """Même avec une tâche validée dans le lot, une seule tâche non
    validée bloque la campagne ENTIÈRE — pas d'exécution partielle."""
    reliability, oracle_repo = repos
    validated_task = make_task("T-VALID")
    unvalidated_task = make_task("T-INVALID")
    reliability.record_task(validated_task)
    reliability.record_task(unvalidated_task)
    oracle_repo.save_oracle(make_validated_oracle("T-VALID"))
    oracle_repo.save_oracle(TaskOracle(task_id="T-INVALID", verification_method="manual"))

    def runner(t: Task, trial_id: int) -> AgentResult:
        return AgentResult(task_id=t.id, trial_id=trial_id, final_answer="Fait.", tool_calls=(),
                           declared_success=True, latency_ms=1.0, cost_usd=0.0)

    campaign_id = reliability.start_campaign("c")
    with pytest.raises(OracleNotValidatedError):
        run_gated_campaign(
            oracle_repo, reliability, campaign_id, [validated_task, unvalidated_task],
            runner, lambda t: {}, HarnessConfig(n_trials=2, k_values=(1,)),
        )
    assert reliability.list_trials(campaign_id) == []


def test_run_gated_campaign_succeeds_when_all_oracles_are_validated(repos):
    reliability, oracle_repo = repos
    task = make_task()
    reliability.record_task(task)
    oracle_repo.save_oracle(make_validated_oracle())

    state = {"diagnostic.status": "pending"}

    def runner(t: Task, trial_id: int) -> AgentResult:
        state["diagnostic.status"] = "treated"
        return AgentResult(task_id=t.id, trial_id=trial_id, final_answer="Fait.", tool_calls=(),
                           declared_success=True, latency_ms=1.0, cost_usd=0.0)

    def before_trial(t: Task, trial_id: int) -> None:
        state["diagnostic.status"] = "pending"

    campaign_id = reliability.start_campaign("c")
    reports = run_gated_campaign(
        oracle_repo, reliability, campaign_id, [task], runner, lambda t: dict(state),
        HarnessConfig(n_trials=4, k_values=(1,)), before_trial=before_trial,
    )

    assert len(reports) == 1
    assert reports[0].n_trials == 4
    assert reports[0].n_success == 4
    assert len(reliability.list_trials(campaign_id)) == 4
