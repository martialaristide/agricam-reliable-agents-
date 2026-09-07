"""
Modèles de données centraux du système AgriCam Reliable Agents.

Ces structures forment le contrat commun entre le serveur MCP, la boucle
agent, le harnais de fiabilité, le vérificateur de succès et la suite de
sécurité. Elles sont volontairement immuables (frozen=True) quand elles
représentent un fait déjà survenu (résultat, vérification), pour éviter
toute mutation accidentelle une fois persisté.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal


def utcnow() -> datetime:
    """Horodatage UTC unique pour tout le système (évite les dérives de fuseau)."""
    return datetime.now(timezone.utc)


class TaskComplexity(str, Enum):
    """Niveau de complexité d'une tâche de test, exprimé en nombre d'étapes attendues."""

    LEVEL_1 = "1-2_steps"
    LEVEL_2 = "3-5_steps"
    LEVEL_3 = "6+_steps"


@dataclass(frozen=True, slots=True)
class Task:
    """
    Une tâche de test reproductible.

    `expected_state_delta` décrit, sous forme de dictionnaire plat,
    les champs métier attendus après une exécution réussie
    (ex. {"diagnostic.status": "traité", "notification.sent": True}).
    `verification_query` est un identifiant de requête de vérification
    (résolu par le Success Verifier, cf. verifier/success_verifier.py) —
    volontairement découplé du texte de la tâche pour ne jamais fuiter
    la méthode de vérification à l'agent.
    """

    id: str
    prompt: str
    complexity: TaskComplexity
    category: str
    expected_state_delta: dict[str, Any]
    verification_query: str

    def __post_init__(self) -> None:
        if not self.id or not self.prompt:
            raise ValueError("Task.id et Task.prompt sont obligatoires et non vides.")
        if not isinstance(self.expected_state_delta, dict):
            raise TypeError("expected_state_delta doit être un dict.")


ToolCallStatus = Literal["ok", "refused", "error"]


@dataclass(frozen=True, slots=True)
class ToolCall:
    """
    Un appel d'outil MCP demandé par l'agent, horodaté pour l'audit.

    `status` dit ce qu'il est advenu de l'appel : `ok` (exécuté), `refused`
    (bloqué par la politique de sécurité) ou `error` (rejeté par l'outil :
    entité inconnue, arguments invalides) ; `error` porte alors le message
    renvoyé au modèle. Un appel refusé ou en erreur n'a modifié aucun état.
    """

    tool_name: str
    arguments: dict[str, Any]
    timestamp: datetime = field(default_factory=utcnow)
    status: ToolCallStatus = "ok"
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.status == "ok"


@dataclass(frozen=True, slots=True)
class AgentResult:
    """
    Résultat brut d'une exécution d'agent sur une tâche donnée.

    `error` est renseigné quand l'essai s'est terminé sur une défaillance
    d'infrastructure (ex. LLM injoignable après retries) plutôt que sur
    une réponse du modèle. L'essai compte alors comme un échec dans le
    harnais, sans interrompre la campagne : une campagne de fiabilité doit
    mesurer les pannes, pas s'arrêter à la première.
    """

    task_id: str
    trial_id: int
    final_answer: str
    tool_calls: tuple[ToolCall, ...]
    declared_success: bool
    latency_ms: float
    cost_usd: float
    max_steps_exceeded: bool = False
    error: str | None = None


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """
    Résultat de la vérification indépendante d'un AgentResult.

    `overconfidence_detected` est vrai si et seulement si l'agent a
    déclaré un succès (`declared_success=True`) alors que l'état réel
    du système ne correspond pas à `expected_state_delta`. C'est le
    signal central du projet : il ne dépend jamais de ce que l'agent dit,
    seulement de l'état réel avant/après.
    """

    task_id: str
    trial_id: int
    actual_state_delta: dict[str, Any]
    matches_expected: bool
    overconfidence_detected: bool


@dataclass(frozen=True, slots=True)
class ReliabilityReport:
    """Rapport de fiabilité agrégé pour une tâche, sur N essais."""

    task_id: str
    n_trials: int
    n_success: int
    p_hat: float
    wilson_ci: tuple[float, float]
    pass_k: dict[int, float]

    def __post_init__(self) -> None:
        if self.n_trials <= 0:
            raise ValueError("n_trials doit être strictement positif.")
        if not (0 <= self.n_success <= self.n_trials):
            raise ValueError("n_success incohérent avec n_trials.")


class AttackCategory(str, Enum):
    """Taxonomie de sécurité alignée sur OWASP Top 10 for LLM Applications."""

    PROMPT_INJECTION_DIRECT = "LLM01_direct"
    PROMPT_INJECTION_INDIRECT = "LLM01_indirect"
    SENSITIVE_INFO_DISCLOSURE = "LLM06"
    EXCESSIVE_AGENCY = "LLM08"
    OVERRELIANCE = "LLM09"


@dataclass(frozen=True, slots=True)
class SecurityIncident:
    """Un incident détecté (bloqué ou non) par la suite de sécurité."""

    trial_id: int
    attack_category: AttackCategory
    payload: str
    blocked: bool
    detected_at: datetime = field(default_factory=utcnow)
    task_id: str | None = None
