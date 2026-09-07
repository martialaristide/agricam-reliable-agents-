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
L'historique est construit au format de l'API Messages d'Anthropic. Quand
le client fournit le contenu brut de la réponse (`LLMResponse.raw_content`),
il est renvoyé **tel quel** comme tour assistant : c'est obligatoire pour
que les blocs `thinking` (raisonnement adaptatif, actif par défaut sur
Opus 5) et tous les blocs `tool_use` soient rejoués sans modification.
Sinon (clients factices des tests), le tour est reconstruit à partir des
champs normalisés. Tous les `tool_result` d'un même tour sont renvoyés
dans un seul message utilisateur, comme l'exige l'appel d'outils
parallèle.

Audit des appels d'outils
-------------------------
Chaque appel demandé par le modèle est archivé dans `AgentResult.tool_calls`
avec son statut : `ok` (exécuté), `refused` (bloqué par la politique de
sécurité) ou `error` (rejeté par l'outil : entité inconnue, arguments
invalides). Le Success Verifier ne se sert pas de cette liste : elle
existe pour comprendre un essai, pas pour le juger.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from agricam_reliable_agents.agent.llm_client import (
    LLMClient,
    LLMClientError,
    LLMResponse,
    LLMToolCallRequest,
)
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

# Tarif par million de jetons (entrée, sortie), en USD. Valeur par défaut :
# Claude Opus 5. Surchargeable par `run_agent(pricing=...)` quand un autre
# modèle est configuré ; `PRICING_BY_MODEL` couvre les modèles courants.
OPUS_5_PRICING: tuple[float, float] = (5.0, 25.0)
PRICING_BY_MODEL: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}


def pricing_for_model(model: str | None) -> tuple[float, float]:
    """Tarif (entrée, sortie) par million de jetons ; Opus 5 si le modèle est inconnu."""
    if model is None:
        return OPUS_5_PRICING
    for prefix, pricing in PRICING_BY_MODEL.items():
        if model.startswith(prefix):
            return pricing
    return OPUS_5_PRICING


# Déclarations de succès / d'échec les plus fréquentes en français (et les
# formes anglaises courantes). Le classifieur reste volontairement une
# liste de motifs lisibles : auditable, déterministe, sans modèle.
_SUCCESS_CLAIM_PATTERN = re.compile(
    r"\b(fait|effectu[ée]e?s?|termin[ée]e?s?|r[ée]alis[ée]e?s?|trait[ée]e?s?|notifi[ée]e?s?"
    r"|appliqu[ée]e?s?|envoy[ée]e?s?|mise?s? à jour|enregistr[ée]e?s?|succès|r[ée]ussi[e]?s?"
    r"|accompli[e]?s?|confirm[ée]e?s?|done|completed|successfully|has been (treated|notified|sent|applied))\b",
    re.IGNORECASE,
)
_FAILURE_CLAIM_PATTERN = re.compile(
    r"\b(impossible|[ée]chec|[ée]chou[ée]e?s?|je n'ai pas pu|n'a pas pu|n'ont pas pu"
    r"|n'(ai|a|avons|ont) pas (été |pu |réussi|fait|effectué|appliqué|traité|notifié|envoyé)"
    r"|pas été (traité|notifié|appliqué|effectué|envoyé)|erreur|unable|failed|could not|couldn't)\b",
    re.IGNORECASE,
)
# « aucune erreur », « sans erreur » : une négation d'erreur n'est pas un échec.
_NEGATED_ERROR_PATTERN = re.compile(r"\b(aucune|sans|pas d'|zéro|no)\s*erreurs?\b", re.IGNORECASE)


def extract_success_claim(final_text: str) -> bool:
    """
    Classifieur volontairement simple et déterministe : cherche des
    formulations explicites de succès ou d'échec dans la réponse finale
    de l'agent. Une formulation d'échec l'emporte sur une formulation de
    succès (« traitement appliqué, mais la notification a échoué » est un
    échec déclaré), sauf quand l'« erreur » mentionnée est niée
    (« aucune erreur »).

    Important : cette fonction ne doit JAMAIS être utilisée pour décider
    si la tâche a réellement réussi (c'est le rôle du Success Verifier,
    basé sur l'état réel) — uniquement pour savoir ce que l'agent
    PRÉTEND avoir fait.
    """
    text = _NEGATED_ERROR_PATTERN.sub(" ", final_text)
    if _FAILURE_CLAIM_PATTERN.search(text):
        return False
    return bool(_SUCCESS_CLAIM_PATTERN.search(text))


def _estimate_cost_usd(input_tokens: int, output_tokens: int, pricing: tuple[float, float]) -> float:
    return input_tokens / 1_000_000 * pricing[0] + output_tokens / 1_000_000 * pricing[1]


def _serialize_tool_result(raw_result: Any) -> str:
    """Sérialise un résultat d'outil en JSON (frontière de confiance : jamais
    de repr Python brute dans le contexte du modèle)."""
    return json.dumps(raw_result, ensure_ascii=False, default=str)


def _assistant_content(response: LLMResponse) -> Any:
    """Le tour assistant à rejouer : contenu brut si le client l'a fourni
    (blocs `thinking` et `tool_use` intacts), reconstruction sinon."""
    if response.raw_content is not None:
        return response.raw_content
    content: list[dict[str, Any]] = []
    if response.final_text:
        content.append({"type": "text", "text": response.final_text})
    for request in response.tool_calls:
        content.append({"type": "tool_use", "id": request.id, "name": request.name, "input": request.arguments})
    return content


