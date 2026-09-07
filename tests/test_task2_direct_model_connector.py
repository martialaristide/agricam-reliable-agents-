"""
Tâche 2 (Phase A) — `AgentConnector` + adaptateur « appel direct modèle ».

Critère d'acceptation : un test d'intégration fait tourner une campagne
complète (`evaluate_task` du harnais EXISTANT, non modifié) sur un agent
connecté via `AgentConnector`, avec un `ReliabilityReport` cohérent en
sortie — la dernière classe de tests ci-dessous le prouve.
"""

from __future__ import annotations

from agricam_reliable_agents.agent.llm_client import LLMClientError, LLMResponse
from agricam_reliable_agents.connector.base import (
    AgentConnector,
    ConnectorMetadata,
    RawAgentReply,
)
from agricam_reliable_agents.connector.bridge import agent_connector_as_runner
from agricam_reliable_agents.connector.direct_model_adapter import (
    CONNECTOR_TYPE,
    DirectModelAdapter,
)
from agricam_reliable_agents.models.data_models import Task, TaskComplexity
from agricam_reliable_agents.reliability.harness import HarnessConfig, evaluate_task


class ScriptedLLMClient:
    """Rejoue une séquence de réponses ; enregistre les messages reçus
    pour vérifier ce qui a réellement été envoyé au modèle."""

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self.seen_messages: list[list[dict]] = []
        self.model: str | None = None

    def generate(self, messages, tool_schemas) -> LLMResponse:
        self.seen_messages.append([dict(m) for m in messages])
        return self._responses.pop(0)


class FailingLLMClient:
    """Simule un LLM injoignable après épuisement des retries."""

    model = "claude-opus-5"

    def generate(self, messages, tool_schemas) -> LLMResponse:
        raise LLMClientError("Échec définitif après 3 tentatives : 503")


def make_task() -> Task:
    return Task(
        id="T-DIRECT", prompt="Réponds à la question posée.", complexity=TaskComplexity.LEVEL_1,
        category="generic", expected_state_delta={}, verification_query="q",
    )


# ---------------------------------------------------------------------------
# Dataclasses de base
# ---------------------------------------------------------------------------


def test_raw_agent_reply_defaults():
    reply = RawAgentReply(text="ok", declared_success=True, latency_ms=1.0)
    assert reply.cost_usd == 0.0
    assert reply.raw is None
    assert reply.error is None


def test_connector_metadata_defaults():
    meta = ConnectorMetadata(connector_type="direct_model", name="x")
    assert meta.version is None
    assert meta.capabilities == ()


# ---------------------------------------------------------------------------
# DirectModelAdapter
# ---------------------------------------------------------------------------


def test_send_happy_path_computes_declared_success_and_cost():
    llm = ScriptedLLMClient([LLMResponse(tool_call=None, final_text="Traitement appliqué avec succès.",
                                         input_tokens=1_000_000, output_tokens=1_000_000)])
    llm.model = "claude-opus-5"
    adapter = DirectModelAdapter(llm)

    reply = adapter.send("Traite le diagnostic D-42.", trial_id=0)

    assert reply.text == "Traitement appliqué avec succès."
    assert reply.declared_success is True
    assert reply.cost_usd == 5.0 + 25.0  # 1M jetons entrée + 1M sortie, tarif Opus 5
    assert reply.latency_ms >= 0.0
    assert reply.error is None
    assert reply.raw is not None


def test_send_uses_custom_success_claim_extractor():
    llm = ScriptedLLMClient([LLMResponse(tool_call=None, final_text="OK")])

    class AlwaysOk:
        def __call__(self, final_text: str) -> bool:
            return final_text.strip() == "OK"

    adapter = DirectModelAdapter(llm, success_claim_extractor=AlwaysOk())
    reply = adapter.send("Fais quelque chose.", trial_id=0)
    assert reply.declared_success is True


def test_system_prompt_is_prefixed_to_the_user_message():
    llm = ScriptedLLMClient([LLMResponse(tool_call=None, final_text="Fait.")])
    adapter = DirectModelAdapter(llm, system_prompt="Tu es un agent utile.")
    adapter.send("Traite D-42.", trial_id=0)

    sent = llm.seen_messages[0][0]["content"]
    assert sent.startswith("Tu es un agent utile.")
    assert sent.endswith("Traite D-42.")


def test_send_without_system_prompt_forwards_prompt_unchanged():
    llm = ScriptedLLMClient([LLMResponse(tool_call=None, final_text="Fait.")])
    adapter = DirectModelAdapter(llm)
    adapter.send("Traite D-42.", trial_id=0)
    assert llm.seen_messages[0][0]["content"] == "Traite D-42."


