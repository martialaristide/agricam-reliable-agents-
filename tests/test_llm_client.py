"""
Tests de `AnthropicLLMClient` sans aucun appel réseau.

Le SDK `anthropic` est réellement importé (pour valider les noms de
classes d'exception et la structure des réponses), mais l'objet client
sous-jacent est un `unittest.mock.MagicMock` injecté : aucune variable
d'environnement ANTHROPIC_API_KEY n'est nécessaire.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import anthropic
import httpx2 as httpx
import pytest

from agricam_reliable_agents.agent.llm_client import (
    DEFAULT_MODEL,
    AnthropicLLMClient,
    LLMClientError,
    LLMTransientError,
)
from agricam_reliable_agents.mcp_tools.tools import ALL_TOOL_SCHEMAS

# ---------------------------------------------------------------------------
# Fabriques d'objets simulés (réponses et exceptions du SDK)
# ---------------------------------------------------------------------------

_REQUEST = httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def _usage(inp: int = 100, out: int = 20) -> SimpleNamespace:
    return SimpleNamespace(input_tokens=inp, output_tokens=out)


def _text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def _tool_use_block(tool_id: str, name: str, arguments: dict) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=tool_id, name=name, input=arguments)


def _thinking_block() -> SimpleNamespace:
    return SimpleNamespace(type="thinking", thinking="", signature="sig")


def _response(*blocks: SimpleNamespace, stop_reason: str = "end_turn", usage=None):
    return SimpleNamespace(
        content=list(blocks), stop_reason=stop_reason, usage=usage or _usage(),
        stop_details=None,
    )


def _status_error(cls: type[anthropic.APIStatusError], status: int) -> anthropic.APIStatusError:
    return cls("erreur simulée", response=httpx.Response(status, request=_REQUEST), body=None)


def _rate_limit() -> anthropic.RateLimitError:
    return _status_error(anthropic.RateLimitError, 429)


def _timeout() -> anthropic.APITimeoutError:
    return anthropic.APITimeoutError(request=_REQUEST)


def make_client(side_effect, **kwargs) -> tuple[AnthropicLLMClient, MagicMock, list[float]]:
    """Construit un client avec un SDK factice et un `sleep` enregistreur."""
    sdk = MagicMock()
    sdk.messages.create.side_effect = side_effect
    sleeps: list[float] = []
    client = AnthropicLLMClient(client=sdk, sleep=sleeps.append, **kwargs)
    return client, sdk, sleeps


MESSAGES = [{"role": "user", "content": "Traite le diagnostic D-42."}]


# ---------------------------------------------------------------------------
# Parsing des réponses
# ---------------------------------------------------------------------------


def test_tool_use_response_is_normalized():
    client, _, _ = make_client([
        _response(_tool_use_block("toolu_1", "recommend_treatment", {"diagnostic_id": "D-42"}),
                  stop_reason="tool_use", usage=_usage(120, 30)),
    ])
    result = client.generate(MESSAGES, list(ALL_TOOL_SCHEMAS))

    assert result.tool_call is not None
    assert result.tool_call.id == "toolu_1"
    assert result.tool_call.name == "recommend_treatment"
    assert result.tool_call.arguments == {"diagnostic_id": "D-42"}
    assert result.final_text is None
    assert (result.input_tokens, result.output_tokens) == (120, 30)


def test_final_text_response_is_normalized():
    client, _, _ = make_client([_response(_text_block("Traitement appliqué."))])
    result = client.generate(MESSAGES, list(ALL_TOOL_SCHEMAS))

    assert result.tool_call is None
    assert result.final_text == "Traitement appliqué."


def test_multiple_content_blocks_join_text_and_skip_thinking():
    client, _, _ = make_client([
        _response(_thinking_block(), _text_block("Première partie."), _text_block("Seconde partie."))
    ])
    result = client.generate(MESSAGES, list(ALL_TOOL_SCHEMAS))

    assert result.tool_call is None
    assert result.final_text == "Première partie.\nSeconde partie."


def test_text_before_tool_use_still_yields_tool_call():
    """Un modèle peut expliquer son intention (bloc texte) puis appeler
    l'outil : le premier `tool_use` prime, le texte est conservé dans
    `final_text` pour la traçabilité."""
    client, _, _ = make_client([
        _response(_text_block("Je vérifie le stock."),
                  _tool_use_block("toolu_2", "check_marketplace_stock", {"product_id": "PRD-7"}),
                  stop_reason="tool_use"),
    ])
    result = client.generate(MESSAGES, list(ALL_TOOL_SCHEMAS))

    assert result.tool_call is not None
    assert result.tool_call.name == "check_marketplace_stock"
    assert result.final_text == "Je vérifie le stock."


def test_refusal_without_text_yields_explicit_marker():
    client, _, _ = make_client([_response(stop_reason="refusal")])
    result = client.generate(MESSAGES, list(ALL_TOOL_SCHEMAS))

    assert result.tool_call is None
    assert result.final_text is not None
    assert "refus" in result.final_text.lower()


def test_tool_schemas_are_forwarded_in_anthropic_format():
    client, sdk, _ = make_client([_response(_text_block("ok"))])
    client.generate(MESSAGES, list(ALL_TOOL_SCHEMAS))

    kwargs = sdk.messages.create.call_args.kwargs
    assert kwargs["model"] == DEFAULT_MODEL
    assert kwargs["messages"] is not MESSAGES  # copie défensive
    assert kwargs["messages"] == MESSAGES
    assert [t["name"] for t in kwargs["tools"]] == [s["name"] for s in ALL_TOOL_SCHEMAS]
    assert all(set(t) == {"name", "description", "input_schema"} for t in kwargs["tools"])


def test_system_prompt_is_passed_only_when_provided():
    client_without, sdk_without, _ = make_client([_response(_text_block("ok"))])
    client_without.generate(MESSAGES, [])
    assert "system" not in sdk_without.messages.create.call_args.kwargs

    client_with, sdk_with, _ = make_client([_response(_text_block("ok"))], system_prompt="Tu es AgriCam.")
    client_with.generate(MESSAGES, [])
    assert sdk_with.messages.create.call_args.kwargs["system"] == "Tu es AgriCam."


# ---------------------------------------------------------------------------
# Retry / backoff
# ---------------------------------------------------------------------------


def test_transient_errors_are_retried_then_succeed():
    """Échoue 2 fois (rate limit puis timeout), réussit à la 3e tentative :
    backoff 1 s puis 2 s, aucune exception propagée."""
    client, sdk, sleeps = make_client([
        _rate_limit(), _timeout(), _response(_text_block("Fait."))
    ])
    result = client.generate(MESSAGES, [])

    assert result.final_text == "Fait."
    assert sdk.messages.create.call_count == 3
    assert sleeps == [1.0, 2.0]


def test_backoff_is_exponential_with_custom_base():
    client, _, sleeps = make_client(
        [_rate_limit(), _rate_limit(), _rate_limit(), _response(_text_block("ok"))],
        max_attempts=4, base_delay=0.5,
    )
    client.generate(MESSAGES, [])
    assert sleeps == [0.5, 1.0, 2.0]


def test_definitive_failure_raises_llm_transient_error_with_cause():
    client, sdk, sleeps = make_client([_rate_limit(), _rate_limit(), _rate_limit()])

    with pytest.raises(LLMTransientError) as exc_info:
        client.generate(MESSAGES, [])

    assert sdk.messages.create.call_count == 3
    assert sleeps == [1.0, 2.0]  # pas d'attente après la dernière tentative
    assert "3 tentatives" in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, anthropic.RateLimitError)
    assert issubclass(LLMTransientError, LLMClientError)


def test_server_error_5xx_is_treated_as_transient():
    client, sdk, _ = make_client([
        _status_error(anthropic.InternalServerError, 503), _response(_text_block("ok"))
    ])
    assert client.generate(MESSAGES, []).final_text == "ok"
    assert sdk.messages.create.call_count == 2


def test_non_transient_api_error_propagates_immediately_unchanged():
    """Une erreur 400 (requête invalide) ne doit JAMAIS être rejouée :
    elle est déterministe et coûterait des tentatives inutiles."""
    client, sdk, sleeps = make_client([_status_error(anthropic.BadRequestError, 400)])

    with pytest.raises(anthropic.BadRequestError):
        client.generate(MESSAGES, [])

    assert sdk.messages.create.call_count == 1
    assert sleeps == []


def test_authentication_error_propagates_immediately():
    client, sdk, _ = make_client([_status_error(anthropic.AuthenticationError, 401)])
    with pytest.raises(anthropic.AuthenticationError):
        client.generate(MESSAGES, [])
    assert sdk.messages.create.call_count == 1


def test_invalid_retry_parameters_are_rejected():
    with pytest.raises(ValueError):
        AnthropicLLMClient(client=MagicMock(), max_attempts=0)
    with pytest.raises(ValueError):
        AnthropicLLMClient(client=MagicMock(), base_delay=-1.0)


# ---------------------------------------------------------------------------
# Construction du client réel (SDK patché, sans clé API)
# ---------------------------------------------------------------------------


def test_default_construction_builds_sdk_client_without_internal_retries(monkeypatch):
    """Sans client injecté, le constructeur instancie `anthropic.Anthropic`
    avec `max_retries=0` : c'est NOTRE boucle de retry qui fait autorité,
    sinon les tentatives se multiplieraient (3 × 3 = 9 appels)."""
    captured: dict = {}

    def fake_anthropic(**kwargs):
        captured.update(kwargs)
        return MagicMock()

    monkeypatch.setattr(anthropic, "Anthropic", fake_anthropic)
    client = AnthropicLLMClient(model="claude-sonnet-5", max_tokens=512)

    assert captured == {"max_retries": 0}
    assert client.model == "claude-sonnet-5"
    assert client.max_tokens == 512