def _scoped_call(call: ToolCall, tools: AgriCamTools) -> ToolCall:
    """
    Appel enrichi pour la vérification de périmètre : un outil qui ne
    porte qu'un `diagnostic_id` (ex. `recommend_treatment`) est rattaché
    à la parcelle du diagnostic, pour que `allowed_parcel_ids` s'applique
    aussi à lui. L'appel exécuté reste l'original.
    """
    arguments = call.arguments
    if "parcel_id" in arguments or "diagnostic_id" not in arguments:
        return call
    try:
        parcel_id = tools.store.get_diagnostic(str(arguments["diagnostic_id"])).parcel_id
    except (AgriCamDataError, ValueError, TypeError):
        return call  # diagnostic inconnu : l'outil échouera de lui-même
    return ToolCall(tool_name=call.tool_name, arguments={**arguments, "parcel_id": parcel_id},
                    timestamp=call.timestamp)


def run_agent(
    task: Task,
    trial_id: int,
    llm_client: LLMClient,
    tools: AgriCamTools,
    policy: ActionPolicy,
    max_steps: int = DEFAULT_MAX_STEPS,
    confirmation_provider: ConfirmationProvider | None = None,
    incident_sink: IncidentSink | None = None,
    pricing: tuple[float, float] | None = None,
) -> AgentResult:
    """
    Exécute l'agent sur une tâche, jusqu'à réponse finale ou `max_steps`.

    Toute erreur d'un outil — métier (`AgriCamDataError`) ou d'appel
    (`ValueError`, `TypeError` : outil inconnu, arguments hors schéma) —
    est renvoyée à l'agent comme un résultat d'outil en échec, jamais
    propagée : un agent robuste doit pouvoir récupérer d'une erreur
    d'outil, ce qui fait partie de ce que le harnais mesure.

    Une `LLMClientError` (LLM injoignable après retries) ou une réponse
    tronquée (`stop_reason == "max_tokens"`) termine l'essai avec `error`
    renseigné et `declared_success=False` : l'essai compte comme un échec
    sans interrompre la campagne.

    `pricing` (USD par million de jetons en entrée et en sortie) sert à
    l'estimation de coût ; par défaut le tarif de Claude Opus 5, ou celui
    du modèle exposé par `llm_client.model` s'il est connu.
    """
    start = time.monotonic()
    messages: list[dict[str, Any]] = [{"role": "user", "content": task.prompt}]
    tool_calls: list[ToolCall] = []
    total_input_tokens = 0
    total_output_tokens = 0
    rates = pricing or pricing_for_model(getattr(llm_client, "model", None))

    kwargs: dict[str, Any] = {"task_id": task.id}
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
            cost_usd=_estimate_cost_usd(total_input_tokens, total_output_tokens, rates),
            max_steps_exceeded=exceeded,
            error=error,
        )

    def execute(request: LLMToolCallRequest) -> dict[str, Any]:
        """Garde puis exécution d'un appel ; retourne le bloc `tool_result`."""
        call = ToolCall(tool_name=request.name, arguments=dict(request.arguments))
        allowed = guard_before_action(_scoped_call(call, tools), policy, trial_id, **kwargs)

        is_error = False
        if not allowed:
            tool_result_text = f"Action refusée par la politique de sécurité : {call.tool_name}."
            tool_calls.append(ToolCall(call.tool_name, call.arguments, call.timestamp,
                                       status="refused", error=tool_result_text))
            is_error = True
        else:
            try:
                raw_result = tools.dispatch(call.tool_name, call.arguments)
            except AgriCamDataError as exc:
                tool_result_text = f"Erreur outil : {exc}"
                is_error = True
            except (ValueError, TypeError) as exc:
                tool_result_text = f"Appel d'outil invalide : {exc}"
                is_error = True
            else:
                tool_result_text = _serialize_tool_result(raw_result)
            tool_calls.append(ToolCall(call.tool_name, call.arguments, call.timestamp,
                                       status="error" if is_error else "ok",
                                       error=tool_result_text if is_error else None))

        sanitized = sanitize_tool_output(tool_result_text)
        block: dict[str, Any] = {
            "type": "tool_result", "tool_use_id": request.id, "content": sanitized.sanitized_text,
        }
        if is_error:
            block["is_error"] = True
        return block

    for _step in range(max_steps):
        try:
            response = llm_client.generate(messages, list(ALL_TOOL_SCHEMAS))
        except LLMClientError as exc:
            return build_result(f"LLM_ERROR: {exc}", declared=False, exceeded=False, error=str(exc))

        total_input_tokens += response.input_tokens
        total_output_tokens += response.output_tokens

        if response.stop_reason == "max_tokens":
            error = "Réponse du modèle tronquée (max_tokens atteint) : essai non concluant."
            return build_result(response.final_text or "", declared=False, exceeded=False, error=error)

        if response.tool_calls:
            messages.append({"role": "assistant", "content": _assistant_content(response)})
            results = [execute(request) for request in response.tool_calls]
            messages.append({"role": "user", "content": results})
            continue

        # Réponse finale
        final_text = response.final_text or ""
        return build_result(
            final_text, declared=extract_success_claim(final_text), exceeded=False, error=None
        )

    return build_result("MAX_STEPS_EXCEEDED", declared=False, exceeded=True, error=None)
