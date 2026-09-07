"""
Inférence de contrat (mode assisté) — Tâche 7 (Phase E, section 3.5 du
document de référence).

Rappel de positionnement, non négociable : cette inférence propose un
point de départ, JAMAIS un substitut à l'approbation de diff (Tâche 5bis).
Elle peut pré-remplir un `approved_fields` candidat ; elle ne le valide
jamais elle-même. Une fonction qui « devine toujours quelque chose » est
le bug le plus dangereux ici, pas une fonctionnalité manquante — c'est
pourquoi `infer_oracle_candidate` retourne explicitement `None` dès que le
signal n'est pas clair, plutôt que de proposer un champ au hasard.
"""

from __future__ import annotations

from dataclasses import dataclass

from agricam_reliable_agents.connector.oracle_repository import TaskOracle

# Noms de propriété reconnus comme un signal de statut/état — correspondance
# exacte (insensible à la casse) ou en suffixe (`treatment_status`,
# `diagnostic_state`...). Volontairement courte et explicite : chaque ajout
# élargit ce que la fonction est prête à "deviner", au risque de proposer un
# champ qui n'est pas réellement un indicateur de succès.
_STATUS_LIKE_NAMES = ("status", "state")


@dataclass(frozen=True, slots=True)
class OracleCandidate:
    """
    Point de départ PROPOSÉ pour un oracle — jamais un oracle validé.

    `suggested_fields` sont des noms de propriété du schéma de retour de
    l'outil (pas encore des clés d'instantané AgriCam au format
    `"entité.id.champ"` : c'est à l'humain, lors de l'approbation, de les
    relier au diff réel observé). `rationale` explique EN CLAIR pourquoi
    ce champ a été retenu, pour qu'un humain qui relit la proposition
    comprenne le raisonnement plutôt que de lui faire confiance aveuglément.
    """

    suggested_fields: tuple[str, ...]
    rationale: str


def infer_oracle_candidate(tool_schema: dict) -> OracleCandidate | None:
    """
    Propose un point de départ à partir du schéma de retour d'un outil
    (schéma JSON — OpenAPI, schéma MCP, ou tout dict au format
    `{"properties": {...}}`).

    Détecte une propriété de premier niveau dont le nom EST `status`/`state`
    (insensible à la casse) ou s'en TERMINE par un tiret bas suivi de l'un
    de ces mots (`treatment_status`, `diagnostic_state`...) — cas simple et
    honnête demandé par la Tâche 7.1. Toute autre situation retourne
    explicitement `None` : un schéma sans un tel champ, un schéma dont la
    forme est inattendue, ou une entrée malformée. Ne lève jamais
    d'exception — un schéma malformé est un cas ATTENDU (fourni par un
    agent tiers imparfaitement documenté), pas une erreur de programmation.

    Args:
        tool_schema: schéma JSON du retour d'un outil. Doit être un `dict`
            avec une clé `"properties"` elle-même un `dict` pour produire
            un résultat ; toute autre forme retourne `None`.

    Returns:
        Un `OracleCandidate` si un signal exploitable a été trouvé, sinon
        `None` — jamais une proposition non fondée.
    """
    if not isinstance(tool_schema, dict):
        return None
    properties = tool_schema.get("properties")
    if not isinstance(properties, dict) or not properties:
        return None

    for name in properties:
        if not isinstance(name, str):
            continue
        lowered = name.lower()
        if lowered in _STATUS_LIKE_NAMES or any(
            lowered.endswith(f"_{candidate}") for candidate in _STATUS_LIKE_NAMES
        ):
            return OracleCandidate(
                suggested_fields=(name,),
                rationale=(
                    f"Le champ de retour {name!r} ressemble à un indicateur de statut "
                    "(nom reconnu : 'status'/'state' ou terminaison '_status'/'_state') — "
                    "à confirmer par une relecture du diff réel avant approbation."
                ),
            )
    return None


def candidate_to_unvalidated_oracle(
    candidate: OracleCandidate, task_id: str, *, verification_config: dict | None = None,
) -> TaskOracle:
    """
    Convertit un `OracleCandidate` en `TaskOracle` NON VALIDÉ
    (`inferred=True`, `validated_by_human=False`, `validated_at=None` —
    codé en dur, sans paramètre pour le changer depuis cette fonction).

    Aucun chemin de ce module ne peut produire un oracle
    `validated_by_human=True` : la seule façon d'en obtenir un reste
    l'approbation humaine explicite (`oracle_approval.approve_baseline`,
    Tâche 5bis) ou une construction manuelle et délibérée de `TaskOracle`
    par l'appelant — jamais un effet de bord automatique de l'inférence.
    La contrainte de la Tâche 5.3 (`orchestration.require_validated_oracle`)
    s'applique donc à l'identique à un oracle inféré : tant qu'il n'a pas
    été approuvé séparément, aucune campagne ne peut s'en servir.
    """
    return TaskOracle(
        task_id=task_id, verification_method="manual", verification_config=verification_config or {},
        approved_fields=candidate.suggested_fields, inferred=True,
        validated_by_human=False, validated_at=None,
    )
