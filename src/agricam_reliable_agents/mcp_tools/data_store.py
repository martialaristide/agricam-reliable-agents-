"""
Store de données AgriCam en mémoire, et contrat commun `DataStore`.

Deux implémentations coexistent :
- `AgriCamDataStore` (ce module) : dictionnaires en mémoire, pour les
  tests unitaires et la démonstration ;
- `SqlAlchemyDataStore` (mcp_tools/sql_store.py) : SQLite ou PostgreSQL,
  pour les campagnes persistées et le serveur MCP configuré par
  `AGRICAM_DATABASE_URL`.

Les outils MCP (`AgriCamTools`) ne dépendent que du protocole `DataStore`
défini en fin de module : basculer de l'une à l'autre ne demande aucune
modification des outils ni de la boucle agent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal, Protocol

MetricName = Literal["humidity", "temperature", "soil_ph"]


class AgriCamDataError(Exception):
    """Erreur métier levée quand une entité demandée n'existe pas."""


@dataclass
class SensorReading:
    parcel_id: str
    metric: MetricName
    value: float
    timestamp: datetime


@dataclass
class Diagnostic:
    id: str
    parcel_id: str
    disease: str
    status: Literal["pending", "treated"] = "pending"
    recommended_product_id: str | None = None


@dataclass
class MarketplaceProduct:
    id: str
    name: str
    stock_qty: int


@dataclass
class Farmer:
    id: str
    name: str
    notified_messages: list[str] = field(default_factory=list)


class AgriCamDataStore:
    """
    Store en mémoire simulant les données AgriCam nécessaires aux outils
    MCP. Thread-safety non garantie : à usage de test/démo uniquement.
    """

    def __init__(self) -> None:
        self._sensor_readings: list[SensorReading] = []
        self._diagnostics: dict[str, Diagnostic] = {}
        self._products: dict[str, MarketplaceProduct] = {}
        self._farmers: dict[str, Farmer] = {}
        self._parcel_farmer: dict[str, str] = {}

    # ---- Cycle de vie des données -----------------------------------------
    def is_empty(self) -> bool:
        return not (self._sensor_readings or self._diagnostics or self._products or self._farmers)

    def reset(self) -> None:
        """Vide tout l'état (même contrat que `SqlAlchemyDataStore.reset`)."""
        self._sensor_readings.clear()
        self._diagnostics.clear()
        self._products.clear()
        self._farmers.clear()
        self._parcel_farmer.clear()

    def seed_demo_data(self) -> None:
        """Jeu de démonstration ; sans effet si le store contient déjà des
        données (idempotent, comme le store SQL)."""
        if not self.is_empty():
            return
        now = datetime.now(timezone.utc)
        self._sensor_readings.extend([
            SensorReading("P-001", "humidity", 78.5, now),
            SensorReading("P-002", "humidity", 41.2, now),
            SensorReading("P-003", "humidity", 82.0, now),
        ])
        self._diagnostics["D-42"] = Diagnostic(
            id="D-42", parcel_id="P-003", disease="mildiou",
            status="pending", recommended_product_id="PRD-7",
        )
        self._products["PRD-7"] = MarketplaceProduct(
            id="PRD-7", name="Fongicide bio FB-12", stock_qty=25,
        )
        self._farmers["F-001"] = Farmer(id="F-001", name="Jean Mballa")
        self._parcel_farmer["P-003"] = "F-001"

    # ---- Lecture ---------------------------------------------------------
    def get_sensor_data(
        self, parcel_id: str, metric: MetricName | Literal["all"] = "all"
    ) -> list[SensorReading]:
        readings = [r for r in self._sensor_readings if r.parcel_id == parcel_id]
        if metric != "all":
            readings = [r for r in readings if r.metric == metric]
        if not readings:
            raise AgriCamDataError(f"Aucune donnée capteur pour la parcelle {parcel_id!r}.")
        return readings

    def get_diagnostic(self, diagnostic_id: str) -> Diagnostic:
        if diagnostic_id not in self._diagnostics:
            raise AgriCamDataError(f"Diagnostic inconnu : {diagnostic_id!r}.")
        return self._diagnostics[diagnostic_id]

    def get_diagnostic_history(self, parcel_id: str | None = None, limit: int = 20) -> list[Diagnostic]:
        if limit <= 0:
            return []
        values = sorted(self._diagnostics.values(), key=lambda d: d.id)
        if parcel_id is not None:
            values = [d for d in values if d.parcel_id == parcel_id]
        return values[:limit]

    def get_product(self, product_id: str) -> MarketplaceProduct:
        if product_id not in self._products:
            raise AgriCamDataError(f"Produit inconnu : {product_id!r}.")
        return self._products[product_id]

    def get_farmer_for_parcel(self, parcel_id: str) -> Farmer:
        farmer_id = self._parcel_farmer.get(parcel_id)
        if farmer_id is None:
            raise AgriCamDataError(f"Aucun exploitant associé à {parcel_id!r}.")
        return self._farmers[farmer_id]

    # ---- Écriture (utilisées uniquement via les outils MCP contrôlés) ---
    def mark_diagnostic_treated(self, diagnostic_id: str) -> None:
        diagnostic = self.get_diagnostic(diagnostic_id)
        diagnostic.status = "treated"

    def decrement_stock(self, product_id: str, quantity: int = 1) -> None:
        if quantity <= 0:
            raise ValueError("quantity doit être strictement positif.")
        product = self.get_product(product_id)
        if product.stock_qty < quantity:
            raise AgriCamDataError(f"Stock insuffisant pour {product_id!r}.")
        product.stock_qty -= quantity

    def notify_farmer(self, farmer_id: str, message: str) -> None:
        if farmer_id not in self._farmers:
            raise AgriCamDataError(f"Exploitant inconnu : {farmer_id!r}.")
        self._farmers[farmer_id].notified_messages.append(message)

    # ---- Instantané d'état (pour le Success Verifier) --------------------
    def snapshot(self) -> dict[str, object]:
        """Instantané plat de tout l'état pertinent, au format attendu
        par `verifier.success_verifier.compute_diff`."""
        state: dict[str, object] = {}
        for diag_id, diag in self._diagnostics.items():
            state[f"diagnostic.{diag_id}.status"] = diag.status
        for prod_id, prod in self._products.items():
            state[f"product.{prod_id}.stock_qty"] = prod.stock_qty
        for farmer_id, farmer in self._farmers.items():
            state[f"farmer.{farmer_id}.notified_count"] = len(farmer.notified_messages)
        return state