def test_send_catches_llm_client_error_as_raw_agent_reply_error():
    """Une panne de transport est renvoyée via RawAgentReply.error, jamais
    levée — même principe que run_agent avec un LLMClientError."""
    adapter = DirectModelAdapter(FailingLLMClient())
    reply = adapter.send("Traite D-42.", trial_id=0)

    assert reply.error is not None
    assert "503" in reply.error
    assert reply.declared_success is False
    assert reply.text == ""


def test_describe_reports_type_name_and_model_version():
    llm = ScriptedLLMClient([])
    llm.model = "claude-sonnet-5"
    adapter = DirectModelAdapter(llm, name="mon-agent-tiers")

    meta = adapter.describe()
    assert meta.connector_type == CONNECTOR_TYPE == "direct_model"
    assert meta.name == "mon-agent-tiers"
    assert meta.version == "claude-sonnet-5"
    assert "text" in meta.capabilities


def test_describe_handles_client_without_model_attribute():
    class BareClient:
        def generate(self, messages, tool_schemas):
            raise NotImplementedError

    adapter = DirectModelAdapter(BareClient())
    assert adapter.describe().version is None


# ---------------------------------------------------------------------------
# bridge.agent_connector_as_runner
# ---------------------------------------------------------------------------


class FakeConnector:
    """AgentConnector factice, indépendant de DirectModelAdapter, pour
    tester le pont isolément de tout LLMClient."""

    def __init__(self, replies: list[RawAgentReply]) -> None:
        self._replies = list(replies)
        self.received_prompts: list[tuple[str, int]] = []

    def send(self, prompt: str, trial_id: int) -> RawAgentReply:
        self.received_prompts.append((prompt, trial_id))
        return self._replies.pop(0)

    def describe(self) -> ConnectorMetadata:
        return ConnectorMetadata(connector_type="fake", name="fake")


def test_bridge_produces_a_conformant_agent_result():
    connector: AgentConnector = FakeConnector([
        RawAgentReply(text="Fait.", declared_success=True, latency_ms=12.5, cost_usd=0.002),
    ])
    runner = agent_connector_as_runner(connector)
    task = make_task()

    result = runner(task, trial_id=3)

    assert result.task_id == task.id
    assert result.trial_id == 3
    assert result.final_answer == "Fait."
    assert result.declared_success is True
    assert result.latency_ms == 12.5
    assert result.cost_usd == 0.002
    assert result.tool_calls == ()  # boîte noire : jamais de visibilité sur les outils internes
    assert result.max_steps_exceeded is False
    assert result.error is None


def test_bridge_forwards_the_task_prompt_to_the_connector():
    connector = FakeConnector([RawAgentReply(text="x", declared_success=False, latency_ms=1.0)])
    runner = agent_connector_as_runner(connector)
    task = make_task()

    runner(task, trial_id=0)

    assert connector.received_prompts == [(task.prompt, 0)]


def test_bridge_propagates_connector_error_field():
    connector = FakeConnector([
        RawAgentReply(text="", declared_success=False, latency_ms=5.0, error="timeout"),
    ])
    runner = agent_connector_as_runner(connector)
    result = runner(make_task(), trial_id=0)
    assert result.error == "timeout"


# ---------------------------------------------------------------------------
# Intégration bout en bout : connecteur générique + harnais EXISTANT
# ---------------------------------------------------------------------------


def test_full_campaign_through_the_existing_harness_with_a_connected_agent():
    """Preuve d'interopérabilité réelle, pas seulement théorique : le
    Connecteur d'Agent Générique et `reliability/harness.py` (non modifié)
    fonctionnent ensemble à travers `evaluate_task`."""
    llm = ScriptedLLMClient([
        LLMResponse(tool_call=None, final_text="Réponse correcte, traitement effectué.")
        for _ in range(5)
    ])
    adapter = DirectModelAdapter(llm)
    runner = agent_connector_as_runner(adapter)
    task = make_task()

    report = evaluate_task(
        task, runner, state_snapshot_fn=lambda t: {},  # connecteur "boîte noire" sans effet d'état observable
        config=HarnessConfig(n_trials=5, k_values=(1, 3)),
    )

    assert report.task_id == task.id
    assert report.n_trials == 5
    # expected_state_delta vide -> matches_expected toujours vrai -> tous
    # les essais comptent comme réussis, quel que soit declared_success :
    # preuve que le Success Verifier (non modifié) a bien été consulté.
    assert report.n_success == 5
    assert report.p_hat == 1.0
    assert report.pass_k[1] == 1.0
    assert report.pass_k[3] == 1.0
    assert 0.0 <= report.wilson_ci[0] <= report.wilson_ci[1] <= 1.0
    assert len(llm.seen_messages) == 5
