"""
Boucle agent (pattern ReAct) : alterne raisonnement/action jusqu'à
produire une réponse finale ou atteindre `max_steps`.

Cette implémentation est le point d'intégration de TOUS les modules du
projet :
- `LLMClient` (agent/llm_client.py) pour le raisonnement,
- `AgriCamTools` (mcp_tools/tools.py) pour l'exécution réelle,
- `ActionPolicy` + `guard_before_action` (security/) pour l'autorisation,
- `sanitize_tool_output` (security/sanitizer.py) avant réinjection,
- extraction de `declared_success`, totalement indépendante du
  Success Verifier (verifier/success_verifier.py) qui l'évalue a posteriori.

Format des messages
-------------------
L'historique est construit au format de l'API Messages d'Anthropic
(blocs `tool_use` côté assistant, blocs `tool_result` côté utilisateur,
reliés par `tool_use_id`). Ce format est celui qu'attend
`AnthropicLLMClient` ; les clients factices des tests l'ignorent. Toute
implémentation alternative de `LLMClient` doit soit accepter ce format,
soit le convertir dans `generate`.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from agricam_reliable_agents.agent.llm_client import LLMClient, LLMClientError
from agricam_reliable_agents.mcp_tools.data_store import AgriCamDataError
from agricam_reliable_agents.mcp_tools.tools import ALL_TOOL_SCHEMAS, AgriCamTools
from agricam_reliable_agents.models.data_models import AgentResult, Task, ToolCall
from agricam_reliable_agents.security.guard import (
    ConfirmationProvider,
    IncidentSink,
    guard_before_action,
)
from agricam_reliable_agents.security.policy import ActionPolicy
from agricam_reliable_agents.security.sanitizer import sanitize_tool_output

DEFAULT_MAX_STEPS = 10

# Coût indicatif par million de tokens (tarif Claude Opus 5 au moment de la
# rédaction ; à ajuster si le modèle configuré change).
_COST_PER_MILLION_INPUT_TOKENS_USD = 5.0
_COST_PER_MILLION_OUTPUT_TOKENS_USD = 25.0

_SUCCESS_CLAIM_PATTERN = re.compile(
    r"\b(fait|effectué|terminé|réalisé|traitement appliqué|notification envoyée)\b",
    re.IGNORECASE,
)
_FAILURE_CLAIM_PATTERN = re.compile(
    r"\b(impossible|échec|je n'ai pas pu|erreur)\b", re.IGNORECASE
)


def extract_success_claim(final_text: str) -> bool:
    """
    Classifieur volontairement simple et déterministe : cherche des
    formulations explicites de succès ou d'échec dans la réponse finale
    de l'agent. Documenté comme un point d'amélioration possible (un
    petit modèle de classification dédié serait plus robuste), mais
    gardé simple ici pour rester auditable et prévisible en test.

    Important : cette fonction ne doit JAMAIS être utilisée pour décider
    si la tâche a réellement réussi (c'est le rôle du Success Verifier,
    basé sur l'état réel) — uniquement pour savoir ce que l'agent
    PRÉTEND avoir fait.
    """
    if _FAILURE_CLAIM_PATTERN.search(final_text):
        return False
    return bool(_SUCCESS_CLAIM_PATTERN.search(final_text))


def _estimate_cost_usd(input_tokens: int, output_tokens: int) -> float:
    return (
        input_tokens / 1_000_000 * _COST_PER_MILLION_INPUT_TOKENS_USD
        + output_tokens / 1_000_000 * _COST_PER_MILLION_OUTPUT_TOKENS_USD
    )


def _serialize_tool_result(raw_result: Any) -> str:
    """Sérialise un résultat d'outil en JSON (frontière de confiance : jamais
    de repr Python brute dans le contexte du modèle)."""
    return json.dumps(raw_result, ensure_ascii=False, default=str)


def run_agent(
    task: Task,
    trial_id: int,
    llm_client: LLMClient,
    tools: AgriCamTools,
    policy: ActionPolicy,
    max_steps: int = DEFAULT_MAX_STEPS,
    confirmation_provider: ConfirmationProvider | None = None,
    incident_sink: IncidentSink | None = None,
) -> AgentResult:
    """
    Exécute l'agent sur une tâche, jusqu'à réponse finale ou `max_steps`.

    Toute exception métier levée par un outil (`AgriCamDataError`) est
    interceptée et renvoyée à l'agent comme un résultat d'outil en échec
    (jamais propagée telle quelle) : un agent robuste doit pouvoir
    récupérer d'une erreur d'outil, ce qui fait partie de ce que le
    harnais de fiabilité mesure.

    Une `LLMClientError` (LLM injoignable après retries) termine l'essai
    avec `error` renseigné et `declared_success=False` : l'essai compte
    comme un échec sans interrompre la campagne.
    """
    start = time.monotonic()
    messages: list[dict[str, Any]] = [{"role": "user", "content": task.prompt}]
    tool_calls: list[ToolCall] = []
    total_input_tokens = 0
    total_output_tokens = 0

    kwargs: dict[str, Any] = {}
    if confirmation_provider is not None:
        kwargs["confirmation_provider"] = confirmation_provider
    if incident_sink is not None:
        kwargs["incident_sink"] = incident_sink

    def build_result(final_answer: str, *, declared: bool, exceeded: bool, error: str | None) -> AgentResult:
        return AgentResult(
            task_id=task.id,
            trial_id=trial_id,
            final_answer=final_answer,
            tool_calls=tuple(tool_calls),
            declared_success=declared,
            latency_ms=(time.monotonic() - start) * 1000,
            cost_usd=_estimate_cost_usd(total_input_tokens, total_output_tokens),
            max_steps_exceeded=exceeded,
            error=error,
        )

    for _step in range(max_steps):
        try:
            response = llm_client.generate(messages, list(ALL_TOOL_SCHEMAS))
        except LLMClientError as exc:
            return build_result(f"LLM_ERROR: {exc}", declared=False, exceeded=False, error=str(exc))

        total_input_tokens += response.input_tokens
        total_output_tokens += response.output_tokens

        if response.tool_call is not None:
            request = response.tool_call
            call = ToolCall(tool_name=request.name, arguments=request.arguments)

            allowed = guard_before_action(call, policy, trial_id, **kwargs)

            assistant_content: list[dict[str, Any]] = []
            if response.final_text:
                assistant_content.append({"type": "text", "text": response.final_text})
            assistant_content.append(
                {"type": "tool_use", "id": request.id, "name": request.name, "input": request.arguments}
            )
            messages.append({"role": "assistant", "content": assistant_content})

            is_error = False
            if not allowed:
                tool_result_text = f"Action refusée par la politique de sécurité : {call.tool_name}."
                is_error = True
            else:
                try:
                    raw_result = tools.dispatch(call.tool_name, call.arguments)
                    tool_calls.append(call)
                    tool_result_text = _serialize_tool_result(raw_result)
                except AgriCamDataError as exc:
                    tool_result_text = f"Erreur outil : {exc}"
                    is_error = True

            sanitized = sanitize_tool_output(tool_result_text)
            tool_result_block: dict[str, Any] = {
                "type": "tool_result",
                "tool_use_id": request.id,
                "content": sanitized.sanitized_text,
            }
            if is_error:
                tool_result_block["is_error"] = True
            messages.append({"role": "user", "content": [tool_result_block]})
            continue

        # Réponse finale
        final_text = response.final_text or ""
        return build_result(
            final_text, declared=extract_success_claim(final_text), exceeded=False, error=None
        )

    return build_result("MAX_STEPS_EXCEEDED", declared=False, exceeded=True, error=None)
