"""
Fonctions statistiques du harnais de fiabilité.

Deux fonctions pures, sans effet de bord, entièrement testables :
- `wilson_score_interval` : intervalle de confiance pour une proportion,
  plus robuste que l'intervalle normal quand n est petit ou p proche de 0/1
  (cas fréquent ici : peu d'essais coûteux en appels API).
- `pass_k` : probabilité qu'un agent réussisse à chaque tentative sur k
  essais consécutifs, sous hypothèse d'indépendance des essais.

Référence : E. B. Wilson, "Probable Inference, the Law of Succession,
and Statistical Inference", Journal of the American Statistical
Association, 1927.
"""

from __future__ import annotations

import math

# z pour un intervalle de confiance à 95 % (loi normale centrée réduite)
Z_95 = 1.959963984540054


def wilson_score_interval(
    successes: int, n_trials: int, z: float = Z_95
) -> tuple[float, float]:
    """
    Calcule l'intervalle de confiance de Wilson pour une proportion.

    Args:
        successes: nombre d'essais réussis (0 <= successes <= n_trials).
        n_trials: nombre total d'essais (strictement positif).
        z: score z pour le niveau de confiance souhaité (1,96 ≈ 95 %).

    Returns:
        Un tuple (borne_basse, borne_haute), chacune dans [0.0, 1.0].

    Raises:
        ValueError: si n_trials <= 0 ou si successes est hors de [0, n_trials].
    """
    if n_trials <= 0:
        raise ValueError("n_trials doit être strictement positif.")
    if not (0 <= successes <= n_trials):
        raise ValueError("successes doit être compris entre 0 et n_trials.")

    n = float(n_trials)
    p_hat = successes / n
    z2 = z * z

    denominator = 1.0 + z2 / n
    center = (p_hat + z2 / (2.0 * n)) / denominator
    margin = (
        z * math.sqrt((p_hat * (1.0 - p_hat) / n) + (z2 / (4.0 * n * n)))
    ) / denominator

    low = max(0.0, center - margin)
    high = min(1.0, center + margin)
    return (low, high)


def pass_k(p_hat: float, k: int) -> float:
    """
    Probabilité qu'un agent réussisse à chaque tentative sur k essais
    consécutifs, en supposant les essais indépendants et de probabilité
    de succès constante p_hat.

    pass^k(p̂) = p̂ ** k

    Contrairement à pass@k (probabilité de succès en AU MOINS une
    tentative sur k, qui mesure la capacité brute), pass^k mesure la
    fiabilité répétée — la métrique pertinente pour un déploiement en
    production où l'agent doit réussir à chaque appel, pas une fois sur k.

    Args:
        p_hat: taux de succès estimé par essai, dans [0.0, 1.0].
        k: nombre d'essais consécutifs considérés (entier >= 1).

    Returns:
        La probabilité estimée de k succès consécutifs.

    Raises:
        ValueError: si p_hat hors de [0, 1] ou si k < 1.
    """
    if not (0.0 <= p_hat <= 1.0):
        raise ValueError("p_hat doit être compris entre 0.0 et 1.0.")
    if k < 1:
        raise ValueError("k doit être un entier supérieur ou égal à 1.")
    return p_hat ** k
