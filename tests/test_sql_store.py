"""
Tests du store persistant SQLAlchemy.

Le store SQL doit être strictement interchangeable avec le store en
mémoire : la plupart des tests ci-dessous sont paramétrés sur les deux
implémentations pour le prouver (même contrat, mêmes erreurs, même
instantané), puis quelques tests couvrent ce qui n'existe qu'en SQL
(persistance entre deux instances, idempotence du seed, reset,
décrément atomique).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agricam_reliable_agents.mcp_tools.data_store import (
    AgriCamDataError,
    AgriCamDataStore,
    DataStore,
)
from agricam_reliable_agents.mcp_tools.sql_store import SqlAlchemyDataStore
from agricam_reliable_agents.mcp_tools.tools import AgriCamTools
from agricam_reliable_agents.persistence.engine import create_engine_from_url


@pytest.fixture(params=["memory", "sql"])
def store(request: pytest.FixtureRequest) -> DataStore:
    if request.param == "memory":
        s: DataStore = AgriCamDataStore()
    else:
        s = SqlAlchemyDataStore.from_url("sqlite://")
    s.seed_demo_data()
    return s


# ---- Contrat commun (mémoire et SQL) -----------------------------------------

def test_snapshot_identical_between_implementations():
    memory = AgriCamDataStore()
    memory.seed_demo_data()
    sql = SqlAlchemyDataStore.from_url("sqlite://")
    sql.seed_demo_data()
    assert memory.snapshot() == sql.snapshot()


def test_get_sensor_data_filters_by_metric_and_parcel(store: DataStore):
    readings = store.get_sensor_data("P-003", "humidity")
    assert [r.value for r in readings] == [82.0]
    assert readings[0].timestamp.tzinfo is not None  # toujours UTC, même relu de SQLite
    with pytest.raises(AgriCamDataError):
        store.get_sensor_data("P-003", "soil_ph")
    with pytest.raises(AgriCamDataError):
        store.get_sensor_data("P-404")


def test_get_diagnostic_and_history(store: DataStore):
    diag = store.get_diagnostic("D-42")
    assert (diag.parcel_id, diag.disease, diag.status) == ("P-003", "mildiou", "pending")
    assert [d.id for d in store.get_diagnostic_history("P-003")] == ["D-42"]
    assert store.get_diagnostic_history("P-001") == []
    assert store.get_diagnostic_history(limit=0) == []
    with pytest.raises(AgriCamDataError):
        store.get_diagnostic("D-404")


def test_get_product_and_farmer(store: DataStore):
    assert store.get_product("PRD-7").stock_qty == 25
    farmer = store.get_farmer_for_parcel("P-003")
    assert (farmer.id, farmer.name, farmer.notified_messages) == ("F-001", "Jean Mballa", [])
    with pytest.raises(AgriCamDataError):
        store.get_product("PRD-404")
    with pytest.raises(AgriCamDataError):
        store.get_farmer_for_parcel("P-001")


def test_writes_are_reflected_in_snapshot(store: DataStore):
    store.mark_diagnostic_treated("D-42")
    store.decrement_stock("PRD-7", 3)
    store.notify_farmer("F-001", "Traitement prêt.")
    store.notify_farmer("F-001", "Rappel.")

    assert store.snapshot() == {
        "diagnostic.D-42.status": "treated",
        "product.PRD-7.stock_qty": 22,
        "farmer.F-001.notified_count": 2,
    }
    assert store.get_farmer_for_parcel("P-003").notified_messages == ["Traitement prêt.", "Rappel."]


def test_write_errors(store: DataStore):
    with pytest.raises(AgriCamDataError):
        store.mark_diagnostic_treated("D-404")
    with pytest.raises(AgriCamDataError, match="inconnu"):
        store.decrement_stock("PRD-404")
    with pytest.raises(AgriCamDataError, match="insuffisant"):
        store.decrement_stock("PRD-7", 26)
    assert store.get_product("PRD-7").stock_qty == 25  # rien n'a été décrémenté
    with pytest.raises(AgriCamDataError):
        store.notify_farmer("F-404", "x")


def test_tools_work_unchanged_on_sql_store():
    """`AgriCamTools` ne dépend que du protocole : il fonctionne tel quel sur SQL."""
    sql = SqlAlchemyDataStore.from_url("sqlite://")
    sql.seed_demo_data()
    tools = AgriCamTools(sql)
    assert tools.store is sql

    assert tools.recommend_treatment("D-42")["status"] == "treated"
    assert tools.check_marketplace_stock("PRD-7")["stock_qty"] == 24
    assert tools.dispatch("notify_farmer", {"farmer_id": "F-001", "message": "ok"})["notified"] is True
    assert sql.snapshot()["farmer.F-001.notified_count"] == 1


# ---- Spécifique au store SQL ---------------------------------------------------

def test_seed_is_idempotent_and_reset_empties_tables():
    sql = SqlAlchemyDataStore.from_url("sqlite://")
    assert sql.is_empty()
    sql.seed_demo_data()
    sql.seed_demo_data()  # second appel : aucun doublon
    assert len(sql.get_sensor_data("P-001")) == 1
    assert len(sql.get_diagnostic_history()) == 1

    sql.notify_farmer("F-001", "x")
    sql.reset()
    assert sql.is_empty()
    assert sql.snapshot() == {}
    with pytest.raises(AgriCamDataError):
        sql.get_product("PRD-7")


def test_state_survives_across_store_instances_on_same_file(tmp_path: Path):
    url = f"sqlite:///{(tmp_path / 'agricam.db').as_posix()}"
    first = SqlAlchemyDataStore.from_url(url)
    first.seed_demo_data()
    first.mark_diagnostic_treated("D-42")
    first.engine.dispose()

    second = SqlAlchemyDataStore.from_url(url)
    second.seed_demo_data()  # base non vide : ne réinitialise pas le statut
    assert second.get_diagnostic("D-42").status == "treated"
    second.engine.dispose()


def test_store_accepts_prebuilt_engine():
    engine = create_engine_from_url("sqlite://")
    sql = SqlAlchemyDataStore(engine)
    assert sql.engine is engine
    sql.seed_demo_data()
    assert sql.get_product("PRD-7").name == "Fongicide bio FB-12"
