"""
Dépôt persistant des résultats de campagne (SQLAlchemy 2.x).

Ce module enregistre ce que le harnais produit — essais (`AgentResult` +
`VerificationResult`), rapports (`ReliabilityReport`), incidents de
sécurité (`SecurityIncident`) — et sait les relire sous forme de
dataclasses immuables, sans jamais exposer d'objet ORM. C'est la source
de données du dashboard (phase 5) et la mémoire longue des campagnes :
une régression de fiabilité entre deux versions d'agent se lit en
comparant deux campagnes.

Il ne connaît ni AgriCam ni l'agent : comme le harnais, il est réutilisable
sur n'importe quel agent évalué avec `reliability/harness.py`.

Schéma
------
- `campaigns` : une exécution du harnais (nom, modèle, date).
- `tasks` : définition des tâches évaluées (upsert par identifiant, pour
  que le dashboard puisse croiser fiabilité et complexité).
- `trials` : un essai = résultat brut de l'agent + verdict du vérificateur.
- `reports` : rapport agrégé par tâche et par campagne.
- `security_incidents` : incidents journalisés par la garde de sécurité.

Les champs structurés (appels d'outils, deltas d'état, pass^k) sont
stockés en JSON : portable entre SQLite et PostgreSQL, et suffisant pour
un dépôt d'audit où l'on relit un essai entier plutôt que d'y filtrer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, DateTime, Engine, ForeignKey, String, Text, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from agricam_reliable_agents.models.data_models import (
    AgentResult,
    AttackCategory,
    ReliabilityReport,
    SecurityIncident,
    Task,
    TaskComplexity,
    VerificationResult,
)
from agricam_reliable_agents.persistence.engine import (
    create_engine_from_url,
    database_url_from_env,
)
from agricam_reliable_agents.reliability.harness import TrialObserver
from agricam_reliable_agents.security.guard import IncidentSink


class CampaignBase(DeclarativeBase):
    """Base déclarative des tables de campagne (distincte des tables métier)."""


class CampaignRow(CampaignBase):
    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128))
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class TaskRow(CampaignBase):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    prompt: Mapped[str] = mapped_column(Text)
    complexity: Mapped[str] = mapped_column(String(16))
    category: Mapped[str] = mapped_column(String(64))
    expected_state_delta: Mapped[dict[str, Any]] = mapped_column(JSON)
    verification_query: Mapped[str] = mapped_column(String(128))


class TrialRow(CampaignBase):
    __tablename__ = "trials"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"), index=True)
    task_id: Mapped[str] = mapped_column(String(64), index=True)
    trial_id: Mapped[int]
    final_answer: Mapped[str] = mapped_column(Text)
    tool_calls: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    declared_success: Mapped[bool]
    latency_ms: Mapped[float]
    cost_usd: Mapped[float]
    max_steps_exceeded: Mapped[bool]
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    actual_state_delta: Mapped[dict[str, Any]] = mapped_column(JSON)
    matches_expected: Mapped[bool]
    overconfidence_detected: Mapped[bool]
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ReportRow(CampaignBase):
    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"), index=True)
    task_id: Mapped[str] = mapped_column(String(64), index=True)
    n_trials: Mapped[int]
    n_success: Mapped[int]
    p_hat: Mapped[float]
    wilson_low: Mapped[float]
    wilson_high: Mapped[float]
    pass_k: Mapped[dict[str, float]] = mapped_column(JSON)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SecurityIncidentRow(CampaignBase):
    __tablename__ = "security_incidents"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"), index=True)
    trial_id: Mapped[int]
    attack_category: Mapped[str] = mapped_column(String(32))
    payload: Mapped[str] = mapped_column(Text)
    blocked: Mapped[bool]
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


# ---------------------------------------------------------------------------
# Vues en lecture (dataclasses immuables, jamais d'objet ORM hors du module)
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class CampaignRecord:
    id: int
    name: str
    model: str | None
    notes: str | None
    started_at: datetime


@dataclass(frozen=True, slots=True)
class TrialRecord:
    """Un essai relu depuis la base : résultat de l'agent + verdict du vérificateur."""

    campaign_id: int
    task_id: str
    trial_id: int
    final_answer: str
    tool_call_names: tuple[str, ...]
    declared_success: bool
    latency_ms: float
    cost_usd: float
    max_steps_exceeded: bool
    error: str | None
    actual_state_delta: dict[str, Any]
    matches_expected: bool
    overconfidence_detected: bool
    recorded_at: datetime

    @property
    def verified_success(self) -> bool:
        """Même règle de comptage que le harnais (`evaluate_task`)."""
        return self.matches_expected and not self.overconfidence_detected


