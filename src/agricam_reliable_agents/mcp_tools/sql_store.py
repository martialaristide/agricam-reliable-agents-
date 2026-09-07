"""
Store de données AgriCam persistant (SQLAlchemy 2.x, SQLite ou PostgreSQL).

Implémente exactement le contrat `DataStore` (mcp_tools/data_store.py) :
les outils MCP (`AgriCamTools`) et la boucle agent fonctionnent sans
aucune modification avec ce store à la place du store en mémoire. Les
méthodes de lecture renvoient les mêmes dataclasses (`SensorReading`,
`Diagnostic`, ...) que le store en mémoire, détachées de toute session :
l'appelant ne manipule jamais d'objet ORM.

Décisions de conception
-----------------------
- Une session courte par opération (`with self._session() as s, s.begin()`)
  plutôt qu'une session longue : chaque appel d'outil est une transaction
  autonome, ce qui correspond au modèle d'exécution de l'agent (un outil
  = une action atomique, observable par le Success Verifier).
- `decrement_stock` est un `UPDATE ... WHERE stock_qty >= :qty` atomique,
  pas un lire-modifier-écrire : deux agents concurrents ne peuvent pas
  faire passer le stock en négatif.
- `seed_demo_data` est idempotent (ne fait rien si la base contient déjà
  des données) pour que le serveur MCP puisse être relancé sans doubler
  les jeux de démonstration ; `reset` vide les tables pour les campagnes
  de fiabilité, où chaque essai doit repartir d'un état connu.
- Les horodatages sont stockés en UTC ; SQLite ne conservant pas le fuseau,
  ils sont re-marqués UTC à la lecture pour garder le même contrat que le
  store en mémoire.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from sqlalchemy import (
    DateTime,
    Engine,
    ForeignKey,
    String,
    delete,
    func,
    select,
    update,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from agricam_reliable_agents.mcp_tools.data_store import (
    AgriCamDataError,
    Diagnostic,
    Farmer,
    MarketplaceProduct,
    MetricName,
    SensorReading,
)
from agricam_reliable_agents.persistence.engine import (
    create_engine_from_url,
    database_url_from_env,
)


class AgriCamBase(DeclarativeBase):
    """Base déclarative des tables métier AgriCam (distincte de celle des campagnes)."""


class SensorReadingRow(AgriCamBase):
    __tablename__ = "sensor_readings"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    parcel_id: Mapped[str] = mapped_column(String(32), index=True)
    metric: Mapped[str] = mapped_column(String(16))
    value: Mapped[float]
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class DiagnosticRow(AgriCamBase):
    __tablename__ = "diagnostics"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    parcel_id: Mapped[str] = mapped_column(String(32), index=True)
    disease: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="pending")
    recommended_product_id: Mapped[str | None] = mapped_column(String(32), nullable=True)


class ProductRow(AgriCamBase):
    __tablename__ = "products"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    stock_qty: Mapped[int]


class FarmerRow(AgriCamBase):
    __tablename__ = "farmers"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))


class FarmerNotificationRow(AgriCamBase):
    __tablename__ = "farmer_notifications"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    farmer_id: Mapped[str] = mapped_column(ForeignKey("farmers.id"), index=True)
    message: Mapped[str] = mapped_column(String(300))
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ParcelAssignmentRow(AgriCamBase):
    __tablename__ = "parcel_assignments"

    parcel_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    farmer_id: Mapped[str] = mapped_column(ForeignKey("farmers.id"))


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


class SqlAlchemyDataStore:
    """Store AgriCam persistant, interchangeable avec `AgriCamDataStore`."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        AgriCamBase.metadata.create_all(engine)
        self._session = sessionmaker(engine, expire_on_commit=False)

    @classmethod
    def from_url(cls, url: str | None = None) -> SqlAlchemyDataStore:
        """Construit le store depuis une URL, ou depuis `AGRICAM_DATABASE_URL`."""
        return cls(create_engine_from_url(url or database_url_from_env()))

    @property
    def engine(self) -> Engine:
        return self._engine

    # ---- Cycle de vie des données -----------------------------------------
    def is_empty(self) -> bool:
        """Vrai si AUCUNE table métier ne contient de ligne (un seed partiel
        laisserait sinon une clé primaire en doublon)."""
        with self._session() as session:
            for table in (SensorReadingRow, DiagnosticRow, ProductRow, FarmerRow):
                if session.scalar(select(func.count()).select_from(table)):
                    return False
        return True

    def reset(self) -> None:
        """Vide toutes les tables métier (ordre respectant les clés étrangères)."""
        with self._session() as session, session.begin():
            for table in (
                FarmerNotificationRow, ParcelAssignmentRow, SensorReadingRow,
                DiagnosticRow, ProductRow, FarmerRow,
            ):
                session.execute(delete(table))

    def seed_demo_data(self) -> None:
        """Jeu de démonstration identique au store en mémoire ; sans effet si
        la base contient déjà des données."""
        if not self.is_empty():
            return
        now = datetime.now(timezone.utc)
        with self._session() as session, session.begin():
            session.add_all([
                SensorReadingRow(parcel_id="P-001", metric="humidity", value=78.5, timestamp=now),
                SensorReadingRow(parcel_id="P-002", metric="humidity", value=41.2, timestamp=now),
                SensorReadingRow(parcel_id="P-003", metric="humidity", value=82.0, timestamp=now),
                DiagnosticRow(
                    id="D-42", parcel_id="P-003", disease="mildiou",
                    status="pending", recommended_product_id="PRD-7",
                ),
                ProductRow(id="PRD-7", name="Fongicide bio FB-12", stock_qty=25),
                FarmerRow(id="F-001", name="Jean Mballa"),
                ParcelAssignmentRow(parcel_id="P-003", farmer_id="F-001"),
            ])

    # ---- Lecture ---------------------------------------------------------
    def get_sensor_data(
        self, parcel_id: str, metric: MetricName | Literal["all"] = "all"
    ) -> list[SensorReading]:
        stmt = select(SensorReadingRow).where(SensorReadingRow.parcel_id == parcel_id)
        if metric != "all":
            stmt = stmt.where(SensorReadingRow.metric == metric)
        with self._session() as session:
            rows = session.scalars(stmt.order_by(SensorReadingRow.id)).all()
        if not rows:
            raise AgriCamDataError(f"Aucune donnée capteur pour la parcelle {parcel_id!r}.")
        return [
            SensorReading(r.parcel_id, r.metric, r.value, _as_utc(r.timestamp))  # type: ignore[arg-type]
            for r in rows
        ]

    def get_diagnostic(self, diagnostic_id: str) -> Diagnostic:
        with self._session() as session:
            row = session.get(DiagnosticRow, diagnostic_id)
        if row is None:
            raise AgriCamDataError(f"Diagnostic inconnu : {diagnostic_id!r}.")
        return self._to_diagnostic(row)

    def get_diagnostic_history(self, parcel_id: str | None = None, limit: int = 20) -> list[Diagnostic]:
        if limit <= 0:
            return []
        stmt = select(DiagnosticRow)
        if parcel_id is not None:
            stmt = stmt.where(DiagnosticRow.parcel_id == parcel_id)
        with self._session() as session:
            rows = session.scalars(stmt.order_by(DiagnosticRow.id).limit(limit)).all()
        return [self._to_diagnostic(r) for r in rows]

    def get_product(self, product_id: str) -> MarketplaceProduct:
        with self._session() as session:
            row = session.get(ProductRow, product_id)
        if row is None:
            raise AgriCamDataError(f"Produit inconnu : {product_id!r}.")
        return MarketplaceProduct(id=row.id, name=row.name, stock_qty=row.stock_qty)

    def get_farmer_for_parcel(self, parcel_id: str) -> Farmer:
        with self._session() as session:
            assignment = session.get(ParcelAssignmentRow, parcel_id)
            if assignment is None:
                raise AgriCamDataError(f"Aucun exploitant associé à {parcel_id!r}.")
            return self._load_farmer(session, assignment.farmer_id)

    # ---- Écriture (utilisées uniquement via les outils MCP contrôlés) ---
    def mark_diagnostic_treated(self, diagnostic_id: str) -> None:
        with self._session() as session, session.begin():
            row = session.get(DiagnosticRow, diagnostic_id)
            if row is None:
                raise AgriCamDataError(f"Diagnostic inconnu : {diagnostic_id!r}.")
            row.status = "treated"

    def decrement_stock(self, product_id: str, quantity: int = 1) -> None:
        if quantity <= 0:
            raise ValueError("quantity doit être strictement positif.")
        with self._session() as session, session.begin():
            result = session.execute(
                update(ProductRow)
                .where(ProductRow.id == product_id, ProductRow.stock_qty >= quantity)
                .values(stock_qty=ProductRow.stock_qty - quantity)
            )
            if result.rowcount == 1:
                return
            if session.get(ProductRow, product_id) is None:
                raise AgriCamDataError(f"Produit inconnu : {product_id!r}.")
            raise AgriCamDataError(f"Stock insuffisant pour {product_id!r}.")

    def notify_farmer(self, farmer_id: str, message: str) -> None:
        with self._session() as session, session.begin():
            if session.get(FarmerRow, farmer_id) is None:
                raise AgriCamDataError(f"Exploitant inconnu : {farmer_id!r}.")
            session.add(FarmerNotificationRow(
                farmer_id=farmer_id, message=message, sent_at=datetime.now(timezone.utc),
            ))

    # ---- Instantané d'état (pour le Success Verifier) --------------------
    def snapshot(self) -> dict[str, object]:
        """Même format de clés que `AgriCamDataStore.snapshot`."""
        state: dict[str, object] = {}
        with self._session() as session:
            for diag in session.scalars(select(DiagnosticRow).order_by(DiagnosticRow.id)):
                state[f"diagnostic.{diag.id}.status"] = diag.status
            for prod in session.scalars(select(ProductRow).order_by(ProductRow.id)):
                state[f"product.{prod.id}.stock_qty"] = prod.stock_qty
            counts = dict(
                session.execute(
                    select(FarmerNotificationRow.farmer_id, func.count())
                    .group_by(FarmerNotificationRow.farmer_id)
                ).all()
            )
            for farmer in session.scalars(select(FarmerRow).order_by(FarmerRow.id)):
                state[f"farmer.{farmer.id}.notified_count"] = counts.get(farmer.id, 0)
        return state

    # ---- Conversions ORM -> dataclasses ------------------------------------
    @staticmethod
    def _to_diagnostic(row: DiagnosticRow) -> Diagnostic:
        return Diagnostic(
            id=row.id, parcel_id=row.parcel_id, disease=row.disease,
            status=row.status, recommended_product_id=row.recommended_product_id,  # type: ignore[arg-type]
        )

    @staticmethod
    def _load_farmer(session: Session, farmer_id: str) -> Farmer:
        row = session.get(FarmerRow, farmer_id)
        if row is None:
            raise AgriCamDataError(f"Exploitant inconnu : {farmer_id!r}.")
        messages = session.scalars(
            select(FarmerNotificationRow.message)
            .where(FarmerNotificationRow.farmer_id == farmer_id)
            .order_by(FarmerNotificationRow.id)
        ).all()
        return Farmer(id=row.id, name=row.name, notified_messages=list(messages))
