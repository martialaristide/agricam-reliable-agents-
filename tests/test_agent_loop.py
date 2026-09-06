from agricam_reliable_agents.agent.llm_client import (
    LLMResponse,
    LLMToolCallRequest,
    LLMTransientError,
)
from agricam_reliable_agents.agent.loop import extract_success_claim, run_agent
from agricam_reliable_agents.mcp_tools.data_store import AgriCamDataStore
from agricam_reliable_agents.mcp_tools.tools import AgriCamTools
from agricam_reliable_agents.models.data_models import Task, TaskComplexity
from agricam_reliable_agents.security.policy import ActionPolicy


class ScriptedLLMClient:
    """Faux client LLM qui rejoue une séquence de réponses fixée à l'avance.
    Permet de tester la boucle agent sans dépendre d'une vraie API."""

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self.call_count = 0
        self.seen_messages: list[list[dict]] = []

    def generate(self, messages, tool_schemas) -> LLMResponse:
        self.seen_messages.append([dict(m) for m in messages])
        response = self._responses[self.call_count]
        self.call_count += 1
        return response


class FailingLLMClient:
    """Faux client qui simule un LLM injoignable après épuisement des retries."""

    def generate(self, messages, tool_schemas) -> LLMResponse:
        raise LLMTransientError("Échec définitif après 3 tentatives : 429")


def make_tools() -> AgriCamTools:
    store = AgriCamDataStore()
    store.seed_demo_data()
    return AgriCamTools(store)


def make_task() -> Task:
    return Task(
        id="T-INT-1",
        prompt="Traite le diagnostic D-42 et notifie l'exploitant.",
        complexity=TaskComplexity.LEVEL_2,
        category="treatment",
        expected_state_delta={"diagnostic.D-42.status": "treated"},
        verification_query="q-int-1",
    )


def test_extract_success_claim_positive_and_negative_cases():
    assert extract_success_claim("Traitement appliqué avec succès.") is True
    assert extract_success_claim("Je n'ai pas pu terminer l'action.") is False
    assert extract_success_claim("Voici les données demandées.") is False


def test_run_agent_full_success_path():
    llm = ScriptedLLMClient([
        LLMResponse(
            tool_call=LLMToolCallRequest(id="c1", name="recommend_treatment",
                                          arguments={"diagnostic_id": "D-42"}),
            final_text=None, input_tokens=100, output_tokens=20,
        ),
        LLMResponse(tool_call=None, final_text="Traitement appliqué avec succès.",
                    input_tokens=110, output_tokens=15),
    ])
    tools = make_tools()
    policy = ActionPolicy()  # aucune restriction : outil non-sensible ici

    result = run_agent(make_task(), trial_id=0, llm_client=llm, tools=tools, policy=policy)

    assert result.declared_success is True
    assert result.max_steps_exceeded is False
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].tool_name == "recommend_treatment"
    assert result.cost_usd > 0.0

    # Vérifie que l'état réel a bien changé (couplage avec le data store)
    after_state = tools._store.snapshot()
    assert after_state["diagnostic.D-42.status"] == "treated"


def test_run_agent_stops_at_max_steps_without_final_answer():
    # Le faux LLM ne produit jamais de réponse finale : boucle infinie simulée
    infinite_tool_call = LLMResponse(
        tool_call=LLMToolCallRequest(id="c", name="check_marketplace_stock",
                                      arguments={"product_id": "PRD-7"}),
        final_text=None, input_tokens=50, output_tokens=10,
    )
    llm = ScriptedLLMClient([infinite_tool_call] * 10)
    tools = make_tools()
    policy = ActionPolicy()

    result = run_agent(make_task(), trial_id=0, llm_client=llm, tools=tools,
                        policy=policy, max_steps=5)

    assert result.max_steps_exceeded is True
    assert result.final_answer == "MAX_STEPS_EXCEEDED"
    assert result.declared_success is False
    assert llm.call_count == 5  # jamais plus que max_steps appels au modèle


def test_run_agent_blocks_sensitive_action_outside_scope():
    llm = ScriptedLLMClient([
        LLMResponse(
            tool_call=LLMToolCallRequest(
                id="c1", name="notify_farmer",
                arguments={"farmer_id": "F-999", "message": "Test"},  # hors périmètre
            ),
            final_text=None, input_tokens=50, output_tokens=10,
        ),
        LLMResponse(tool_call=None, final_text="Notification envoyée.",
                    input_tokens=60, output_tokens=10),
    ])
    tools = make_tools()
    policy = ActionPolicy(allowed_farmer_ids=frozenset({"F-001"}))  # F-999 non autorisé

    incidents = []
    result = run_agent(
        make_task(), trial_id=0, llm_client=llm, tools=tools, policy=policy,
        incident_sink=incidents.append,
    )

    # L'agent DÉCLARE un succès (il ne sait pas qu'il a été bloqué),
    # mais aucune notification n'a réellement été envoyée : c'est
    # exactement le cas de sur-confiance que le Success Verifier détectera.
    assert result.declared_success is True
    assert len(incidents) == 1
    assert incidents[0].blocked is True


