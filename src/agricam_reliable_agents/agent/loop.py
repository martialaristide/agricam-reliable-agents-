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
"""

from __future__ import annotations

import re
import time
from typing import Any

from agricam_reliable_agents.agent.llm_client import LLMClient
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

# Coût indicatif par million de tokens (à ajuster selon le modèle réel utilisé).
_COST_PER_MILLION_INPUT_TOKENS_USD = 3.0
_COST_PER_MILLION_OUTPUT_TOKENS_USD = 15.0

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

    for _step in range(max_steps):
        response = llm_client.generate(messages, list(ALL_TOOL_SCHEMAS))
        total_input_tokens += response.input_tokens
        total_output_tokens += response.output_tokens

        if response.tool_call is not None:
            call = ToolCall(tool_name=response.tool_call.name, arguments=response.tool_call.arguments)

            allowed = guard_before_action(call, policy, trial_id, **kwargs)
            messages.append({"role": "assistant", "content": f"[tool_call:{call.tool_name}]"})

            if not allowed:
                tool_result_text = (
                    f"Action refusée par la politique de sécurité : {call.tool_name}."
                )
            else:
                try:
                    raw_result = tools.dispatch(call.tool_name, call.arguments)
                    tool_calls.append(call)
                    tool_result_text = str(raw_result)
                except AgriCamDataError as exc:
                    tool_result_text = f"Erreur outil : {exc}"

            sanitized = sanitize_tool_output(tool_result_text)
            messages.append({"role": "user", "content": sanitized.sanitized_text})
            continue

        # Réponse finale
        final_text = response.final_text or ""
        return AgentResult(
            task_id=task.id,
            trial_id=trial_id,
            final_answer=final_text,
            tool_calls=tuple(tool_calls),
            declared_success=extract_success_claim(final_text),
            latency_ms=(time.monotonic() - start) * 1000,
            cost_usd=_estimate_cost_usd(total_input_tokens, total_output_tokens),
            max_steps_exceeded=False,
        )

    return AgentResult(
        task_id=task.id,
        trial_id=trial_id,
        final_answer="MAX_STEPS_EXCEEDED",
        tool_calls=tuple(tool_calls),
        declared_success=False,
        latency_ms=(time.monotonic() - start) * 1000,
        cost_usd=_estimate_cost_usd(total_input_tokens, total_output_tokens),
        max_steps_exceeded=True,
    )
