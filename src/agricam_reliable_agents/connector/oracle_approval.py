"""
Oracle par approbation de diff (approval testing) — Tâche 5bis, mode
PRINCIPAL de définition d'un oracle (section 3.2 du document de
référence), pour éliminer l'angle mort de l'omission humaine.

Écrire `expected_state_delta` de mémoire fait courir un risque documenté
en génie logiciel : on oublie plus facilement d'écrire un champ qu'on ne
le remarque en le voyant apparaître. Ce module inverse donc la charge :

1. `capture_baseline` exécute UNE FOIS un agent de référence jugé correct
   et capture l'INTÉGRALITÉ du diff d'état — réutilise strictement
   `verifier.success_verifier.compute_diff`, aucune réimplémentation.
2. Un humain relit ce diff complet (hors de ce module : une interface, un
   notebook, un simple `print`) et choisit les champs à approuver.
3. `approve_baseline` persiste l'oracle avec `validated_by_human=True` —
   n'est JAMAIS appelée automatiquement à la suite de `capture_baseline`,
   seulement après cette revue humaine explicite.
4. `verify_against_approved_oracle` (fonction SÉPARÉE de `verify()`,
   laquelle garde sa signature exacte pour ne risquer aucune régression
   sur le reste du harnais) signale tout champ qui change SANS être
   approuvé, plutôt que de l'ignorer silencieusement.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from agricam_reliable_agents.connector.oracle_repository import (
    OracleRepository,
    TaskOracle,
)
from agricam_reliable_agents.models.data_models import (
    AgentResult,
    Task,
    VerificationResult,
    utcnow,
)
from agricam_reliable_agents.reliability.harness import AgentRunner, StateSnapshotFn
from agricam_reliable_agents.verifier.success_verifier import compute_diff


@dataclass(frozen=True, slots=True)
class BaselineCapture:
    """Résultat d'une exécution de référence : diff COMPLET, avant toute
    sélection humaine — c'est ce qu'un humain relit pour approuver."""

    task_id: str
    trial_id: int
    state_before: dict[str, Any]
    state_after: dict[str, Any]
    diff: dict[str, Any]
    reference_result: AgentResult


def capture_baseline(
    task: Task,
    reference_agent_runner: AgentRunner,
    state_snapshot_fn: StateSnapshotFn,
    *,
    trial_id: int = 0,
) -> BaselineCapture:
    """
    Exécute `reference_agent_runner` UNE SEULE FOIS sur `task` et capture
    le diff complet de l'état.

    `reference_agent_runner` doit être une version de l'agent JUGÉE
    CORRECTE (référence humaine, ex. relecture manuelle d'un essai) —
    cette fonction ne juge de rien : elle capture ce qui a changé, pour
    qu'un humain le relise et l'approuve via `approve_baseline`.
    """
    state_before = state_snapshot_fn(task)
    reference_result = reference_agent_runner(task, trial_id)
    state_after = state_snapshot_fn(task)
    diff = compute_diff(state_before, state_after)
    return BaselineCapture(
        task_id=task.id, trial_id=trial_id, state_before=state_before,
        state_after=state_after, diff=diff, reference_result=reference_result,
    )


def approve_baseline(
    oracle_repository: OracleRepository,
    baseline: BaselineCapture,
    approved_fields: set[str] | frozenset[str],
    unexpected_field_policy: Literal["flag", "ignore"] = "flag",
) -> TaskOracle:
    """
    Persiste l'oracle de `baseline.task_id` avec `validated_by_human=True`.

    N'appeler qu'après une revue humaine EXPLICITE de `baseline.diff` —
    jamais automatiquement à la suite de `capture_baseline`, qui ne fait
    que capturer, jamais approuver (section 3.6 du document de référence :
    « aucun oracle n'est activé sans confirmation humaine explicite »).

    Raises:
        ValueError: si `approved_fields` contient un champ absent du diff
            capturé — impossible d'approuver un champ jamais observé.
    """
    unknown_fields = set(approved_fields) - set(baseline.diff)
    if unknown_fields:
        raise ValueError(
            f"approved_fields contient des champs absents du diff capturé : {sorted(unknown_fields)}."
        )
    oracle = TaskOracle(
        task_id=baseline.task_id, verification_method="approved_diff",
        verification_config={}, baseline_state_diff=dict(baseline.diff),
        approved_fields=tuple(sorted(approved_fields)), unexpected_field_policy=unexpected_field_policy,
        inferred=False, validated_by_human=True, validated_at=utcnow(),
    )
    return oracle_repository.save_oracle(oracle)


@dataclass(frozen=True, slots=True)
class OracleVerificationResult:
    """
    Verdict de `verify_against_approved_oracle` — une EXTENSION de
    `VerificationResult` (`as_verification_result()` en donne la vue
    compatible), jamais un remplacement : `verifier.success_verifier.verify`
    garde sa signature exacte, utilisée ailleurs dans le harnais.

    `unexpected_fields` est le signal propre à l'oracle par approbation de
    diff : les champs qui ont changé SANS être dans `approved_fields`.
    `fuzzy_match` (mécanisme historique, `expected_state_delta`) les
    ignore silencieusement par conception (cf. sa docstring : « des
    changements supplémentaires non prévus... sans que cela invalide le
    succès ») ; ici, sous `unexpected_field_policy="flag"`, ils invalident
    `matches_expected` au lieu d'être ignorés.
    """

    task_id: str
    trial_id: int
    actual_state_delta: dict[str, Any]
    matches_expected: bool
    overconfidence_detected: bool
    unexpected_fields: tuple[str, ...]

    def as_verification_result(self) -> VerificationResult:
        """Vue compatible `VerificationResult`, pour tout code qui
        consomme déjà ce contrat (ex. `ReliabilityRepository.record_trial`)
        sans avoir besoin de connaître `unexpected_fields`."""
        return VerificationResult(
            task_id=self.task_id, trial_id=self.trial_id, actual_state_delta=self.actual_state_delta,
            matches_expected=self.matches_expected, overconfidence_detected=self.overconfidence_detected,
        )


def verify_against_approved_oracle(
    oracle: TaskOracle,
    result: AgentResult,
    state_before: dict[str, Any],
    state_after: dict[str, Any],
) -> OracleVerificationResult:
    """
    Vérifie un `AgentResult` contre un oracle par approbation de diff.

    Comme `verify()` (non modifiée), la comparaison passe par
    `compute_diff` — réutilisation stricte. Un champ approuvé doit être
    présent dans le diff observé ET valoir exactement ce qu'il valait
    dans le diff de référence (`oracle.baseline_state_diff`) ; sinon
    `matches_expected` est faux (même principe que `fuzzy_match`, sans
    sa tolérance flottante — les champs de ce projet sont des statuts et
    des compteurs, jamais des mesures physiques approximatives).

    Différence avec `verify()` : un champ qui change SANS figurer dans
    `oracle.approved_fields` invalide en plus `matches_expected` quand
    `oracle.unexpected_field_policy == "flag"` (comportement par défaut).
    Sous `"ignore"`, il est toujours listé dans `unexpected_fields` (pour
    l'observabilité) mais n'invalide rien.

    Raises:
        ValueError: si `oracle.task_id` ne correspond pas à `result.task_id`.
    """
    if oracle.task_id != result.task_id:
        raise ValueError(
            f"L'oracle fourni concerne la tâche {oracle.task_id!r}, "
            f"pas {result.task_id!r} (résultat de l'essai)."
        )
    actual_delta = compute_diff(state_before, state_after)
    approved = set(oracle.approved_fields)
    baseline = oracle.baseline_state_diff or {}

    mismatched = [key for key in approved if actual_delta.get(key, object()) != baseline.get(key)]
    unexpected = tuple(sorted(set(actual_delta) - approved))

    matches = not mismatched
    if oracle.unexpected_field_policy == "flag" and unexpected:
        matches = False

    overconfidence = bool(result.declared_success) and not matches
    return OracleVerificationResult(
        task_id=oracle.task_id, trial_id=result.trial_id, actual_state_delta=actual_delta,
        matches_expected=matches, overconfidence_detected=overconfidence, unexpected_fields=unexpected,
    )