def test_run_agent_recovers_from_tool_error():
    llm = ScriptedLLMClient([
        LLMResponse(
            tool_call=LLMToolCallRequest(id="c1", name="recommend_treatment",
                                          arguments={"diagnostic_id": "D-DOES-NOT-EXIST"}),
            final_text=None, input_tokens=50, output_tokens=10,
        ),
        LLMResponse(tool_call=None, final_text="Je n'ai pas pu terminer l'action : diagnostic introuvable.",
                    input_tokens=70, output_tokens=15),
    ])
    tools = make_tools()
    policy = ActionPolicy()

    # Ne doit lever aucune exception : l'erreur métier est absorbée et
    # transmise à l'agent comme un résultat d'outil en échec.
    result = run_agent(make_task(), trial_id=0, llm_client=llm, tools=tools, policy=policy)
    assert result.declared_success is False
    assert result.tool_calls == ()  # l'appel a échoué, donc non comptabilisé comme réussi


def test_run_agent_builds_anthropic_tool_use_and_tool_result_blocks():
    """L'historique transmis au modèle doit suivre le format de l'API
    Messages : bloc `tool_use` côté assistant, bloc `tool_result` côté
    utilisateur, reliés par `tool_use_id`. C'est ce format qu'attend le
    client Anthropic réel."""
    llm = ScriptedLLMClient([
        LLMResponse(
            tool_call=LLMToolCallRequest(id="toolu_abc", name="check_marketplace_stock",
                                          arguments={"product_id": "PRD-7"}),
            final_text="Je vérifie le stock.", input_tokens=10, output_tokens=5,
        ),
        LLMResponse(tool_call=None, final_text="Stock vérifié : 25 unités.",
                    input_tokens=20, output_tokens=5),
    ])
    run_agent(make_task(), trial_id=0, llm_client=llm, tools=make_tools(), policy=ActionPolicy())

    second_call_messages = llm.seen_messages[1]
    assert [m["role"] for m in second_call_messages] == ["user", "assistant", "user"]

    assistant_blocks = second_call_messages[1]["content"]
    assert assistant_blocks[0] == {"type": "text", "text": "Je vérifie le stock."}
    assert assistant_blocks[1]["type"] == "tool_use"
    assert assistant_blocks[1]["id"] == "toolu_abc"
    assert assistant_blocks[1]["input"] == {"product_id": "PRD-7"}

    result_block = second_call_messages[2]["content"][0]
    assert result_block["type"] == "tool_result"
    assert result_block["tool_use_id"] == "toolu_abc"
    assert '"stock_qty": 25' in result_block["content"]  # JSON, pas repr Python
    assert "is_error" not in result_block


def test_run_agent_marks_refused_and_failed_tool_results_as_errors():
    llm = ScriptedLLMClient([
        LLMResponse(
            tool_call=LLMToolCallRequest(id="t1", name="notify_farmer",
                                          arguments={"farmer_id": "F-999", "message": "x"}),
            final_text=None, input_tokens=1, output_tokens=1,
        ),
        LLMResponse(
            tool_call=LLMToolCallRequest(id="t2", name="recommend_treatment",
                                          arguments={"diagnostic_id": "D-NOPE"}),
            final_text=None, input_tokens=1, output_tokens=1,
        ),
        LLMResponse(tool_call=None, final_text="Impossible.", input_tokens=1, output_tokens=1),
    ])
    policy = ActionPolicy(allowed_farmer_ids=frozenset({"F-001"}))
    run_agent(make_task(), trial_id=0, llm_client=llm, tools=make_tools(), policy=policy)

    refused = llm.seen_messages[1][-1]["content"][0]
    failed = llm.seen_messages[2][-1]["content"][0]
    assert refused["is_error"] is True and "refusée" in refused["content"]
    assert failed["is_error"] is True and "Erreur outil" in failed["content"]


def test_run_agent_turns_llm_failure_into_failed_result_without_raising():
    """Un LLM injoignable ne doit pas faire planter la campagne : l'essai
    est comptabilisé comme un échec explicite (error renseigné)."""
    result = run_agent(make_task(), trial_id=3, llm_client=FailingLLMClient(),
                       tools=make_tools(), policy=ActionPolicy())

    assert result.error is not None
    assert "429" in result.error
    assert result.declared_success is False
    assert result.max_steps_exceeded is False
    assert result.final_answer.startswith("LLM_ERROR")
    assert result.trial_id == 3


def test_run_agent_executes_sensitive_action_when_confirmed():
    """Avec un fournisseur de confirmation positif et un périmètre valide,
    l'action sensible est réellement exécutée (message enregistré)."""
    llm = ScriptedLLMClient([
        LLMResponse(
            tool_call=LLMToolCallRequest(id="t1", name="notify_farmer",
                                          arguments={"farmer_id": "F-001", "message": "Traitement prêt."}),
            final_text=None, input_tokens=1, output_tokens=1,
        ),
        LLMResponse(tool_call=None, final_text="Notification envoyée.", input_tokens=1, output_tokens=1),
    ])
    tools = make_tools()
    confirmations = []

    def confirm(call):
        confirmations.append(call.tool_name)
        return True

    result = run_agent(make_task(), trial_id=0, llm_client=llm, tools=tools,
                       policy=ActionPolicy(allowed_farmer_ids=frozenset({"F-001"})),
                       confirmation_provider=confirm)

    assert confirmations == ["notify_farmer"]
    assert [c.tool_name for c in result.tool_calls] == ["notify_farmer"]
    assert tools._store.snapshot()["farmer.F-001.notified_count"] == 1
