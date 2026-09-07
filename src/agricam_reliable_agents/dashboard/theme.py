"""
Palette et gabarit Plotly du dashboard.

Palette de référence validée (daltonisme, contraste) en modes clair et
sombre. Règles appliquées :
- une seule teinte (bleu, clair → foncé) pour les magnitudes ;
- teintes catégorielles attribuées dans un ordre fixe, jamais cyclées :
  au-delà de huit séries, on regroupe, on ne génère pas de couleur ;
- couleurs d'état réservées (jamais réutilisées pour une série) et
  toujours accompagnées d'une icône + libellé ;
- texte en encre de texte, jamais dans la couleur de la série ;
- traits fins, grille en filet, un seul axe par graphique.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Palette:
    surface: str
    page: str
    text_primary: str
    text_secondary: str
    muted: str
    gridline: str
    baseline: str
    series: tuple[str, ...]
    sequential: tuple[str, ...]  # clair → foncé
    status_good: str
    status_warning: str
    status_serious: str
    status_critical: str
    neutral: str  # gris de dé-emphase


LIGHT = Palette(
    surface="#fcfcfb", page="#f9f9f7",
    text_primary="#0b0b0b", text_secondary="#52514e", muted="#898781",
    gridline="#e1e0d9", baseline="#c3c2b7",
    series=("#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"),
    sequential=("#86b6ef", "#5598e7", "#2a78d6", "#1c5cab", "#104281"),
    status_good="#0ca30c", status_warning="#fab219", status_serious="#ec835a", status_critical="#d03b3b",
    neutral="#c3c2b7",
)

DARK = Palette(
    surface="#1a1a19", page="#0d0d0d",
    text_primary="#ffffff", text_secondary="#c3c2b7", muted="#898781",
    gridline="#2c2c2a", baseline="#383835",
    series=("#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"),
    sequential=("#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95"),
    status_good="#0ca30c", status_warning="#fab219", status_serious="#ec835a", status_critical="#d03b3b",
    neutral="#383835",
)

MAX_SERIES = len(LIGHT.series)
FONT_FAMILY = 'system-ui, -apple-system, "Segoe UI", sans-serif'


def palette_for(theme: str | None) -> Palette:
    return DARK if theme == "dark" else LIGHT


def series_color(palette: Palette, index: int) -> str:
    """Teinte du rang `index` (ordre fixe). Au-delà de la palette : gris de
    regroupement — jamais une teinte générée."""
    return palette.series[index] if index < len(palette.series) else palette.neutral


def base_layout(palette: Palette, *, height: int = 360) -> dict[str, Any]:
    """Gabarit commun : surfaces, encre, grille en filet, marges compactes."""
    axis = {
        "gridcolor": palette.gridline, "gridwidth": 1,
        "linecolor": palette.baseline, "linewidth": 1,
        "zeroline": False, "tickfont": {"color": palette.muted, "size": 12},
        "title": {"font": {"color": palette.text_secondary, "size": 12}},
        "automargin": True,  # les marges compactes s'élargissent pour ne jamais rogner les étiquettes
    }
    return {
        "height": height,
        "paper_bgcolor": palette.surface,
        "plot_bgcolor": palette.surface,
        "font": {"family": FONT_FAMILY, "color": palette.text_secondary, "size": 12},
        "margin": {"l": 8, "r": 16, "t": 32, "b": 8},
        "xaxis": dict(axis),
        "yaxis": dict(axis),
        "legend": {
            "orientation": "h", "yanchor": "bottom", "y": 1.02, "xanchor": "left", "x": 0,
            "font": {"color": palette.text_secondary}, "bgcolor": "rgba(0,0,0,0)",
        },
        "hoverlabel": {
            "bgcolor": palette.surface, "bordercolor": palette.baseline,
            "font": {"family": FONT_FAMILY, "color": palette.text_primary, "size": 12},
        },
        "hovermode": "closest",
    }
