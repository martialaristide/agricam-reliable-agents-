"""
Pont entre `AgentConnector` (générique, prompt texte) et `AgentRunner`
(contrat déjà consommé par `reliability/harness.py` : `Task` en entrée,
`AgentResult` en sortie).

Ce module est TOUT le prix à payer pour interopérer avec le harnais
existant — `reliability/harness.py` n'est pas modifié, et n'a même pas
conscience que l'agent qu'il évalue est branché via un connecteur.
"""

from __future__ import annotations

from agricam_reliable_agents.connector.base import AgentConnector
from agricam_reliable_agents.models.data_models import AgentResult, Task
from agricam_reliable_agents.reliability.harness import AgentRunner


def agent_connector_as_runner(connector: AgentConnector) -> AgentRunner:
    """
    Construit un `AgentRunner` (compatible `evaluate_task`/
    `evaluate_reliability`) qui délègue chaque essai à `connector.send`.

    `AgentResult.tool_calls` est toujours vide : un connecteur générique
    n'a par construction aucune visibilité sur les outils qu'un agent
    tiers a pu appeler en interne (boîte noire, cf. section 2 du document
    de référence) — seul l'agent AgriCam interne (`agent/loop.py`), dont
    on contrôle la boucle, peut peupler ce champ. `max_steps_exceeded` est
    toujours `False` pour la même raison : la notion de « pas » n'existe
    pas côté connecteur, seulement côté boucle interne.

    Args:
        connector: l'adaptateur à envelopper (appel direct modèle, REST,
            MCP, CLI...).

    Returns:
        Une fonction `(task, trial_id) -> AgentResult` utilisable partout
        où le harnais attend un `AgentRunner`.
    """

    def run(task: Task, trial_id: int) -> AgentResult:
        reply = connector.send(task.prompt, trial_id)
        return AgentResult(
            task_id=task.id,
            trial_id=trial_id,
            final_answer=reply.text,
            tool_calls=(),
            declared_success=reply.declared_success,
            latency_ms=reply.latency_ms,
            cost_usd=reply.cost_usd,
            max_steps_exceeded=False,
            error=reply.error,
        )

    return run
