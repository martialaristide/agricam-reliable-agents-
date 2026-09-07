"""
Implémentation des outils MCP exposés à l'agent.

Chaque outil est composé de deux parties :
1. `*_SCHEMA` : le schéma JSON déclaré au serveur MCP (contrat d'entrée).
2. La fonction Python correspondante, qui valide ses entrées, appelle le
   data store, et retourne un dict JSON-sérialisable (jamais un objet
   Python interne — c'est une frontière de confiance).

Toutes les fonctions lèvent `AgriCamDataError` (jamais une exception
générique) en cas d'échec métier, pour que la boucle agent puisse la
distinguer d'une erreur de programmation.
"""

from __future__ import annotations

from typing import Any

from agricam_reliable_agents.mcp_tools.data_store import DataStore

# ---------------------------------------------------------------------------
# Schémas JSON (déclarés au serveur MCP)
# ---------------------------------------------------------------------------

GET_SENSOR_DATA_SCHEMA: dict[str, Any] = {
    "name": "get_sensor_data",
    "description": "Récupère les dernières mesures des capteurs IoT d'une parcelle.",
    "input_schema": {
        "type": "object",
        "properties": {
            "parcel_id": {"type": "string"},
            "metric": {
                "type": "string",
                "enum": ["humidity", "temperature", "soil_ph", "all"],
                "default": "all",
            },
        },
        "required": ["parcel_id"],
    },
}

GET_DIAGNOSTIC_HISTORY_SCHEMA: dict[str, Any] = {
    "name": "get_diagnostic_history",
    "description": "Retourne l'historique des diagnostics de maladies pour une parcelle ou toutes.",
    "input_schema": {
        "type": "object",
        "properties": {
            "parcel_id": {"type": "string"},
            "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 100},
        },
    },
}

RECOMMEND_TREATMENT_SCHEMA: dict[str, Any] = {
    "name": "recommend_treatment",
    "description": "Marque un diagnostic comme traité et décrémente le stock du produit recommandé.",
    "input_schema": {
        "type": "object",
        "properties": {"diagnostic_id": {"type": "string"}},
        "required": ["diagnostic_id"],
    },
}

CHECK_MARKETPLACE_STOCK_SCHEMA: dict[str, Any] = {
    "name": "check_marketplace_stock",
    "description": "Vérifie la disponibilité d'un produit sur le marketplace AgriCam.",
    "input_schema": {
        "type": "object",
        "properties": {"product_id": {"type": "string"}},
        "required": ["product_id"],
    },
}

NOTIFY_FARMER_SCHEMA: dict[str, Any] = {
    "name": "notify_farmer",
    "description": "Envoie une notification à un agriculteur. Outil SENSIBLE : soumis à ActionPolicy.",
    "input_schema": {
        "type": "object",
        "properties": {
            "farmer_id": {"type": "string"},
            "message": {"type": "string", "maxLength": 300},
        },
        "required": ["farmer_id", "message"],
    },
}

ALL_TOOL_SCHEMAS: tuple[dict[str, Any], ...] = (
    GET_SENSOR_DATA_SCHEMA,
    GET_DIAGNOSTIC_HISTORY_SCHEMA,
    RECOMMEND_TREATMENT_SCHEMA,
    CHECK_MARKETPLACE_STOCK_SCHEMA,
    NOTIFY_FARMER_SCHEMA,
)


# ---------------------------------------------------------------------------
# Implémentations (liées à un data store injecté — jamais de variable globale
# mutable, pour permettre l'exécution parallèle de plusieurs sessions/tests)
# ---------------------------------------------------------------------------

class AgriCamTools:
    """Regroupe les outils MCP, liés à une instance de data store (mémoire
    ou persistant : tout objet respectant le contrat `DataStore`)."""

    def __init__(self, store: DataStore) -> None:
        self._store = store

    @property
    def store(self) -> DataStore:
        """Store sous-jacent (pour les instantanés du Success Verifier)."""
        return self._store

    def get_sensor_data(self, parcel_id: str, metric: str = "all") -> dict[str, Any]:
        readings = self._store.get_sensor_data(parcel_id, metric)  # type: ignore[arg-type]
        return {
            "parcel_id": parcel_id,
            "readings": [
                {"metric": r.metric, "value": r.value, "timestamp": r.timestamp.isoformat()}
                for r in readings
            ],
        }

    def get_diagnostic_history(self, parcel_id: str | None = None, limit: int = 20) -> dict[str, Any]:
        diagnostics = self._store.get_diagnostic_history(parcel_id, limit)
        return {
            "diagnostics": [
                {
                    "id": d.id, "parcel_id": d.parcel_id, "disease": d.disease,
                    "status": d.status, "recommended_product_id": d.recommended_product_id,
                }
                for d in diagnostics
            ]
        }

    def recommend_treatment(self, diagnostic_id: str) -> dict[str, Any]:
        diagnostic = self._store.get_diagnostic(diagnostic_id)
        if diagnostic.recommended_product_id is not None:
            self._store.decrement_stock(diagnostic.recommended_product_id, quantity=1)
        self._store.mark_diagnostic_treated(diagnostic_id)
        return {
            "diagnostic_id": diagnostic_id,
            "status": "treated",
            "product_used": diagnostic.recommended_product_id,
        }

    def check_marketplace_stock(self, product_id: str) -> dict[str, Any]:
        product = self._store.get_product(product_id)
        return {"product_id": product_id, "name": product.name, "stock_qty": product.stock_qty}

    def notify_farmer(self, farmer_id: str, message: str) -> dict[str, Any]:
        self._store.notify_farmer(farmer_id, message)
        return {"farmer_id": farmer_id, "notified": True}

    def dispatch(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Point d'entrée unique utilisé par la boucle agent (agent/loop.py)."""
        handlers = {
            "get_sensor_data": self.get_sensor_data,
            "get_diagnostic_history": self.get_diagnostic_history,
            "recommend_treatment": self.recommend_treatment,
            "check_marketplace_stock": self.check_marketplace_stock,
            "notify_farmer": self.notify_farmer,
        }
        if tool_name not in handlers:
            raise ValueError(f"Outil MCP inconnu : {tool_name!r}.")
        return handlers[tool_name](**arguments)
