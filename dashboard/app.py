"""
Dashboard de fiabilité AgriCam Reliable Agents (Streamlit, phase 5).

Lit le dépôt de campagnes (`ReliabilityRepository`) alimenté par
`scripts/run_campaign.py` et répond aux trois questions du projet :

1. Quelle est la fiabilité réelle de l'agent par tâche (p̂, IC de Wilson) ?
2. Comment s'effondre-t-elle avec la répétition (pass^k) et la longueur
   de la tâche (complexité) ?
3. À quelle fréquence l'agent prétend avoir réussi alors que l'état réel
   dit le contraire (sur-confiance), et combien d'actions hors périmètre
   la garde a-t-elle bloquées ?

Lancement :
    PYTHONPATH=src streamlit run dashboard/app.py
La base est choisie par `AGRICAM_DATABASE_URL` (ou saisie dans la barre latérale).
"""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from agricam_reliable_agents.dashboard import queries
from agricam_reliable_agents.dashboard.theme import (
    MAX_SERIES,
    Palette,
    base_layout,
    palette_for,
    series_color,
)
from agricam_reliable_agents.persistence.engine import (
    DATABASE_URL_ENV_VAR,
    database_url_from_env,
)
from agricam_reliable_agents.reliability.repository import ReliabilityRepository

st.set_page_config(page_title="AgriCam Reliable Agents", page_icon="🌱", layout="wide")


# ---------------------------------------------------------------------------
# Accès aux données (mis en cache par URL)
# ---------------------------------------------------------------------------

@st.cache_resource(show_spinner=False)
def get_repository(url: str) -> ReliabilityRepository:
    return ReliabilityRepository.from_url(url)


def current_theme() -> str | None:
    """« light » ou « dark » selon le thème du visiteur ; None si l'API
    `st.context.theme` n'est pas disponible (versions anciennes, tests)."""
    theme = getattr(st.context, "theme", None)
    return getattr(theme, "type", None)


def pct(value: float) -> str:
    return f"{value * 100:.1f} %"


# ---------------------------------------------------------------------------
# Graphiques
# ---------------------------------------------------------------------------

def reliability_chart(reports: pd.DataFrame, palette: Palette) -> go.Figure:
    """p̂ par tâche (barres horizontales, une teinte) avec l'IC de Wilson."""
    ordered = reports.iloc[::-1]  # Plotly dessine de bas en haut : on inverse pour lire de haut en bas
    fig = go.Figure(go.Bar(
        x=ordered["p_hat"], y=ordered["task_id"], orientation="h",
        marker={"color": palette.sequential[2], "line": {"width": 0}},
        width=0.55,
        error_x={
            "type": "data", "symmetric": False,
            "array": ordered["ci_high"] - ordered["p_hat"],
            "arrayminus": ordered["p_hat"] - ordered["ci_low"],
            "color": palette.text_secondary, "thickness": 1.5, "width": 4,
        },
        customdata=ordered[["n_success", "n_trials", "ci_low", "ci_high", "complexity"]].astype(object).values,
        hovertemplate=(
            "<b>%{y}</b> · %{customdata[4]}<br>p̂ = %{x:.3f} "
            "(%{customdata[0]}/%{customdata[1]} essais)<br>"
            "IC 95 % : [%{customdata[2]:.3f}, %{customdata[3]:.3f}]<extra></extra>"
        ),
    ))
    # Valeur en étiquette directe, placée APRÈS la borne haute de l'IC pour ne
    # jamais chevaucher la barre d'erreur ; encre de texte, pas couleur de série.
    fig.add_trace(go.Scatter(
        x=ordered["ci_high"] + 0.02, y=ordered["task_id"], mode="text",
        text=[f"{v:.2f}" for v in ordered["p_hat"]], textposition="middle right",
        textfont={"color": palette.text_secondary, "size": 12},
        hoverinfo="skip", showlegend=False,
    ))
    layout = base_layout(palette, height=90 + 48 * len(reports))
    layout["xaxis"].update(range=[0, 1.16], tickformat=".0%", title_text="p̂ (succès vérifiés) et IC de Wilson 95 %")
    layout["yaxis"].update(showgrid=False, tickfont={"color": palette.text_primary, "size": 12})
    layout["bargap"] = 0.45
    layout["showlegend"] = False  # série unique : le titre de section la nomme, pas de légende
    fig.update_layout(**layout)
    return fig


