from agricam_reliable_agents.agent.llm_client import LLMResponse, LLMToolCallRequest
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

    def generate(self, messages, tool_schemas) -> LLMResponse:
        response = self._responses[self.call_count]
        self.call_count += 1
        return response


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
