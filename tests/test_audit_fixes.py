"""
Tests des corrections issues de l'audit expert du 7 septembre 2026.

Chaque test cite le point d'audit qu'il verrouille (B = bloquant,
I = important, M = mineur), pour qu'une régression soit traçable.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agricam_reliable_agents.agent.llm_client import (
    AnthropicLLMClient,
    LLMResponse,
    LLMToolCallRequest,
)
from agricam_reliable_agents.agent.loop import (
    extract_success_claim,
    pricing_for_model,
    run_agent,
)
from agricam_reliable_agents.mcp_tools.data_store import (
    AgriCamDataError,
    AgriCamDataStore,
    DataStore,
)
from agricam_reliable_agents.mcp_tools.sql_store import SqlAlchemyDataStore
from agricam_reliable_agents.mcp_tools.tools import AgriCamTools
from agricam_reliable_agents.models.data_models import (
    AgentResult,
    Task,
    TaskComplexity,
    ToolCall,
    VerificationResult,
)
from agricam_reliable_agents.persistence.engine import (
    create_engine_from_url,
    is_memory_sqlite_url,
)
from agricam_reliable_agents.reliability.repository import ReliabilityRepository
from agricam_reliable_agents.reliability.stats import pass_k_interval
from agricam_reliable_agents.security.policy import ActionPolicy

# ---------------------------------------------------------------------------
# Fabriques
# ---------------------------------------------------------------------------


class ScriptedLLMClient:
    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self.seen_messages: list[list[dict]] = []

    def generate(self, messages, tool_schemas) -> LLMResponse:
        self.seen_messages.append([dict(m) for m in messages])
        return self._responses.pop(0)


def make_tools() -> AgriCamTools:
    store = AgriCamDataStore()
    store.seed_demo_data()
    return AgriCamTools(store)


def make_task() -> Task:
    return Task(id="T-AUDIT", prompt="Traite D-42 et notifie F-001.", complexity=TaskComplexity.LEVEL_2,
                category="treatment", expected_state_delta={"diagnostic.D-42.status": "treated"},
                verification_query="q")


def _usage(inp=100, out=20, **extra):
    return SimpleNamespace(input_tokens=inp, output_tokens=out, **extra)


def _sdk_response(*blocks, stop_reason="end_turn", usage=None):
    return SimpleNamespace(content=list(blocks), stop_reason=stop_reason, usage=usage or _usage(), stop_details=None)


def _tool_use(tool_id, name, arguments):
    return SimpleNamespace(type="tool_use", id=tool_id, name=name, input=arguments)


def _thinking():
    return SimpleNamespace(type="thinking", thinking="", signature="sig-123")


def _text(text):
    return SimpleNamespace(type="text", text=text)


# ---------------------------------------------------------------------------
# B1 / B2 : appels parallèles et contenu brut rejoué tel quel
# ---------------------------------------------------------------------------


def test_b1_all_parallel_tool_uses_are_normalized_and_answered_in_one_message():
    sdk = MagicMock()
    sdk.messages.create.side_effect = [
        _sdk_response(
            _thinking(), _text("Je lis les données."),
            _tool_use("t1", "get_sensor_data", {"parcel_id": "P-003"}),
            _tool_use("t2", "check_marketplace_stock", {"product_id": "PRD-7"}),
            stop_reason="tool_use",
        ),
        _sdk_response(_text("Lecture terminée."), stop_reason="end_turn"),
    ]
    client = AnthropicLLMClient(client=sdk, sleep=lambda s: None)
    result = run_agent(make_task(), 0, client, make_tools(), ActionPolicy())

    assert [c.tool_name for c in result.tool_calls] == ["get_sensor_data", "check_marketplace_stock"]
    second_request = sdk.messages.create.call_args_list[1].kwargs
    messages = second_request["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    # B2 : le tour assistant est le contenu brut (bloc thinking + signature intacts)
    assistant_blocks = messages[1]["content"]
    assert assistant_blocks[0].type == "thinking" and assistant_blocks[0].signature == "sig-123"
    assert [b.type for b in assistant_blocks] == ["thinking", "text", "tool_use", "tool_use"]
    # B1 : tous les tool_result dans un seul message utilisateur, dans l'ordre
    results = messages[2]["content"]
    assert [r["tool_use_id"] for r in results] == ["t1", "t2"]
    assert all(r["type"] == "tool_result" for r in results)


def test_b1_normalize_exposes_every_tool_call_and_raw_content():
    sdk = MagicMock()
    sdk.messages.create.side_effect = [
        _sdk_response(_tool_use("a", "x", {}), _tool_use("b", "y", {"k": 1}), stop_reason="tool_use",
                      usage=_usage(10, 5, cache_read_input_tokens=90, cache_creation_input_tokens=0)),
    ]
    response = AnthropicLLMClient(client=sdk, sleep=lambda s: None).generate([], [])
    assert [c.id for c in response.tool_calls] == ["a", "b"]
    assert response.tool_call.id == "a" and response.extra_tool_calls[0].id == "b"
    assert response.stop_reason == "tool_use"
    assert [b.type for b in response.raw_content] == ["tool_use", "tool_use"]
    assert response.input_tokens == 100  # jetons de cache comptés (approximation prudente)


def test_scripted_clients_without_raw_content_still_get_a_rebuilt_assistant_turn():
    llm = ScriptedLLMClient([
        LLMResponse(tool_call=LLMToolCallRequest("c1", "check_marketplace_stock", {"product_id": "PRD-7"}),
                    final_text="Je vérifie.",
                    extra_tool_calls=(LLMToolCallRequest("c2", "get_sensor_data", {"parcel_id": "P-003"}),)),
        LLMResponse(tool_call=None, final_text="Fait."),
    ])
    run_agent(make_task(), 0, llm, make_tools(), ActionPolicy())
    assistant = llm.seen_messages[1][1]["content"]
    assert [b["type"] for b in assistant] == ["text", "tool_use", "tool_use"]
    assert [b["type"] for b in llm.seen_messages[1][2]["content"]] == ["tool_result", "tool_result"]


# ---------------------------------------------------------------------------
# I1 : arguments hors schéma -> résultat d'outil en erreur, campagne intacte
# ---------------------------------------------------------------------------


def test_i1_invalid_arguments_become_tool_error_not_exception():
    llm = ScriptedLLMClient([
        LLMResponse(tool_call=LLMToolCallRequest("c1", "get_sensor_data", {"parcel_id": "P-003", "metrik": "all"}),
                    final_text=None),
        LLMResponse(tool_call=LLMToolCallRequest("c2", "delete_everything", {}), final_text=None),
        LLMResponse(tool_call=None, final_text="Impossible."),
    ])
    result = run_agent(make_task(), 0, llm, make_tools(), ActionPolicy())
    assert [c.status for c in result.tool_calls] == ["error", "error"]
    assert "invalide" in llm.seen_messages[1][-1]["content"][0]["content"]
    assert llm.seen_messages[1][-1]["content"][0]["is_error"] is True
    assert result.error is None  # ce n'est pas une panne d'infrastructure


# ---------------------------------------------------------------------------
# I2 : réponse tronquée (max_tokens) archivée comme non concluante
# ---------------------------------------------------------------------------


def test_i2_max_tokens_is_reported_as_error_not_as_agent_failure():
    llm = ScriptedLLMClient([LLMResponse(tool_call=None, final_text="", stop_reason="max_tokens")])
    result = run_agent(make_task(), 0, llm, make_tools(), ActionPolicy())
    assert result.error is not None and "max_tokens" in result.error
    assert result.declared_success is False and result.max_steps_exceeded is False


# ---------------------------------------------------------------------------
# I3 : formulations réelles de succès / d'échec
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "Le diagnostic D-42 est désormais marqué comme traité et l'exploitant F-001 a été notifié.",
    "Traitement appliqué, aucune erreur rencontrée.",
    "Le stock a été mis à jour et la notification envoyée.",
    "The treatment has been applied successfully.",
    "C'est fait.",
])
def test_i3_success_claims_are_recognized(text):
    assert extract_success_claim(text) is True


@pytest.mark.parametrize("text", [
    "Je n'ai pas fait le traitement : diagnostic introuvable.",
    "Le traitement a été appliqué mais la notification a échoué.",
    "Erreur outil : diagnostic inconnu, je n'ai pas pu continuer.",
    "Unable to notify the farmer.",
    "Voici les données demandées.",
])
def test_i3_failures_and_neutral_answers_are_not_success_claims(text):
    assert extract_success_claim(text) is False


# ---------------------------------------------------------------------------
# I4 : le périmètre de parcelle s'applique à recommend_treatment
# ---------------------------------------------------------------------------


def test_i4_treatment_on_diagnostic_of_another_parcel_is_blocked():
    llm = ScriptedLLMClient([
        LLMResponse(tool_call=LLMToolCallRequest("c1", "recommend_treatment", {"diagnostic_id": "D-42"}),
                    final_text=None),
        LLMResponse(tool_call=None, final_text="Traitement appliqué."),
    ])
    tools = make_tools()
    incidents = []
    policy = ActionPolicy(allowed_parcel_ids=frozenset({"P-001"}))  # D-42 est sur P-003
    result = run_agent(make_task(), 0, llm, tools, policy, incident_sink=incidents.append)
    assert [c.status for c in result.tool_calls] == ["refused"]
    assert tools.store.snapshot()["diagnostic.D-42.status"] == "pending"
    assert len(incidents) == 1 and '"diagnostic_id": "D-42"' in incidents[0].payload


def test_i4_treatment_within_parcel_scope_is_executed():
    llm = ScriptedLLMClient([
        LLMResponse(tool_call=LLMToolCallRequest("c1", "recommend_treatment", {"diagnostic_id": "D-42"}),
                    final_text=None),
        LLMResponse(tool_call=None, final_text="Traitement appliqué."),
    ])
    tools = make_tools()
    result = run_agent(make_task(), 0, llm, tools, ActionPolicy(allowed_parcel_ids=frozenset({"P-003"})))
    assert [c.status for c in result.tool_calls] == ["ok"]
    assert result.tool_calls[0].arguments == {"diagnostic_id": "D-42"}  # l'appel exécuté n'est pas altéré
    assert tools.store.snapshot()["diagnostic.D-42.status"] == "treated"


def test_i4_unknown_diagnostic_falls_through_to_tool_error():
    llm = ScriptedLLMClient([
        LLMResponse(tool_call=LLMToolCallRequest("c1", "recommend_treatment", {"diagnostic_id": "D-404"}),
                    final_text=None),
        LLMResponse(tool_call=None, final_text="Échec."),
    ])
    result = run_agent(make_task(), 0, llm, make_tools(), ActionPolicy(allowed_parcel_ids=frozenset({"P-003"})))
    assert [c.status for c in result.tool_calls] == ["error"]


# ---------------------------------------------------------------------------
# I5 / I6 / M3 : contrat DataStore identique entre mémoire et SQL
# ---------------------------------------------------------------------------


@pytest.fixture(params=["memory", "sql"])
def store(request) -> DataStore:
    s: DataStore = AgriCamDataStore() if request.param == "memory" else SqlAlchemyDataStore.from_url("sqlite://")
    return s


def test_i5_seed_is_idempotent_and_reset_empties_both_stores(store: DataStore):
    assert store.is_empty()
    store.seed_demo_data()
    store.mark_diagnostic_treated("D-42")
    store.notify_farmer("F-001", "x")
    store.seed_demo_data()  # sans effet : ne réinitialise ni ne duplique
    assert store.snapshot()["diagnostic.D-42.status"] == "treated"
    assert len(store.get_sensor_data("P-003")) == 1
    store.reset()
    assert store.is_empty() and store.snapshot() == {}
    store.seed_demo_data()
    assert store.snapshot()["diagnostic.D-42.status"] == "pending"


def test_m3_limit_and_quantity_contract_is_identical(store: DataStore):
    store.seed_demo_data()
    assert store.get_diagnostic_history(limit=0) == []
    assert store.get_diagnostic_history(limit=-1) == []
    with pytest.raises(ValueError):
        store.decrement_stock("PRD-7", 0)
    with pytest.raises(ValueError):
        store.decrement_stock("PRD-7", -5)
    assert store.get_product("PRD-7").stock_qty == 25


def test_i6_sql_is_empty_looks_at_every_table():
    sql = SqlAlchemyDataStore.from_url("sqlite://")
    sql.seed_demo_data()
    from sqlalchemy import delete

    from agricam_reliable_agents.mcp_tools.sql_store import DiagnosticRow

    with sql._session() as session, session.begin():
        session.execute(delete(DiagnosticRow))
    assert not sql.is_empty()
    sql.seed_demo_data()  # ne doit pas lever d'IntegrityError sur PRD-7 / F-001
    assert sql.get_diagnostic_history() == []


# ---------------------------------------------------------------------------
# M4 / M9 : outils
# ---------------------------------------------------------------------------


def test_m4_second_treatment_does_not_consume_stock_again():
    tools = make_tools()
    tools.recommend_treatment("D-42")
    with pytest.raises(AgriCamDataError, match="déjà traité"):
        tools.recommend_treatment("D-42")
    assert tools.check_marketplace_stock("PRD-7")["stock_qty"] == 24


def test_m9_notification_message_length_is_enforced_before_the_store():
    tools = make_tools()
    with pytest.raises(AgriCamDataError, match="trop long"):
        tools.notify_farmer("F-001", "x" * 301)
    with pytest.raises(AgriCamDataError, match="vide"):
        tools.notify_farmer("F-001", "   ")
    assert tools.notify_farmer("F-001", "x" * 300)["notified"] is True


# ---------------------------------------------------------------------------
# I7 / I8 / I9 : dépôt
# ---------------------------------------------------------------------------


def test_i7_trial_keeps_the_task_definition_it_was_judged_with():
    repo = ReliabilityRepository.from_url("sqlite://")
    task_v1 = make_task()
    cid = repo.start_campaign("v1")
    observer = repo.trial_observer(cid, [task_v1])
    result = AgentResult(task_id=task_v1.id, trial_id=0, final_answer="Fait.", tool_calls=(),
                         declared_success=True, latency_ms=1.0, cost_usd=0.0)
    verification = VerificationResult(task_id=task_v1.id, trial_id=0, actual_state_delta={},
                                      matches_expected=False, overconfidence_detected=True)
    observer(result, verification)
    # La tâche est ensuite réécrite (autre attendu, autre complexité)
    task_v2 = Task(id=task_v1.id, prompt="v2", complexity=TaskComplexity.LEVEL_3, category="c",
                   expected_state_delta={"x": 1}, verification_query="q")
    repo.record_task(task_v2)
    [trial] = repo.list_trials(cid)
    assert trial.expected_state_delta == {"diagnostic.D-42.status": "treated"}
    assert trial.complexity == "3-5_steps"
    assert repo.get_task(task_v1.id).complexity is TaskComplexity.LEVEL_3
    with pytest.raises(ValueError):
        repo.record_trial(cid, result, verification, task=task_v2._replace(id="AUTRE") if hasattr(task_v2, "_replace") else Task(
            id="AUTRE", prompt="p", complexity=TaskComplexity.LEVEL_1, category="c",
            expected_state_delta={}, verification_query="q"))


def test_i8_tool_call_statuses_round_trip():
    repo = ReliabilityRepository.from_url("sqlite://")
    cid = repo.start_campaign("c")
    calls = (ToolCall("notify_farmer", {"farmer_id": "F-404"}, status="refused", error="refusé"),
             ToolCall("recommend_treatment", {"diagnostic_id": "D-42"}))
    result = AgentResult(task_id="T", trial_id=0, final_answer="Fait.", tool_calls=calls,
                         declared_success=True, latency_ms=1.0, cost_usd=0.0)
    repo.record_trial(cid, result, VerificationResult("T", 0, {}, True, False))
    [trial] = repo.list_trials(cid)
    assert [(c.tool_name, c.status, c.error) for c in trial.tool_calls] == [
        ("notify_farmer", "refused", "refusé"), ("recommend_treatment", "ok", None),
    ]
    assert trial.successful_tool_call_names == ("recommend_treatment",)


def test_i9_incident_task_id_round_trip():
    from agricam_reliable_agents.models.data_models import (
        AttackCategory,
        SecurityIncident,
    )

    repo = ReliabilityRepository.from_url("sqlite://")
    cid = repo.start_campaign("c")
    repo.record_incident(cid, SecurityIncident(3, AttackCategory.EXCESSIVE_AGENCY, "{}", True, task_id="T2"))
    [incident] = repo.list_incidents(cid)
    assert incident.task_id == "T2"


# ---------------------------------------------------------------------------
# M1 / M2 / M5 / M6
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("url,expected", [
    ("sqlite://", True), ("sqlite:///:memory:", True), ("sqlite+pysqlite:///:memory:", True),
    ("sqlite:///file:x?mode=memory&cache=shared&uri=true", True),
    ("sqlite:///agricam.db", False), ("postgresql+psycopg://u:p@h/db", False),
])
def test_m2_memory_url_detection(url, expected):
    assert is_memory_sqlite_url(url) is expected


def test_m2_explicit_driver_memory_url_shares_state_between_connections():
    from sqlalchemy import text
    engine = create_engine_from_url("sqlite+pysqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE t (x INTEGER)"))
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM t")).scalar() == 0


def test_m5_pass_k_interval_is_the_image_of_wilson_interval():
    assert pass_k_interval((0.5, 0.9), 1) == (0.5, 0.9)
    low, high = pass_k_interval((0.5, 0.9), 3)
    assert (low, high) == (pytest.approx(0.125), pytest.approx(0.729))
    with pytest.raises(ValueError):
        pass_k_interval((0.9, 0.5), 2)


def test_m6_cost_follows_the_configured_model():
    assert pricing_for_model("claude-opus-5") == (5.0, 25.0)
    assert pricing_for_model("claude-sonnet-5") == (2.0, 10.0)
    assert pricing_for_model("modele-inconnu") == (5.0, 25.0)
    assert pricing_for_model(None) == (5.0, 25.0)

    class SonnetClient(ScriptedLLMClient):
        model = "claude-sonnet-5"

    llm = SonnetClient([LLMResponse(tool_call=None, final_text="Fait.", input_tokens=1_000_000, output_tokens=0)])
    result = run_agent(make_task(), 0, llm, make_tools(), ActionPolicy())
    assert result.cost_usd == pytest.approx(2.0)
    llm = ScriptedLLMClient([LLMResponse(tool_call=None, final_text="Fait.", input_tokens=1_000_000, output_tokens=0)])
    assert run_agent(make_task(), 0, llm, make_tools(), ActionPolicy(), pricing=(1.0, 5.0)).cost_usd == pytest.approx(1.0)