def pass_k_chart(curves: pd.DataFrame, task_order: list[str], palette: Palette) -> go.Figure:
    """pass^k = p̂^k par tâche : une ligne par tâche, teinte fixe par rang,
    étiquette directe en bout de ligne, légende toujours présente."""
    fig = go.Figure()
    for index, task_id in enumerate(task_order):
        serie = curves[curves["task_id"] == task_id]
        color = series_color(palette, index)
        fig.add_trace(go.Scatter(
            x=serie["k"], y=serie["pass_k"], mode="lines+markers", name=task_id,
            line={"color": color, "width": 2}, marker={"size": 8, "color": color,
                                                      "line": {"color": palette.surface, "width": 2}},
            hovertemplate=f"<b>{task_id}</b><br>k = %{{x}}<br>pass^k = %{{y:.3f}}<extra></extra>",
        ))
        if index < 4 and not serie.empty:
            last = serie.iloc[-1]
            fig.add_annotation(
                x=last["k"], y=last["pass_k"], text=task_id, showarrow=False,
                xanchor="left", xshift=8, font={"color": palette.text_secondary, "size": 11},
            )
    k_max = int(curves["k"].max()) if not curves.empty else 1
    layout = base_layout(palette, height=380)
    layout["xaxis"].update(title_text="k (succès consécutifs exigés)", dtick=1, showgrid=False,
                           range=[0.5, k_max + 0.5])
    layout["yaxis"].update(title_text="pass^k = p̂^k", range=[0, 1.05], tickformat=".0%")
    layout["margin"]["r"] = 110
    layout["hovermode"] = "x unified"
    fig.update_layout(**layout)
    return fig


def comparison_chart(frame: pd.DataFrame, palette: Palette) -> go.Figure:
    """p̂ par tâche et par campagne (barres groupées, une teinte par campagne)."""
    fig = go.Figure()
    for index, (campaign_id, group) in enumerate(frame.groupby("campaign_id", sort=True)):
        name = group["campaign"].iloc[0]
        fig.add_trace(go.Bar(
            x=group["task_id"], y=group["p_hat"], name=f"#{campaign_id} {name}",
            marker={"color": series_color(palette, index), "line": {"color": palette.surface, "width": 2}},
            error_y={
                "type": "data", "symmetric": False,
                "array": group["ci_high"] - group["p_hat"], "arrayminus": group["p_hat"] - group["ci_low"],
                "color": palette.text_secondary, "thickness": 1.5, "width": 4,
            },
            hovertemplate=f"<b>{name}</b><br>%{{x}}<br>p̂ = %{{y:.3f}}<extra></extra>",
        ))
    layout = base_layout(palette, height=380)
    layout["yaxis"].update(range=[0, 1.05], tickformat=".0%", title_text="p̂")
    layout["xaxis"].update(showgrid=False, tickfont={"color": palette.text_primary})
    layout["barmode"] = "group"
    layout["bargap"] = 0.45
    layout["bargroupgap"] = 0.08
    fig.update_layout(**layout)
    return fig


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

