"""
Success Verifier — vérifie objectivement si une tâche a réussi, sans jamais
se fier à ce que l'agent déclare.

Principe central du projet : `declared_success` (ce que dit l'agent) et
`matches_expected` (ce que confirme l'état réel du système) sont calculés
par des chemins de code totalement indépendants. Le champ
`overconfidence_detected` capture précisément le cas où l'agent ment
(ou se trompe) sur son propre succès — c'est le signal le plus important
du harnais de fiabilité.
"""

from __future__ import annotations

from numbers import Number
from typing import Any

from agricam_reliable_agents.models.data_models import (
    AgentResult,
    Task,
    VerificationResult,
)

# Tolérance relative pour la comparaison de valeurs numériques
# (évite les faux négatifs dus à des arrondis flottants, ex. 12.0000001 vs 12).
_FLOAT_RELATIVE_TOLERANCE = 1e-6


def compute_diff(
    state_before: dict[str, Any], state_after: dict[str, Any]
) -> dict[str, Any]:
    """
    Calcule les champs qui ont changé entre deux instantanés d'état plats.

    Les deux instantanés doivent être des dictionnaires à clés de type
    "entité.champ" (ex. "diagnostic_42.status") et valeurs scalaires ou
    sérialisables (str, bool, int, float, None).

    Args:
        state_before: instantané pris juste avant l'exécution de l'agent.
        state_after: instantané pris juste après l'exécution de l'agent.

    Returns:
        Un dict ne contenant que les clés dont la valeur a changé,
        avec pour valeur l'état APRÈS changement. Une clé apparue
        seulement dans `state_after` est incluse ; une clé disparue
        est incluse avec la valeur sentinelle None.
    """
    diff: dict[str, Any] = {}
    all_keys = set(state_before) | set(state_after)
    for key in all_keys:
        before_val = state_before.get(key)
        after_val = state_after.get(key)
        if not _values_equal(before_val, after_val):
            diff[key] = after_val
    return diff


def _values_equal(a: Any, b: Any) -> bool:
    """Égalité tolérante : compare les nombres avec une tolérance relative."""
    if isinstance(a, Number) and isinstance(b, Number) and not isinstance(a, bool) and not isinstance(b, bool):
        return abs(a - b) <= _FLOAT_RELATIVE_TOLERANCE * max(1.0, abs(a), abs(b))
    return a == b


def fuzzy_match(
    actual_delta: dict[str, Any], expected_delta: dict[str, Any]
) -> bool:
    """
    Vérifie que tous les changements attendus sont bien présents et
    corrects dans les changements réellement observés.

    Le sens de la comparaison est important : `actual_delta` peut
    contenir des changements supplémentaires non prévus par la tâche
    (ex. un timestamp de dernière modification) sans que cela invalide
    le succès — seuls les champs listés dans `expected_delta` sont
    exigeants.

    Args:
        actual_delta: différence réellement observée (compute_diff).
        expected_delta: différence attendue, définie par la tâche de test.

    Returns:
        True si chaque clé de `expected_delta` est présente dans
        `actual_delta` avec une valeur équivalente ; False sinon.
    """
    if not expected_delta:
        # Une tâche sans effet attendu sur l'état (ex. simple question en
        # lecture seule) est toujours considérée comme "correspondante" au
        # niveau de l'état — la validité de la réponse elle-même relève
        # d'une vérification complémentaire (hors périmètre de ce module).
        return True
    for key, expected_value in expected_delta.items():
        if key not in actual_delta:
            return False
        if not _values_equal(actual_delta[key], expected_value):
            return False
    return True


def verify(
    task: Task,
    result: AgentResult,
    state_before: dict[str, Any],
    state_after: dict[str, Any],
) -> VerificationResult:
    """
    Vérifie un AgentResult par rapport à l'état réel du système,
    indépendamment de ce que l'agent a déclaré.

    Args:
        task: la tâche exécutée, portant l'état attendu.
        result: le résultat brut produit par l'agent.
        state_before: instantané de l'état avant exécution.
        state_after: instantané de l'état après exécution.

    Returns:
        Un VerificationResult décrivant la différence observée, si elle
        correspond à l'attendu, et si l'agent a été en situation de
        sur-confiance (succès déclaré mais non vérifié).
    """
    actual_delta = compute_diff(state_before, state_after)
    matches = fuzzy_match(actual_delta, task.expected_state_delta)
    overconfidence = bool(result.declared_success) and not matches

    return VerificationResult(
        task_id=task.id,
        trial_id=result.trial_id,
        actual_state_delta=actual_delta,
        matches_expected=matches,
        overconfidence_detected=overconfidence,
    )
