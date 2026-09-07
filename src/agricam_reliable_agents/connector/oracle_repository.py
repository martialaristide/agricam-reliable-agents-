"""
Tables `agent_connections` et `task_oracles` — Phase C du plan
d'implémentation (section 6 du document de référence, version mise à
jour incluant `baseline_state_diff`, `approved_fields`,
`unexpected_field_policy`, `last_mutation_audit_at`,
`last_mutation_audit_passed`).

Partagent la même `MetaData` SQLAlchemy que `reliability/repository.py`
(`CampaignBase`, importée telle quelle — jamais redéfinie) :
`task_oracles.task_id` porte une vraie clé étrangère vers `tasks.id`,
appliquée pour de vrai sur SQLite grâce à `PRAGMA foreign_keys=ON`
(Tâche 0, `persistence/engine.py`) — un oracle ne peut pas référencer une
tâche qui n'existe pas. Instancier `OracleRepository` OU
`ReliabilityRepository` sur le même moteur crée l'ensemble du schéma du
projet, les deux dépôts partageant `CampaignBase.metadata`.

Sécurité des identifiants (Tâche 5.2)
--------------------------------------
`credential_ref` ne doit JAMAIS contenir un secret en clair — seulement
une référence vers un endroit où le trouver (gestionnaire de secrets,
variable d'environnement...). `looks_like_raw_secret` applique une
heuristique simple (préfixes connus de clés d'API, forme d'une chaîne
aléatoire sans structure de référence) pour refuser, dès la construction,
toute valeur qui y ressemble. Ce n'est PAS un détecteur de secrets
exhaustif (un vrai scanner — entropie de Shannon, git-secrets — resterait
nécessaire en production) : elle attrape le cas le plus fréquent et le
plus dangereux, coller directement une clé d'API dans un formulaire de
connexion plutôt que sa référence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from sqlalchemy import JSON, Boolean, DateTime, Engine, ForeignKey, String, Text, select
from sqlalchemy.orm import Mapped, mapped_column, sessionmaker

from agricam_reliable_agents.persistence.engine import (
    create_engine_from_url,
    database_url_from_env,
)
from agricam_reliable_agents.reliability.repository import CampaignBase

ConnectorType = Literal["rest", "mcp", "direct_model", "cli"]
Environment = Literal["test", "staging", "production"]
VerificationMethod = Literal["approved_diff", "manual", "api_call", "db_query"]
UnexpectedFieldPolicy = Literal["flag", "ignore"]

_REFERENCE_SCHEME_PREFIXES = ("vault://", "env:", "secretsmanager:", "ssm:", "keyring:", "arn:")
_SECRET_LOOKING_PREFIXES = ("sk-", "sk_", "pk_", "ghp_", "gho_", "AKIA", "AIza", "xox", "Bearer ")
_SECRET_LOOKING_MIN_LENGTH = 20


def looks_like_raw_secret(value: str) -> bool:
    """
    Heuristique : `value` ressemble-t-elle à un secret en clair plutôt
    qu'à une référence ?

    Une référence a un schéma reconnaissable (`vault://...`, `env:...`,
    `arn:aws:secretsmanager:...`). Un secret en clair ressemble soit à une
    clé d'API connue (préfixe `sk-`, `AKIA`, en-tête `Bearer ...`), soit à
    une chaîne aléatoire longue (>= 20 caractères) sans espace ni
    structure de référence (`:` ou `/`).
    """
    if value.startswith(_REFERENCE_SCHEME_PREFIXES):
        return False
    if value.startswith(_SECRET_LOOKING_PREFIXES):
        return True
    has_reference_structure = ":" in value or "/" in value
    return len(value) >= _SECRET_LOOKING_MIN_LENGTH and " " not in value and not has_reference_structure


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Tables SQL
# ---------------------------------------------------------------------------

class AgentConnectionRow(CampaignBase):
    __tablename__ = "agent_connections"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    connector_type: Mapped[str] = mapped_column(String(16))
    environment: Mapped[str] = mapped_column(String(16))
    config: Mapped[dict[str, Any]] = mapped_column(JSON)
    credential_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class TaskOracleRow(CampaignBase):
    __tablename__ = "task_oracles"

    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), primary_key=True)
    verification_method: Mapped[str] = mapped_column(String(16))
    verification_config: Mapped[dict[str, Any]] = mapped_column(JSON)
    baseline_state_diff: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    approved_fields: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    unexpected_field_policy: Mapped[str] = mapped_column(String(8), default="flag")
    inferred: Mapped[bool] = mapped_column(Boolean, default=False)
    validated_by_human: Mapped[bool] = mapped_column(Boolean, default=False)
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_mutation_audit_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_mutation_audit_passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)


# ---------------------------------------------------------------------------
# Vues en lecture (dataclasses immuables)
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class AgentConnectionRecord:
    id: str
    connector_type: str
    environment: str
    config: dict[str, Any]
    credential_ref: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class TaskOracle:
    """
    Oracle d'une tâche : comment vérifier qu'un essai a réussi, et si
    cette méthode a déjà été validée par un humain (`validated_by_human`
    — condition nécessaire, appliquée par `connector/orchestration.py`,
    pour qu'une campagne puisse utiliser cette tâche).
    """

    task_id: str
    verification_method: str
    verification_config: dict[str, Any] = field(default_factory=dict)
    baseline_state_diff: dict[str, Any] | None = None
    approved_fields: tuple[str, ...] = ()
    unexpected_field_policy: str = "flag"
    inferred: bool = False
    validated_by_human: bool = False
    validated_at: datetime | None = None
    last_mutation_audit_at: datetime | None = None
    last_mutation_audit_passed: bool | None = None

    def __post_init__(self) -> None:
        if self.unexpected_field_policy not in ("flag", "ignore"):
            raise ValueError("unexpected_field_policy doit être 'flag' ou 'ignore'.")
        if self.validated_by_human and self.validated_at is None:
            raise ValueError("validated_at doit être renseigné quand validated_by_human est vrai.")


# ---------------------------------------------------------------------------
# Dépôt
# ---------------------------------------------------------------------------

class OracleRepository:
    """Écriture et relecture de `agent_connections` et `task_oracles`."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        CampaignBase.metadata.create_all(engine)
        self._session = sessionmaker(engine, expire_on_commit=False)

    @classmethod
    def from_url(cls, url: str | None = None) -> OracleRepository:
        return cls(create_engine_from_url(url or database_url_from_env()))

    @property
    def engine(self) -> Engine:
        return self._engine

    # ---- Connexions d'agent -----------------------------------------------

    def save_connection(
        self,
        connection_id: str,
        *,
        connector_type: ConnectorType,
        environment: Environment,
        config: dict[str, Any],
        credential_ref: str | None = None,
    ) -> AgentConnectionRecord:
        """Enregistre (ou remplace) une connexion d'agent. Refuse
        `credential_ref` s'il ressemble à un secret en clair plutôt qu'à
        une référence (Tâche 5.2)."""
        if credential_ref is not None and looks_like_raw_secret(credential_ref):
            raise ValueError(
                "credential_ref ressemble à un secret en clair, pas à une référence "
                "(ex. 'vault://...', 'env:MA_VARIABLE') : ne jamais stocker de secret directement."
            )
        created_at = _utcnow()
        with self._session() as session, session.begin():
            session.merge(AgentConnectionRow(
                id=connection_id, connector_type=connector_type, environment=environment,
                config=dict(config), credential_ref=credential_ref, created_at=created_at,
            ))
        return AgentConnectionRecord(
            id=connection_id, connector_type=connector_type, environment=environment,
            config=dict(config), credential_ref=credential_ref, created_at=created_at,
        )

    def get_connection(self, connection_id: str) -> AgentConnectionRecord | None:
        with self._session() as session:
            row = session.get(AgentConnectionRow, connection_id)
        return None if row is None else _to_connection(row)

    def list_connections(self) -> list[AgentConnectionRecord]:
        with self._session() as session:
            rows = session.scalars(select(AgentConnectionRow).order_by(AgentConnectionRow.created_at)).all()
        return [_to_connection(r) for r in rows]

    # ---- Oracles ------------------------------------------------------------

    def save_oracle(self, oracle: TaskOracle) -> TaskOracle:
        """Enregistre (ou remplace) l'oracle d'une tâche — upsert par
        `task_id`, comme `ReliabilityRepository.record_task`. Lève
        `IntegrityError` si `task_id` ne référence aucune tâche existante
        (FK réellement appliquée, cf. docstring de module)."""
        with self._session() as session, session.begin():
            session.merge(TaskOracleRow(
                task_id=oracle.task_id, verification_method=oracle.verification_method,
                verification_config=dict(oracle.verification_config),
                baseline_state_diff=(
                    dict(oracle.baseline_state_diff) if oracle.baseline_state_diff is not None else None
                ),
                approved_fields=list(oracle.approved_fields) if oracle.approved_fields else None,
                unexpected_field_policy=oracle.unexpected_field_policy, inferred=oracle.inferred,
                validated_by_human=oracle.validated_by_human, validated_at=oracle.validated_at,
                last_mutation_audit_at=oracle.last_mutation_audit_at,
                last_mutation_audit_passed=oracle.last_mutation_audit_passed,
            ))
        return oracle

    def get_oracle(self, task_id: str) -> TaskOracle | None:
        with self._session() as session:
            row = session.get(TaskOracleRow, task_id)
        return None if row is None else _to_oracle(row)

    def list_oracles(self) -> list[TaskOracle]:
        with self._session() as session:
            rows = session.scalars(select(TaskOracleRow).order_by(TaskOracleRow.task_id)).all()
        return [_to_oracle(r) for r in rows]

    def record_mutation_audit(self, task_id: str, *, passed: bool, at: datetime | None = None) -> TaskOracle:
        """Persiste le résultat du dernier audit de mutation (Tâche 5ter),
        sans toucher au reste de l'oracle."""
        timestamp = at or _utcnow()
        with self._session() as session, session.begin():
            row = session.get(TaskOracleRow, task_id)
            if row is None:
                raise ValueError(f"Aucun oracle pour la tâche {task_id!r} : rien à mettre à jour.")
            row.last_mutation_audit_at = timestamp
            row.last_mutation_audit_passed = passed
        oracle = self.get_oracle(task_id)
        assert oracle is not None  # vient d'être écrit dans la même transaction
        return oracle


def _to_connection(row: AgentConnectionRow) -> AgentConnectionRecord:
    return AgentConnectionRecord(
        id=row.id, connector_type=row.connector_type, environment=row.environment,
        config=dict(row.config), credential_ref=row.credential_ref, created_at=_as_utc(row.created_at),
    )


def _to_oracle(row: TaskOracleRow) -> TaskOracle:
    return TaskOracle(
        task_id=row.task_id, verification_method=row.verification_method,
        verification_config=dict(row.verification_config),
        baseline_state_diff=dict(row.baseline_state_diff) if row.baseline_state_diff is not None else None,
        approved_fields=tuple(row.approved_fields) if row.approved_fields else (),
        unexpected_field_policy=row.unexpected_field_policy, inferred=row.inferred,
        validated_by_human=row.validated_by_human, validated_at=_as_utc(row.validated_at),
        last_mutation_audit_at=_as_utc(row.last_mutation_audit_at),
        last_mutation_audit_passed=row.last_mutation_audit_passed,
    )
