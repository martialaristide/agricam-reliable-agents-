import math

import pytest

from agricam_reliable_agents.reliability.stats import pass_k, wilson_score_interval


def test_pass_k_matches_power_formula():
    assert math.isclose(pass_k(0.9, 10), 0.9 ** 10, rel_tol=1e-12)


def test_pass_k_k_equals_1_returns_p_hat():
    assert pass_k(0.73, 1) == pytest.approx(0.73)


def test_pass_k_rejects_invalid_p():
    with pytest.raises(ValueError):
        pass_k(1.5, 3)
    with pytest.raises(ValueError):
        pass_k(-0.1, 3)


def test_pass_k_rejects_invalid_k():
    with pytest.raises(ValueError):
        pass_k(0.9, 0)


def test_wilson_interval_bounds_are_valid_probability():
    low, high = wilson_score_interval(successes=18, n_trials=20)
    assert 0.0 <= low < high <= 1.0


def test_wilson_interval_widens_with_fewer_trials():
    low_small_n, high_small_n = wilson_score_interval(successes=9, n_trials=10)
    low_large_n, high_large_n = wilson_score_interval(successes=90, n_trials=100)
    width_small_n = high_small_n - low_small_n
    width_large_n = high_large_n - low_large_n
    assert width_small_n > width_large_n


def test_wilson_interval_perfect_score_has_nonzero_lower_bound_uncertainty():
    # Pour 20/20 succès, la borne haute de Wilson atteint exactement 1.0
    # (propriété mathématique de la méthode), mais la borne BASSE reste
    # strictement inférieure à 1.0 : l'incertitude sur un petit échantillon
    # est donc bien représentée, contrairement à un intervalle qui
    # collapserait au point (1.0, 1.0).
    low, high = wilson_score_interval(successes=20, n_trials=20)
    assert high == pytest.approx(1.0)
    assert low < 1.0
    assert low > 0.8  # borne basse resserrée mais non triviale pour n=20

def test_wilson_rejects_invalid_inputs():
    with pytest.raises(ValueError):
        wilson_score_interval(successes=5, n_trials=0)
    with pytest.raises(ValueError):
        wilson_score_interval(successes=21, n_trials=20)
