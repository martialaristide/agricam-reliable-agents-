"""
Interface `AgentConnector` — le contrat minimal que tout adaptateur
(appel direct modèle, REST, MCP, CLI) doit respecter.

Décision de conception : `send` prend un `prompt` texte brut, pas un objet
`Task` du cœur du projet. Un agent tiers ne connaît rien d'AgriCam ni de
ses dataclasses ; il ne voit qu'une instruction en langage naturel et y
répond. C'est `bridge.agent_connector_as_runner` qui fait le pont entre
ce monde générique et le contrat `AgentRunner` (`Task` en entrée,
`AgentResult` en sortie) attendu par `reliability/harness.py`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class RawAgentReply:
    """
    Réponse brute d'un agent externe à un envoi de prompt, avant toute
    interprétation par le harnais.

    `declared_success` est déjà calculée par le connecteur (via un
    `SuccessClaimExtractor`, cf. `agent/loop.py`) : le connecteur est le
    seul endroit qui sait comment CET agent formule un succès.

    `raw` conserve la réponse brute du transport (objet SDK, JSON HTTP,
    stdout du sous-processus...) pour l'audit — jamais interprété par le
    harnais, qui ne lit que les champs typés de cette dataclass.

    `error` est renseigné pour une panne d'infrastructure (timeout,
    connexion refusée, sous-processus tué...) plutôt qu'une réponse de
    l'agent — comme `AgentResult.error`, l'essai compte alors comme un
    échec sans interrompre la campagne.
    """

    text: str
    declared_success: bool
    latency_ms: float
    cost_usd: float = 0.0
    raw: Any = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ConnectorMetadata:
    """
    Décrit un connecteur pour l'écran « Connecter un agent » (section 5
    du document de référence) et pour l'audit — jamais consommé par le
    harnais lui-même.

    `connector_type` doit être l'une des valeurs de
    `oracle_repository.ConnectorType` (`'direct_model' | 'rest' | 'mcp' |
    'cli'`) — non importé ici pour éviter un cycle (`oracle_repository`
    dépend de modèles de données, pas l'inverse).
    """

    connector_type: str
    name: str
    version: str | None = None
    capabilities: tuple[str, ...] = field(default_factory=tuple)


class AgentConnector(Protocol):
    """
    Adapte un agent externe (déployé ou non, AgriCam ou tiers) au contrat
    `AgentRunner` du harnais — via `bridge.agent_connector_as_runner`,
    jamais par modification du harnais lui-même.
    """

    def send(self, prompt: str, trial_id: int) -> RawAgentReply:
        """Envoie `prompt` à l'agent pour l'essai `trial_id` et attend sa
        réponse. Doit intercepter les pannes de transport et les renvoyer
        via `RawAgentReply.error`, jamais les laisser se propager comme
        exception non gérée (l'essai doit compter comme un échec mesuré,
        pas interrompre la campagne — même principe que `run_agent`)."""
        ...

    def describe(self) -> ConnectorMetadata:
        """Décrit ce connecteur (type, nom, version, capacités) pour
        l'audit et l'interface — jamais utilisé pour une décision du
        harnais."""
        ...
