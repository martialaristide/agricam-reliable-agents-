"""
Interface LLM découplée du fournisseur, pour que la boucle agent
(agent/loop.py) soit testable sans dépendre d'une clé API réelle, et
pour permettre de changer de modèle (Claude, GPT, autre) sans toucher
à la logique de la boucle.

`AnthropicLLMClient` est une implémentation réelle basée sur le SDK
officiel `anthropic` et le format actuel de l'API Messages avec tool use.
Son exécution effective nécessite une variable d'environnement
ANTHROPIC_API_KEY valide — elle n'est donc pas couverte par les tests
unitaires du projet (qui utilisent `FakeLLMClient`, cf. tests/), mais sa
syntaxe et sa structure sont conformes à la documentation officielle du
SDK au moment de la rédaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class LLMToolCallRequest:
    """Demande d'appel d'outil émise par le modèle."""
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """
    Réponse normalisée du modèle, indépendante du fournisseur.

    Exactement un des deux champs (`tool_call` ou `final_text`) est
    renseigné : le modèle demande soit un outil, soit conclut.
    """
    tool_call: LLMToolCallRequest | None
    final_text: str | None
    input_tokens: int = 0
    output_tokens: int = 0


class LLMClient(Protocol):
    """Contrat minimal requis par la boucle agent."""

    def generate(
        self, messages: list[dict[str, Any]], tool_schemas: list[dict[str, Any]]
    ) -> LLMResponse: ...


class AnthropicLLMClient:
    """
    Implémentation réelle utilisant le SDK officiel `anthropic`.

    Nécessite : `pip install anthropic` et la variable d'environnement
    ANTHROPIC_API_KEY définie dans l'environnement d'exécution.
    """

    def __init__(self, model: str = "claude-sonnet-4-6", max_tokens: int = 1024) -> None:
        # Import différé : ne casse pas le reste du projet si le paquet
        # `anthropic` n'est pas installé dans un environnement qui n'a
        # besoin que du harnais de fiabilité ou de la sécurité, par exemple.
        import anthropic

        self._client = anthropic.Anthropic()
        self._model = model
        self._max_tokens = max_tokens

    def generate(
        self, messages: list[dict[str, Any]], tool_schemas: list[dict[str, Any]]
    ) -> LLMResponse:
        anthropic_tools = [
            {
                "name": schema["name"],
                "description": schema["description"],
                "input_schema": schema["input_schema"],
            }
            for schema in tool_schemas
        ]

        response = self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            messages=messages,
            tools=anthropic_tools,
        )

        for block in response.content:
            if block.type == "tool_use":
                return LLMResponse(
                    tool_call=LLMToolCallRequest(
                        id=block.id, name=block.name, arguments=block.input
                    ),
                    final_text=None,
                    input_tokens=response.usage.input_tokens,
                    output_tokens=response.usage.output_tokens,
                )

        text_blocks = [b.text for b in response.content if b.type == "text"]
        return LLMResponse(
            tool_call=None,
            final_text="\n".join(text_blocks),
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )
