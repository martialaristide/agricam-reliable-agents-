"""
Point d'entrée unique par lequel TOUT appel d'outil doit passer avant
exécution réelle. Centraliser ce contrôle (plutôt que de le disperser
dans chaque outil) garantit qu'aucun nouvel outil ajouté au serveur MCP
ne puisse accidentellement contourner la politique de sécurité.
"""

from __future__ import annotations

import json
from typing import Protocol

from agricam_reliable_agents.models.data_models import (
    AttackCategory,
    SecurityIncident,
    ToolCall,
)
from agricam_reliable_agents.security.policy import SENSITIVE_TOOLS, ActionPolicy


class ConfirmationProvider(Protocol):
    """
    Fournit une confirmation humaine (ou simulée, en environnement de
    test) avant l'exécution d'une action irréversible.
    """

    def __call__(self, call: ToolCall) -> bool: ...


class IncidentSink(Protocol):
    """Callback de journalisation des incidents de sécurité détectés."""

    def __call__(self, incident: SecurityIncident) -> None: ...


def _deny_all_confirmation(call: ToolCall) -> bool:
    """Politique de confirmation par défaut : refuse tant qu'aucun
    fournisseur explicite n'est branché (fail-safe, pas fail-open)."""
    return False


def guard_before_action(
    call: ToolCall,
    policy: ActionPolicy,
    trial_id: int,
    confirmation_provider: ConfirmationProvider = _deny_all_confirmation,
    incident_sink: IncidentSink | None = None,
    task_id: str | None = None,
) -> bool:
    """
    Décide si un appel d'outil peut être exécuté.

    Ordre des vérifications (du plus rapide/large au plus coûteux) :
    1. L'outil est-il sensible ? Si non, autorisation immédiate.
    2. L'appel est-il dans le périmètre autorisé (farmer_id/parcel_id) ?
       Si non : refus + incident LLM08 (Excessive Agency) journalisé.
    3. L'outil exige-t-il une confirmation explicite ? Si oui, elle doit
       être positive pour autoriser l'exécution.

    Args:
        call: l'appel d'outil demandé par l'agent.
        policy: la politique d'autorisation de la session courante.
        trial_id: identifiant de l'essai en cours (pour la traçabilité).
        confirmation_provider: fonction de confirmation (humaine en
            production, simulée en test). Par défaut, refuse tout —
            principe de sécurité "fail-safe".
        incident_sink: callback optionnel de journalisation des incidents.
        task_id: tâche en cours, pour rattacher l'incident à un essai précis
            (`trial_id` seul est partagé entre les tâches d'une campagne).

    Returns:
        True si l'action peut être exécutée, False sinon.
    """
    if call.tool_name not in SENSITIVE_TOOLS:
        return True

    if not policy.is_within_scope(call):
        if incident_sink is not None:
            incident_sink(
                SecurityIncident(
                    trial_id=trial_id,
                    attack_category=AttackCategory.EXCESSIVE_AGENCY,
                    payload=json.dumps(
                        {"tool_name": call.tool_name, "arguments": call.arguments},
                        ensure_ascii=False, default=str, sort_keys=True,
                    ),
                    blocked=True,
                    task_id=task_id,
                )
            )
        return False

    if policy.requires_confirmation(call):
        return confirmation_provider(call)

    return True
