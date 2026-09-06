"""
Interface LLM découplée du fournisseur, pour que la boucle agent
(agent/loop.py) soit testable sans dépendre d'une clé API réelle, et
pour permettre de changer de modèle (Claude, autre) sans toucher à la
logique de la boucle.

`AnthropicLLMClient` est l'implémentation réelle basée sur le SDK
officiel `anthropic` (API Messages avec tool use). Elle est entièrement
testée par mocks (tests/test_llm_client.py) : aucun test n'exige de
variable d'environnement ANTHROPIC_API_KEY.

Hiérarchie d'erreurs exposée à la boucle agent :

- `LLMClientError` : base de toutes les erreurs émises par ce module.
- `LLMTransientError(LLMClientError)` : échec définitif après épuisement
  des tentatives sur une erreur transitoire (rate limit, timeout, réseau,
  5xx). La cause SDK d'origine est chaînée dans `__cause__`.

Les erreurs non transitoires du SDK (400 requête invalide, 401
authentification, 404, ...) sont propagées telles quelles, sans retry :
elles sont déterministes et rejouer la requête ne ferait que consommer
des tentatives inutiles.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

# Modèle par défaut : le plus capable de la gamme Opus au moment de la
# rédaction. Surchargeable via le paramètre `model` du constructeur.
DEFAULT_MODEL = "claude-opus-5"
DEFAULT_MAX_TOKENS = 4096
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BASE_DELAY_SECONDS = 1.0

_REFUSAL_MARKER = "[RÉPONSE REFUSÉE PAR LE MODÈLE — aucune action effectuée]"


class LLMClientError(Exception):
    """Erreur de base émise par un client LLM du projet."""


class LLMTransientError(LLMClientError):
    """Échec définitif après épuisement des tentatives sur une erreur transitoire."""


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

    Si `tool_call` est renseigné, le modèle demande un outil ; `final_text`
    contient alors l'éventuel texte d'accompagnement (ex. « Je vérifie le
    stock. »). Si `tool_call` est None, `final_text` est la conclusion.
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

    Retry et backoff exponentiel
    ----------------------------
    Chaque appel à `generate` est tenté au plus `max_attempts` fois (3 par
    défaut). Entre deux tentatives, le client attend
    `base_delay * 2 ** i` secondes (1 s, 2 s avec les valeurs par défaut ;
    4 s si `max_attempts=4`, etc.). Seules les erreurs **transitoires** du
    SDK déclenchent un retry : `RateLimitError` (429), `APITimeoutError`,
    `APIConnectionError` (réseau) et `InternalServerError` (5xx).

    Comportement en cas d'échec définitif
    -------------------------------------
    Après la dernière tentative infructueuse, `generate` lève
    `LLMTransientError` avec le message
    « Échec définitif après N tentatives : <erreur SDK> ». L'exception SDK
    d'origine est disponible via `__cause__`. Toute autre erreur du SDK
    (`BadRequestError`, `AuthenticationError`, ...) est propagée
    immédiatement, sans retry ni encapsulation.

    Décisions de conception
    -----------------------
    - Le client SDK est construit avec `max_retries=0` : le SDK possède sa
      propre boucle de retry, et la laisser active multiplierait les
      tentatives (3 × 3 = 9 appels réseau) tout en rendant le backoff
      observé imprévisible. Une seule boucle de retry fait autorité.
    - `client` et `sleep` sont injectables pour rendre le comportement
      testable sans réseau ni attente réelle.
    - Un bloc `tool_use` a priorité sur le texte : la boucle agent exécute
      l'outil, et le texte d'accompagnement est conservé pour l'audit.
    - Les blocs `thinking` sont ignorés (leur contenu n'est pas destiné à
      la boucle) ; un `stop_reason == "refusal"` sans texte est traduit en
      marqueur explicite pour que le Success Verifier voie un échec net.

    Limites connues
    ---------------
    - Le coût estimé dans agent/loop.py utilise des tarifs indicatifs ;
      il ne lit pas la tarification réelle du modèle configuré.
    - Le streaming n'est pas utilisé : `max_tokens` doit rester modéré
      (≤ 16 000) pour éviter les timeouts HTTP du SDK.
    """

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        *,
        system_prompt: str | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        base_delay: float = DEFAULT_BASE_DELAY_SECONDS,
        client: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts doit être >= 1.")
        if base_delay < 0:
            raise ValueError("base_delay doit être >= 0.")

        # Import différé : ne casse pas le reste du projet si le paquet
        # `anthropic` n'est pas installé dans un environnement qui n'a
        # besoin que du harnais de fiabilité ou de la sécurité.
        import anthropic

        self._transient_errors: tuple[type[Exception], ...] = (
            anthropic.RateLimitError,
            anthropic.APITimeoutError,
            anthropic.APIConnectionError,
            anthropic.InternalServerError,
        )
        self._client = client if client is not None else anthropic.Anthropic(max_retries=0)
        self._model = model
        self._max_tokens = max_tokens
        self._system_prompt = system_prompt
        self._max_attempts = max_attempts
        self._base_delay = base_delay
        self._sleep = sleep

    @property
    def model(self) -> str:
        return self._model

    @property
    def max_tokens(self) -> int:
        return self._max_tokens

    # ------------------------------------------------------------------
    def generate(
        self, messages: list[dict[str, Any]], tool_schemas: list[dict[str, Any]]
    ) -> LLMResponse:
        """Appelle l'API Messages avec retry, puis normalise la réponse."""
        request: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": list(messages),
            "tools": [
                {
                    "name": schema["name"],
                    "description": schema["description"],
                    "input_schema": schema["input_schema"],
                }
                for schema in tool_schemas
            ],
        }
        if self._system_prompt is not None:
            request["system"] = self._system_prompt

        response = self._create_with_retry(request)
        return self._normalize(response)

    def _create_with_retry(self, request: dict[str, Any]) -> Any:
        last_error: Exception | None = None
        for attempt in range(self._max_attempts):
            try:
                return self._client.messages.create(**request)
            except self._transient_errors as exc:
                last_error = exc
                if attempt < self._max_attempts - 1:
                    self._sleep(self._base_delay * (2**attempt))
        raise LLMTransientError(
            f"Échec définitif après {self._max_attempts} tentatives : {last_error}"
        ) from last_error

    @staticmethod
    def _normalize(response: Any) -> LLMResponse:
        text_parts: list[str] = []
        tool_call: LLMToolCallRequest | None = None
        for block in response.content:
            if block.type == "tool_use" and tool_call is None:
                tool_call = LLMToolCallRequest(id=block.id, name=block.name, arguments=dict(block.input))
            elif block.type == "text":
                text_parts.append(block.text)

        final_text: str | None = "\n".join(text_parts) if text_parts else None
        if tool_call is None and final_text is None:
            final_text = _REFUSAL_MARKER if getattr(response, "stop_reason", None) == "refusal" else ""

        return LLMResponse(
            tool_call=tool_call,
            final_text=final_text,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )
