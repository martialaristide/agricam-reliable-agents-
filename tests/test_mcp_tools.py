import pytest

from agricam_reliable_agents.mcp_tools.data_store import (
    AgriCamDataError,
    AgriCamDataStore,
)
from agricam_reliable_agents.mcp_tools.tools import AgriCamTools


@pytest.fixture
def tools() -> AgriCamTools:
    store = AgriCamDataStore()
    store.seed_demo_data()
    return AgriCamTools(store)


def test_get_sensor_data_returns_expected_shape(tools: AgriCamTools):
    result = tools.get_sensor_data(parcel_id="P-003", metric="humidity")
    assert result["parcel_id"] == "P-003"
    assert len(result["readings"]) == 1
    assert result["readings"][0]["metric"] == "humidity"
    assert "timestamp" in result["readings"][0]


def test_get_sensor_data_unknown_parcel_raises(tools: AgriCamTools):
    with pytest.raises(AgriCamDataError):
        tools.get_sensor_data(parcel_id="P-DOES-NOT-EXIST")


def test_get_diagnostic_history_filters_by_parcel(tools: AgriCamTools):
    result = tools.get_diagnostic_history(parcel_id="P-003")
    assert len(result["diagnostics"]) == 1
    assert result["diagnostics"][0]["id"] == "D-42"


def test_recommend_treatment_marks_treated_and_decrements_stock(tools: AgriCamTools):
    before_stock = tools.check_marketplace_stock("PRD-7")["stock_qty"]
    result = tools.recommend_treatment("D-42")
    after_stock = tools.check_marketplace_stock("PRD-7")["stock_qty"]

    assert result["status"] == "treated"
    assert after_stock == before_stock - 1


def test_notify_farmer_records_message(tools: AgriCamTools):
    result = tools.notify_farmer(farmer_id="F-001", message="Traitement recommandé sur P-003.")
    assert result["notified"] is True


def test_dispatch_routes_to_correct_handler(tools: AgriCamTools):
    result = tools.dispatch("check_marketplace_stock", {"product_id": "PRD-7"})
    assert result["product_id"] == "PRD-7"


def test_dispatch_unknown_tool_raises_value_error(tools: AgriCamTools):
    with pytest.raises(ValueError):
        tools.dispatch("delete_everything", {})


def test_snapshot_reflects_state_change_after_treatment(tools: AgriCamTools):
    store = tools._store  # accès direct au store pour l'instantané, cf. section verifier
    before = store.snapshot()
    tools.recommend_treatment("D-42")
    after = store.snapshot()
    assert before["diagnostic.D-42.status"] == "pending"
    assert after["diagnostic.D-42.status"] == "treated"