class DataStore(Protocol):
    """
    Contrat minimal attendu par `AgriCamTools` et par le harnais (via
    `snapshot`, `reset`, `seed_demo_data`). Toute implémentation doit :
    - lever `AgriCamDataError` pour une entité inconnue ou un stock
      insuffisant, `ValueError` pour une quantité non positive ;
    - avoir un `seed_demo_data` idempotent (sans effet si non vide) et un
      `reset` qui vide tout, pour qu'un `before_trial` soit portable ;
    - renvoyer `[]` pour `limit <= 0` et trier l'historique par identifiant.
    """

    def is_empty(self) -> bool: ...

    def reset(self) -> None: ...

    def seed_demo_data(self) -> None: ...

    def get_sensor_data(
        self, parcel_id: str, metric: MetricName | Literal["all"] = "all"
    ) -> list[SensorReading]: ...

    def get_diagnostic(self, diagnostic_id: str) -> Diagnostic: ...

    def get_diagnostic_history(
        self, parcel_id: str | None = None, limit: int = 20
    ) -> list[Diagnostic]: ...

    def get_product(self, product_id: str) -> MarketplaceProduct: ...

    def get_farmer_for_parcel(self, parcel_id: str) -> Farmer: ...

    def mark_diagnostic_treated(self, diagnostic_id: str) -> None: ...

    def decrement_stock(self, product_id: str, quantity: int = 1) -> None: ...

    def notify_farmer(self, farmer_id: str, message: str) -> None: ...

    def snapshot(self) -> dict[str, object]: ...