def main() -> None:
    palette = palette_for(current_theme())

    with st.sidebar:
        st.title("🌱 AgriCam Reliable Agents")
        url = st.text_input(
            "Base des campagnes", value=database_url_from_env(),
            help=f"URL SQLAlchemy ; par défaut la variable {DATABASE_URL_ENV_VAR}.",
        )
        repository = get_repository(url)
        campaigns = repository.list_campaigns()
        if not campaigns:
            st.info("Aucune campagne dans cette base. Lancez d'abord "
                    "`PYTHONPATH=src python scripts/run_campaign.py`.")
            st.stop()
        labels = {c.id: f"#{c.id} · {c.name} · {c.started_at:%Y-%m-%d %H:%M}" for c in campaigns}
        campaign_id = st.selectbox("Campagne", options=list(labels), format_func=labels.get)
        k_max = st.slider("k maximal (pass^k)", min_value=3, max_value=20, value=queries.DEFAULT_K_MAX)
        compare_ids = st.multiselect(
            "Comparer avec", options=[c.id for c in campaigns if c.id != campaign_id],
            format_func=labels.get,
        )

    campaign = next(c for c in campaigns if c.id == campaign_id)
    st.title(f"Campagne #{campaign.id} — {campaign.name}")
    meta = f"Modèle : **{campaign.model or 'non renseigné'}** · démarrée le {campaign.started_at:%d/%m/%Y à %H:%M} UTC"
    if campaign.notes:
        meta += f" · {campaign.notes}"
    st.caption(meta)

    overview = queries.campaign_overview(repository, campaign_id)
    reports = queries.reports_frame(repository, campaign_id)
    if reports.empty:
        st.warning("Cette campagne n'a aucun rapport archivé.")
        st.stop()

    # ---- KPI --------------------------------------------------------------------
    k1, k2, k3, k4, k5 = st.columns(5)
    k1.metric("Essais vérifiés", f"{overview.n_success} / {overview.n_trials}",
              help="Succès selon le Success Verifier (état réel), pas selon l'agent.")
    k2.metric("p̂ global", pct(overview.p_hat))
    k3.metric("⚠️ Sur-confiance", pct(overview.overconfidence_rate),
              help="Part des essais où l'agent a déclaré un succès non confirmé par l'état réel.")
    k4.metric("🛡️ Incidents bloqués", f"{overview.n_blocked} / {overview.n_incidents}",
              help="Actions hors périmètre interceptées par la garde (OWASP LLM08).")
    k5.metric("Coût estimé", f"{overview.total_cost_usd:.4f} USD",
              help=f"Latence moyenne : {overview.mean_latency_ms:.0f} ms par essai.")

    # ---- Fiabilité par tâche ------------------------------------------------------
    left, right = st.columns((1, 1))
    with left:
        st.subheader("Fiabilité par tâche")
        st.plotly_chart(reliability_chart(reports, palette), use_container_width=True)
    with right:
        st.subheader("Effondrement avec la répétition (pass^k)")
        task_order = list(reports["task_id"])
        if len(task_order) > MAX_SERIES:
            st.caption(f"Seules les {MAX_SERIES} premières tâches sont colorées ; les autres sont en gris.")
        curves = queries.pass_k_frame(repository.list_reports(campaign_id), k_max)
        st.plotly_chart(pass_k_chart(curves, task_order, palette), use_container_width=True)

    with st.expander("Tableau des rapports"):
        st.dataframe(
            reports.style.format({c: "{:.3f}" for c in reports.columns if reports[c].dtype.kind == "f"}),
            use_container_width=True, hide_index=True,
        )

    # ---- Comparaison entre campagnes ----------------------------------------------
    if compare_ids:
        st.subheader("Comparaison entre campagnes")
        comparison = queries.comparison_frame(repository, [campaign_id, *compare_ids])
        st.plotly_chart(comparison_chart(comparison, palette), use_container_width=True)

    # ---- Essais et incidents --------------------------------------------------------
    st.subheader("Essais")
    trials = queries.trials_frame(repository, campaign_id)
    f1, f2 = st.columns((1, 2))
    only_overconfident = f1.toggle("Sur-confiance uniquement", value=False)
    task_filter = f2.multiselect("Tâches", options=task_order, default=task_order)
    filtered = trials[trials["task_id"].isin(task_filter)]
    if only_overconfident:
        filtered = filtered[filtered["overconfidence_detected"]]
    st.dataframe(
        filtered.drop(columns=["recorded_at"]),
        use_container_width=True, hide_index=True,
        column_config={
            "verified_success": st.column_config.CheckboxColumn("✅ vérifié"),
            "declared_success": st.column_config.CheckboxColumn("déclaré"),
            "overconfidence_detected": st.column_config.CheckboxColumn("⚠️ sur-confiance"),
            "max_steps_exceeded": st.column_config.CheckboxColumn("max_steps"),
            "latency_ms": st.column_config.NumberColumn("latence (ms)", format="%.0f"),
            "cost_usd": st.column_config.NumberColumn("coût (USD)", format="%.5f"),
        },
    )

    incidents = queries.incidents_frame(repository, campaign_id)
    st.subheader(f"Incidents de sécurité ({len(incidents)})")
    if incidents.empty:
        st.caption("Aucun incident journalisé pour cette campagne.")
    else:
        st.dataframe(incidents, use_container_width=True, hide_index=True,
                     column_config={"blocked": st.column_config.CheckboxColumn("🛡️ bloqué")})


main()