@dataclass(frozen=True, slots=True)
class IncidentRecord:
    campaign_id: int
    trial_id: int
    attack_category: AttackCategory
    payload: str
    blocked: bool
    detected_at: datetime


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


class ReliabilityRepository:
    """Écriture et relecture des campagnes de fiabilité."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        CampaignBase.metadata.create_all(engine)
        self._session = sessionmaker(engine, expire_on_commit=False)

    @classmethod
    def from_url(cls, url: str | None = None) -> ReliabilityRepository:
        return cls(create_engine_from_url(url or database_url_from_env()))

    @property
    def engine(self) -> Engine:
        return self._engine

    # ---- Écriture ---------------------------------------------------------
    def start_campaign(self, name: str, *, model: str | None = None, notes: str | None = None) -> int:
        if not name:
            raise ValueError("Le nom de campagne est obligatoire.")
        with self._session() as session, session.begin():
            row = CampaignRow(name=name, model=model, notes=notes, started_at=_utcnow())
            session.add(row)
            session.flush()
            return row.id

    def record_task(self, task: Task) -> None:
        """Enregistre (ou met à jour) la définition d'une tâche."""
        with self._session() as session, session.begin():
            session.merge(TaskRow(
                id=task.id, prompt=task.prompt, complexity=task.complexity.value,
                category=task.category, expected_state_delta=dict(task.expected_state_delta),
                verification_query=task.verification_query,
            ))

    def record_trial(self, campaign_id: int, result: AgentResult, verification: VerificationResult) -> int:
        if result.task_id != verification.task_id or result.trial_id != verification.trial_id:
            raise ValueError("AgentResult et VerificationResult ne décrivent pas le même essai.")
        with self._session() as session, session.begin():
            row = TrialRow(
                campaign_id=campaign_id, task_id=result.task_id, trial_id=result.trial_id,
                final_answer=result.final_answer,
                tool_calls=[
                    {"tool_name": c.tool_name, "arguments": dict(c.arguments),
                     "timestamp": c.timestamp.isoformat()}
                    for c in result.tool_calls
                ],
                declared_success=result.declared_success, latency_ms=result.latency_ms,
                cost_usd=result.cost_usd, max_steps_exceeded=result.max_steps_exceeded,
                error=result.error, actual_state_delta=dict(verification.actual_state_delta),
                matches_expected=verification.matches_expected,
                overconfidence_detected=verification.overconfidence_detected,
                recorded_at=_utcnow(),
            )
            session.add(row)
            session.flush()
            return row.id

    def record_report(self, campaign_id: int, report: ReliabilityReport) -> int:
        with self._session() as session, session.begin():
            row = ReportRow(
                campaign_id=campaign_id, task_id=report.task_id,
                n_trials=report.n_trials, n_success=report.n_success, p_hat=report.p_hat,
                wilson_low=report.wilson_ci[0], wilson_high=report.wilson_ci[1],
                pass_k={str(k): v for k, v in report.pass_k.items()},
                recorded_at=_utcnow(),
            )
            session.add(row)
            session.flush()
            return row.id

    def record_incident(self, campaign_id: int, incident: SecurityIncident) -> int:
        with self._session() as session, session.begin():
            row = SecurityIncidentRow(
                campaign_id=campaign_id, trial_id=incident.trial_id,
                attack_category=incident.attack_category.value, payload=incident.payload,
                blocked=incident.blocked, detected_at=incident.detected_at,
            )
            session.add(row)
            session.flush()
            return row.id

    # ---- Adaptateurs vers les callbacks du harnais et de la garde ---------
    def trial_observer(self, campaign_id: int) -> TrialObserver:
        """Callback à passer en `on_trial=` à `evaluate_task`/`evaluate_reliability`."""

        def observe(result: AgentResult, verification: VerificationResult) -> None:
            self.record_trial(campaign_id, result, verification)

        return observe

    def incident_sink(self, campaign_id: int) -> IncidentSink:
        """Callback à passer en `incident_sink=` à `run_agent`/`guard_before_action`."""

        def sink(incident: SecurityIncident) -> None:
            self.record_incident(campaign_id, incident)

        return sink

    # ---- Lecture ----------------------------------------------------------
    def list_campaigns(self) -> list[CampaignRecord]:
        with self._session() as session:
            rows = session.scalars(select(CampaignRow).order_by(CampaignRow.id.desc())).all()
        return [
            CampaignRecord(id=r.id, name=r.name, model=r.model, notes=r.notes,
                           started_at=_as_utc(r.started_at))
            for r in rows
        ]

    def get_task(self, task_id: str) -> Task | None:
        with self._session() as session:
            row = session.get(TaskRow, task_id)
        if row is None:
            return None
        return Task(
            id=row.id, prompt=row.prompt, complexity=TaskComplexity(row.complexity),
            category=row.category, expected_state_delta=dict(row.expected_state_delta),
            verification_query=row.verification_query,
        )

    def list_tasks(self) -> list[Task]:
        with self._session() as session:
            ids = session.scalars(select(TaskRow.id).order_by(TaskRow.id)).all()
        return [t for t in (self.get_task(i) for i in ids) if t is not None]

    def list_trials(self, campaign_id: int, task_id: str | None = None) -> list[TrialRecord]:
        stmt = select(TrialRow).where(TrialRow.campaign_id == campaign_id)
        if task_id is not None:
            stmt = stmt.where(TrialRow.task_id == task_id)
        with self._session() as session:
            rows = session.scalars(stmt.order_by(TrialRow.id)).all()
        return [
            TrialRecord(
                campaign_id=r.campaign_id, task_id=r.task_id, trial_id=r.trial_id,
                final_answer=r.final_answer,
                tool_call_names=tuple(c["tool_name"] for c in r.tool_calls),
                declared_success=r.declared_success, latency_ms=r.latency_ms,
                cost_usd=r.cost_usd, max_steps_exceeded=r.max_steps_exceeded, error=r.error,
                actual_state_delta=dict(r.actual_state_delta),
                matches_expected=r.matches_expected,
                overconfidence_detected=r.overconfidence_detected,
                recorded_at=_as_utc(r.recorded_at),
            )
            for r in rows
        ]

    def list_reports(self, campaign_id: int) -> list[ReliabilityReport]:
        with self._session() as session:
            rows = session.scalars(
                select(ReportRow).where(ReportRow.campaign_id == campaign_id).order_by(ReportRow.id)
            ).all()
        return [
            ReliabilityReport(
                task_id=r.task_id, n_trials=r.n_trials, n_success=r.n_success, p_hat=r.p_hat,
                wilson_ci=(r.wilson_low, r.wilson_high),
                pass_k={int(k): v for k, v in r.pass_k.items()},
            )
            for r in rows
        ]

    def list_incidents(self, campaign_id: int) -> list[IncidentRecord]:
        with self._session() as session:
            rows = session.scalars(
                select(SecurityIncidentRow)
                .where(SecurityIncidentRow.campaign_id == campaign_id)
                .order_by(SecurityIncidentRow.id)
            ).all()
        return [
            IncidentRecord(
                campaign_id=r.campaign_id, trial_id=r.trial_id,
                attack_category=AttackCategory(r.attack_category), payload=r.payload,
                blocked=r.blocked, detected_at=_as_utc(r.detected_at),
            )
            for r in rows
        ]
