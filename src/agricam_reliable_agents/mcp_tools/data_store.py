"""
Store de données AgriCam en mémoire.

En production, cette classe serait remplacée par une couche d'accès à la
vraie base AgriCam (PostgreSQL, cf. section infrastructure). L'interface
publique (les méthodes get_*/set_*) est volontairement identique à ce que
serait un repository réel, pour que les outils MCP n'aient aucune
modification à faire lors de la bascule vers la production — seul
`AgriCamDataStore.__init__` changerait (connexion DB au lieu de dicts).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

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

    # ---- Semences de démonstration -------------------------------------
    def seed_demo_data(self) -> None:
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
    def get_sensor_data(self, parcel_id: str, metric: MetricName | Literal["all"] = "all"):
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
        values = list(self._diagnostics.values())
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
