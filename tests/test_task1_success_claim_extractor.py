"""
Tâche 1 — `extract_success_claim` configurable par connexion d'agent.

Chaque agent tiers formule un succès différemment (section 12 du document
de référence) : le classifieur par motifs, câblé en dur, produirait de
faux taux de sur-confiance appliqué tel quel à un agent non-AgriCam.
"""

from __future__ import annotations

from agricam_reliable_agents.agent.llm_client import LLMResponse, LLMToolCallRequest
from agricam_reliable_agents.agent.loop import (
    DEFAULT_SUCCESS_CLAIM_EXTRACTOR,
    PatternSuccessClaimExtractor,
    SuccessClaimExtractor,
    extract_success_claim,
    run_agent,
)
from agricam_reliable_agents.mcp_tools.data_store import AgriCamDataStore
from agricam_reliable_agents.mcp_tools.tools import AgriCamTools
from agricam_reliable_agents.models.data_models import Task, TaskComplexity
from agricam_reliable_agents.security.policy import ActionPolicy


class ScriptedLLMClient:
    """Rejoue une séquence de réponses fixée à l'avance (déjà utilisé
    ailleurs dans le projet, ex. tests/test_agent_loop.py)."""

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)

    def generate(self, messages, tool_schemas) -> LLMResponse:
        return self._responses.pop(0)


class AlwaysOkExtractor:
    """Un agent tiers imaginaire qui dit toujours « OK » pour un succès —
    aucun des motifs français/anglais d'AgriCam ne le reconnaîtrait."""

    def __call__(self, final_text: str) -> bool:
        return final_text.strip().upper() == "OK"


def make_task() -> Task:
    return Task(
        id="T-EXT", prompt="Fais quelque chose.", complexity=TaskComplexity.LEVEL_1,
        category="generic", expected_state_delta={}, verification_query="q",
    )


def make_tools() -> AgriCamTools:
    store = AgriCamDataStore()
    store.seed_demo_data()
    return AgriCamTools(store)


# ---------------------------------------------------------------------------
# extract_success_claim (fonction historique) : comportement inchangé
# ---------------------------------------------------------------------------


def test_extract_success_claim_unchanged():
    assert extract_success_claim("Traitement appliqué avec succès.") is True
    assert extract_success_claim("Je n'ai pas pu terminer l'action.") is False
    assert extract_success_claim("OK") is False  # aucun motif AgriCam ne matche "OK" seul


def test_pattern_extractor_delegates_to_the_free_function():
    extractor: SuccessClaimExtractor = PatternSuccessClaimExtractor()
    assert extractor("Traitement appliqué avec succès.") is True
    assert extractor("Je n'ai pas pu terminer l'action.") is False


def test_default_extractor_singleton_is_a_pattern_extractor():
    assert isinstance(DEFAULT_SUCCESS_CLAIM_EXTRACTOR, PatternSuccessClaimExtractor)


# ---------------------------------------------------------------------------
# Injection dans run_agent : le comportement de détection change réellement
# ---------------------------------------------------------------------------


def test_run_agent_uses_pattern_extractor_by_default():
    llm = ScriptedLLMClient([LLMResponse(tool_call=None, final_text="OK")])
    result = run_agent(make_task(), 0, llm, make_tools(), ActionPolicy())
    # "OK" seul ne matche aucun motif français/anglais du classifieur par défaut.
    assert result.declared_success is False


def test_run_agent_with_custom_extractor_changes_detection():
    llm = ScriptedLLMClient([LLMResponse(tool_call=None, final_text="OK")])
    result = run_agent(
        make_task(), 0, llm, make_tools(), ActionPolicy(),
        success_claim_extractor=AlwaysOkExtractor(),
    )
    # Le même texte "OK", avec l'extracteur du tiers, EST un succès déclaré.
    assert result.declared_success is True


def test_run_agent_custom_extractor_also_recognizes_negative_case():
    llm = ScriptedLLMClient([LLMResponse(tool_call=None, final_text="Traitement appliqué avec succès.")])
    result = run_agent(
        make_task(), 0, llm, make_tools(), ActionPolicy(),
        success_claim_extractor=AlwaysOkExtractor(),
    )
    # Une phrase qui EST un succès pour AgriCam ne l'est PAS pour cet extracteur tiers.
    assert result.declared_success is False


def test_run_agent_custom_extractor_is_never_consulted_when_a_tool_is_called():
    """L'extracteur ne juge que la réponse FINALE ; un appel d'outil suivi
    d'une conclusion suit toujours le même chemin, extracteur mis à part."""
    llm = ScriptedLLMClient([
        LLMResponse(
            tool_call=LLMToolCallRequest(id="c1", name="check_marketplace_stock", arguments={"product_id": "PRD-7"}),
            final_text=None,
        ),
        LLMResponse(tool_call=None, final_text="OK"),
    ])
    result = run_agent(
        make_task(), 0, llm, make_tools(), ActionPolicy(),
        success_claim_extractor=AlwaysOkExtractor(),
    )
    assert len(result.tool_calls) == 1
    assert result.declared_success is True
