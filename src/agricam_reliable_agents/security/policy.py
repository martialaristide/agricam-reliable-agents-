"""
Politique d'autorisation pour les outils sensibles.

Sépare strictement la question « l'agent PEUT-il techniquement appeler
cet outil » (défini par le serveur MCP) de « l'agent A-T-IL LE DROIT de
l'appeler dans ce contexte précis » (défini ici). C'est la mitigation
principale contre la catégorie OWASP LLM08 (Excessive Agency).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agricam_reliable_agents.models.data_models import ToolCall

# Outils dont l'exécution a un effet observable hors du système
# (notification, action marketplace) : ils exigent une vérification de
# périmètre explicite avant tout appel réel.
SENSITIVE_TOOLS: frozenset[str] = frozenset({"notify_farmer", "recommend_treatment"})


@dataclass(frozen=True, slots=True)
class ActionPolicy:
    """
    Politique d'autorisation pour la session courante d'un agent.

    `allowed_farmer_ids` et `allowed_parcel_ids` définissent le périmètre
    de données que l'agent est autorisé à affecter durant cette session
    (ex. un seul exploitant, ou une parcelle donnée en mode test). Toute
    tentative d'action en dehors de ce périmètre est refusée, quelle que
    soit la légitimité apparente de la requête utilisateur.
    """

    allowed_farmer_ids: frozenset[str] = field(default_factory=frozenset)
    allowed_parcel_ids: frozenset[str] = field(default_factory=frozenset)
    require_confirmation_for: frozenset[str] = field(
        default_factory=lambda: frozenset({"notify_farmer"})
    )

    def is_within_scope(self, call: ToolCall) -> bool:
        """
        Vérifie que les identifiants ciblés par l'appel sont dans le
        périmètre autorisé. Un outil qui ne référence ni farmer_id ni
        parcel_id est considéré hors de portée de cette vérification
        (retourne True) — le contrôle de périmètre ne s'applique qu'aux
        outils qui manipulent des identifiants métier.
        """
        farmer_id = call.arguments.get("farmer_id")
        farmer_out_of_scope = (
            farmer_id is not None
            and bool(self.allowed_farmer_ids)
            and farmer_id not in self.allowed_farmer_ids
        )

        parcel_id = call.arguments.get("parcel_id")
        parcel_out_of_scope = (
            parcel_id is not None
            and bool(self.allowed_parcel_ids)
            and parcel_id not in self.allowed_parcel_ids
        )

        return not (farmer_out_of_scope or parcel_out_of_scope)

    def requires_confirmation(self, call: ToolCall) -> bool:
        """Indique si cet outil exige une confirmation humaine explicite."""
        return call.tool_name in self.require_confirmation_for
