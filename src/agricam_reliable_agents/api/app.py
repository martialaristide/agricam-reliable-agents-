"""
API JSON de l'interface de vérification, au-dessus du schéma SQL existant
(`campaigns`, `tasks`, `trials`, `reports`, `security_incidents`).

Décisions de conception
-----------------------
- Starlette plutôt que FastAPI : déjà présent (dépendance du SDK MCP), et
  huit routes n'ont pas besoin d'un modèle de validation généré ; la
  validation du seul corps entrant (`POST /api/campaigns`) est explicite.
- Aucun calcul statistique ici : p̂, Wilson et pass^k viennent de
  `reliability/stats.py` ; l'API assemble et nomme.
- Les libellés sont ceux de l'utilisateur (« vérifié », « échec avoué »,
  « sur-confiance », « bloqué », « régression »), calculés une fois côté
  serveur pour que le front n'ait pas à réinterpréter le schéma.
- Une campagne lancée depuis l'interface tourne dans un thread avec sa
  propre `ReliabilityRepository` (fichier SQLite ou PostgreSQL) ; son état
  (« en cours », « terminée », « échouée ») est suivi en mémoire par
  `CampaignLauncher`. Avec une base en mémoire (`sqlite://`) le lancement
  est synchrone : un `StaticPool` ne se partage pas entre threads.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from agricam_reliable_agents import __version__
from agricam_reliable_agents.models.data_models import (
    AttackCategory,
    ReliabilityReport,
    Task,
)
from agricam_reliable_agents.persistence.engine import (
    database_url_from_env,
    is_memory_sqlite_url,
)
from agricam_reliable_agents.reliability.repository import (
    CampaignRecord,
    ReliabilityRepository,
    TrialRecord,
)
from agricam_reliable_agents.reliability.simulation import run_simulated_campaign
from agricam_reliable_agents.reliability.stats import (
    pass_k,
    pass_k_interval,
    wilson_score_interval,
)

STATIC_DIR = Path(__file__).parent / "static"
DEFAULT_K_MAX = 10
MAX_K = 50
MAX_TRIALS_FROM_UI = 500

ATTACK_LABELS: dict[AttackCategory, str] = {
    AttackCategory.PROMPT_INJECTION_DIRECT: "Injection de prompt directe (LLM01)",
    AttackCategory.PROMPT_INJECTION_INDIRECT: "Injection de prompt indirecte (LLM01)",
    AttackCategory.SENSITIVE_INFO_DISCLOSURE: "Fuite d'information sensible (LLM06)",
    AttackCategory.EXCESSIVE_AGENCY: "Action hors périmètre (LLM08)",
    AttackCategory.OVERRELIANCE: "Confiance excessive (LLM09)",
}

COMPLEXITY_LABELS: dict[str, str] = {
    "1-2_steps": "1 à 2 étapes",
    "3-5_steps": "3 à 5 étapes",
    "6+_steps": "6 étapes ou plus",
}

TRIAL_STATE_LABELS = {
    "verifie": "vérifié",
    "echec": "échec avoué",
    "surconfiance": "sur-confiance",
}


# ---------------------------------------------------------------------------
# Lancement de campagnes depuis l'interface
# ---------------------------------------------------------------------------

@dataclass
class RunStatus:
    status: str  # "en cours" | "terminée" | "échouée"
    error: str | None = None


class CampaignLauncher:
    """Lance des campagnes simulées et suit leur état en mémoire."""

    def __init__(self, database_url: str, *, background: bool = True) -> None:
        self._database_url = database_url
        self._background = background and not is_memory_sqlite_url(database_url)
        self._runs: dict[int, RunStatus] = {}
        self._threads: dict[int, threading.Thread] = {}
        self._lock = threading.Lock()

    def status_of(self, campaign_id: int) -> RunStatus | None:
        with self._lock:
            return self._runs.get(campaign_id)

    def launch(self, repository: ReliabilityRepository, *, name: str, n_trials: int,
               p_step: float, seed: int) -> int:
        campaign_id = repository.start_campaign(
            name, model=f"simulated-p{p_step}", notes="lancée depuis l'interface de vérification",
        )
        with self._lock:
            self._runs[campaign_id] = RunStatus("en cours")

        def work() -> None:
            repo = repository if not self._background else ReliabilityRepository.from_url(self._database_url)
            try:
                run_simulated_campaign(
                    repo, name=name, n_trials=n_trials, p_step=p_step, seed=seed,
                    campaign_id=campaign_id,
                )
            except Exception as exc:  # noqa: BLE001 - l'état « échouée » doit porter n'importe quelle cause
                with self._lock:
                    self._runs[campaign_id] = RunStatus("échouée", str(exc))
            else:
                with self._lock:
                    self._runs[campaign_id] = RunStatus("terminée")
            finally:
                if repo is not repository:
                    repo.engine.dispose()

        if self._background:
            thread = threading.Thread(target=work, name=f"campaign-{campaign_id}", daemon=True)
            with self._lock:
                self._threads[campaign_id] = thread
            thread.start()
        else:
            work()
        return campaign_id

    def wait(self, campaign_id: int, timeout: float | None = None) -> None:
        """Attend la fin d'une campagne lancée en arrière-plan (tests, arrêt propre)."""
        thread = self._threads.get(campaign_id)
        if thread is not None:
            thread.join(timeout)


# ---------------------------------------------------------------------------
# Assemblage des réponses
# ---------------------------------------------------------------------------

def _iso(value: datetime) -> str:
    return value.isoformat()


def _trial_state(t: TrialRecord) -> str:
    if t.verified_success:
        return "verifie"
    if t.overconfidence_detected:
        return "surconfiance"
    return "echec"


def _proportion(successes: int, n: int) -> dict[str, Any] | None:
    if n <= 0:
        return None
    low, high = wilson_score_interval(successes, n)
    return {"p_hat": successes / n, "ci_low": low, "ci_high": high, "n": n, "successes": successes}


def _campaign_summary(repo: ReliabilityRepository, launcher: CampaignLauncher,
                      campaign: CampaignRecord) -> dict[str, Any]:
    trials = repo.list_trials(campaign.id)
    reports = repo.list_reports(campaign.id)
    incidents = repo.list_incidents(campaign.id)
    n_success = sum(t.verified_success for t in trials)
    run = launcher.status_of(campaign.id)
    if run is not None:
        status, error = run.status, run.error
    else:
        status, error = ("terminée" if reports else "vide"), None
    return {
        "id": campaign.id,
        "name": campaign.name,
        "model": campaign.model,
        "notes": campaign.notes,
        "started_at": _iso(campaign.started_at),
        "status": status,
        "error": error,
        "n_tasks": len(reports) if reports else len({t.task_id for t in trials}),
        "n_trials": len(trials),
        "n_success": n_success,
        "proportion": _proportion(n_success, len(trials)),
        "n_overconfident": sum(t.overconfidence_detected for t in trials),
        "n_honest_failures": sum(1 for t in trials if _trial_state(t) == "echec"),
        "incidents": {
            "total": len(incidents),
            "blocked": sum(i.blocked for i in incidents),
            "unblocked": sum(not i.blocked for i in incidents),
        },
        "total_cost_usd": sum(t.cost_usd for t in trials),
        "mean_latency_ms": (sum(t.latency_ms for t in trials) / len(trials)) if trials else None,
    }


def _task_summary(task: Task | None, task_id: str, report: ReliabilityReport | None,
                  trials: list[TrialRecord]) -> dict[str, Any]:
    complexity = task.complexity.value if task else None
    states = [_trial_state(t) for t in trials]
    proportion = (
        {"p_hat": report.p_hat, "ci_low": report.wilson_ci[0], "ci_high": report.wilson_ci[1],
         "n": report.n_trials, "successes": report.n_success}
        if report else _proportion(sum(s == "verifie" for s in states), len(trials))
    )
    return {
        "task_id": task_id,
        "prompt": task.prompt if task else None,
        "category": task.category if task else None,
        "complexity": complexity,
        "complexity_label": COMPLEXITY_LABELS.get(complexity or "", "inconnue"),
        "expected_state_delta": dict(task.expected_state_delta) if task else {},
        "proportion": proportion,
        "pass_k": {str(k): v for k, v in sorted(report.pass_k.items())} if report else {},
        "n_trials": len(trials),
        "n_verified": states.count("verifie"),
        "n_honest_failures": states.count("echec"),
        "n_overconfident": states.count("surconfiance"),
        "mean_cost_usd": (sum(t.cost_usd for t in trials) / len(trials)) if trials else None,
        "mean_latency_ms": (sum(t.latency_ms for t in trials) / len(trials)) if trials else None,
    }


def _trial_row(t: TrialRecord) -> dict[str, Any]:
    state = _trial_state(t)
    return {
        "task_id": t.task_id,
        "trial_id": t.trial_id,
        "state": state,
        "state_label": TRIAL_STATE_LABELS[state],
        "verified_success": t.verified_success,
        "declared_success": t.declared_success,
        "overconfidence_detected": t.overconfidence_detected,
        "max_steps_exceeded": t.max_steps_exceeded,
        "error": t.error,
        "n_tool_calls": len(t.tool_calls),
        "n_tool_calls_ok": sum(c.status == "ok" for c in t.tool_calls),
        "n_tool_calls_failed": sum(c.status != "ok" for c in t.tool_calls),
        "tool_call_names": list(t.tool_call_names),
        "latency_ms": t.latency_ms,
        "cost_usd": t.cost_usd,
        "recorded_at": _iso(t.recorded_at),
    }


def _diff_rows(expected: dict[str, Any], actual: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for key, value in expected.items():
        if key not in actual:
            rows.append({"key": key, "expected": value, "actual": None, "status": "manquant"})
        elif actual[key] == value:
            rows.append({"key": key, "expected": value, "actual": actual[key], "status": "ok"})
        else:
            rows.append({"key": key, "expected": value, "actual": actual[key], "status": "different"})
    for key, value in actual.items():
        if key not in expected:
            rows.append({"key": key, "expected": None, "actual": value, "status": "inattendu"})
    return rows


def _compare_verdict(before: dict[str, Any] | None, after: dict[str, Any] | None) -> str:
    if before is None or after is None:
        return "incomparable"
    if after["ci_high"] < before["p_hat"]:
        return "regression"
    if after["ci_low"] > before["p_hat"]:
        return "progres"
    if after["p_hat"] < before["p_hat"]:
        return "baisse"
    return "stable"


COMPARE_LABELS = {
    "regression": "régression",
    "baisse": "baisse non significative",
    "stable": "stable",
    "progres": "progrès",
    "incomparable": "absente d'une des campagnes",
}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def _repo(request: Request) -> ReliabilityRepository:
    return request.app.state.repository


def _launcher(request: Request) -> CampaignLauncher:
    return request.app.state.launcher


def _campaign_or_404(repo: ReliabilityRepository, campaign_id: int) -> CampaignRecord:
    for campaign in repo.list_campaigns():
        if campaign.id == campaign_id:
            return campaign
    raise HTTPException(404, "Cette campagne n'existe pas.")


def _int_param(request: Request, name: str) -> int:
    try:
        return int(request.path_params[name])
    except (KeyError, ValueError) as exc:
        raise HTTPException(400, f"Le paramètre {name} doit être un entier.") from exc


async def list_campaigns(request: Request) -> JSONResponse:
    repo, launcher = _repo(request), _launcher(request)
    return JSONResponse({
        "version": __version__,
        "campaigns": [_campaign_summary(repo, launcher, c) for c in repo.list_campaigns()],
    })


async def create_campaign(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(400, "Le corps de la requête doit être du JSON.") from exc
    if not isinstance(body, dict):
        raise HTTPException(400, "Le corps de la requête doit être un objet JSON.")

    n_trials = body.get("n_trials", 30)
    p_step = body.get("p_step", 0.9)
    seed = body.get("seed", 42)
    name = body.get("name") or f"simulated-p{p_step}-n{n_trials}"
    if not isinstance(n_trials, int) or isinstance(n_trials, bool) or not (1 <= n_trials <= MAX_TRIALS_FROM_UI):
        raise HTTPException(400, f"Le nombre d'essais doit être un entier entre 1 et {MAX_TRIALS_FROM_UI}.")
    if not isinstance(p_step, (int, float)) or isinstance(p_step, bool) or not (0.0 <= p_step <= 1.0):
        raise HTTPException(400, "La probabilité de réussite par étape doit être entre 0 et 1.")
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise HTTPException(400, "La graine doit être un entier.")
    if not isinstance(name, str) or not name.strip() or len(name) > 128:
        raise HTTPException(400, "Le nom doit faire entre 1 et 128 caractères.")

    campaign_id = _launcher(request).launch(
        _repo(request), name=name.strip(), n_trials=n_trials, p_step=float(p_step), seed=seed,
    )
    status = _launcher(request).status_of(campaign_id)
    return JSONResponse({"id": campaign_id, "status": status.status if status else "en cours"}, status_code=202)


async def get_campaign(request: Request) -> JSONResponse:
    repo, launcher = _repo(request), _launcher(request)
    campaign_id = _int_param(request, "campaign_id")
    campaign = _campaign_or_404(repo, campaign_id)
    trials = repo.list_trials(campaign_id)
    reports = {r.task_id: r for r in repo.list_reports(campaign_id)}
    task_ids = list(reports) or sorted({t.task_id for t in trials})
    by_task = {task_id: [t for t in trials if t.task_id == task_id] for task_id in task_ids}
    tasks = [
        _task_summary(repo.get_task(task_id), task_id, reports.get(task_id), by_task[task_id])
        for task_id in task_ids
    ]
    tasks.sort(key=lambda t: (t["complexity"] or "~", t["task_id"]))
    return JSONResponse({"campaign": _campaign_summary(repo, launcher, campaign), "tasks": tasks})


async def get_task(request: Request) -> JSONResponse:
    repo = _repo(request)
    campaign_id = _int_param(request, "campaign_id")
    task_id = request.path_params["task_id"]
    _campaign_or_404(repo, campaign_id)
    trials = repo.list_trials(campaign_id, task_id)
    report = next((r for r in repo.list_reports(campaign_id) if r.task_id == task_id), None)
    if not trials and report is None:
        raise HTTPException(404, "Cette tâche n'a pas été évaluée dans cette campagne.")
    try:
        k_max = int(request.query_params.get("k_max", DEFAULT_K_MAX))
    except ValueError as exc:
        raise HTTPException(400, "k_max doit être un entier.") from exc
    k_max = max(1, min(k_max, MAX_K))
    summary = _task_summary(repo.get_task(task_id), task_id, report, trials)
    proportion = summary["proportion"]
    p_hat = proportion["p_hat"] if proportion else 0.0
    ci = (proportion["ci_low"], proportion["ci_high"]) if proportion else (0.0, 0.0)
    curve = []
    for k in range(1, k_max + 1):
        low, high = pass_k_interval(ci, k)
        curve.append({"k": k, "value": pass_k(p_hat, k), "low": low, "high": high})
    return JSONResponse({
        "campaign_id": campaign_id,
        "task": summary,
        "pass_k_curve": curve,
        "trials": [_trial_row(t) for t in trials],
    })


async def get_trial(request: Request) -> JSONResponse:
    repo = _repo(request)
    campaign_id = _int_param(request, "campaign_id")
    trial_id = _int_param(request, "trial_id")
    task_id = request.path_params["task_id"]
    _campaign_or_404(repo, campaign_id)
    trial = repo.get_trial(campaign_id, task_id, trial_id)
    if trial is None:
        raise HTTPException(404, "Cet essai n'existe pas dans cette campagne.")
    task = repo.get_task(task_id)
    # L'attendu figé dans l'essai fait foi (la table `tasks` peut avoir été réécrite depuis).
    if trial.expected_state_delta is not None:
        expected = dict(trial.expected_state_delta)
    else:
        expected = dict(task.expected_state_delta) if task else {}
    complexity = trial.complexity or (task.complexity.value if task else None)
    row = _trial_row(trial)
    row["final_answer"] = trial.final_answer
    row["tool_calls"] = [
        {"tool_name": c.tool_name, "arguments": c.arguments, "timestamp": c.timestamp,
         "status": c.status, "error": c.error}
        for c in trial.tool_calls
    ]
    return JSONResponse({
        "campaign_id": campaign_id,
        "task": {
            "task_id": task_id,
            "prompt": task.prompt if task else None,
            "complexity_label": COMPLEXITY_LABELS.get(complexity or "", "inconnue"),
        },
        "trial": row,
        "expected_state_delta": expected,
        "actual_state_delta": dict(trial.actual_state_delta),
        "diff": _diff_rows(expected, trial.actual_state_delta),
        "verdict": {
            "matches_expected": trial.matches_expected,
            "declared_success": trial.declared_success,
            "overconfidence_detected": trial.overconfidence_detected,
            "state": row["state"],
            "label": row["state_label"],
        },
    })


async def get_incidents(request: Request) -> JSONResponse:
    repo = _repo(request)
    campaign_id = _int_param(request, "campaign_id")
    _campaign_or_404(repo, campaign_id)
    incidents = repo.list_incidents(campaign_id)
    rows = [
        {
            "trial_id": i.trial_id,
            "task_id": i.task_id,
            "attack_category": i.attack_category.value,
            "category_label": ATTACK_LABELS[i.attack_category],
            "blocked": i.blocked,
            "payload": i.payload,
            "detected_at": _iso(i.detected_at),
        }
        for i in incidents
    ]
    rows.sort(key=lambda r: (r["blocked"], r["detected_at"]))  # non bloqués en tête
    return JSONResponse({
        "campaign_id": campaign_id,
        "total": len(rows),
        "blocked": sum(r["blocked"] for r in rows),
        "unblocked": sum(not r["blocked"] for r in rows),
        "incidents": rows,
    })


async def get_costs(request: Request) -> JSONResponse:
    repo = _repo(request)
    campaign_id = _int_param(request, "campaign_id")
    _campaign_or_404(repo, campaign_id)
    trials = repo.list_trials(campaign_id)
    by_task: dict[str, list[TrialRecord]] = {}
    for t in trials:
        by_task.setdefault(t.task_id, []).append(t)
    tasks = [
        {
            "task_id": task_id,
            "n": len(group),
            "mean_cost_usd": sum(t.cost_usd for t in group) / len(group),
            "total_cost_usd": sum(t.cost_usd for t in group),
            "mean_latency_ms": sum(t.latency_ms for t in group) / len(group),
            "max_latency_ms": max(t.latency_ms for t in group),
        }
        for task_id, group in by_task.items()
    ]
    ordered = sorted(trials, key=lambda t: (t.recorded_at, t.task_id, t.trial_id))
    return JSONResponse({
        "campaign_id": campaign_id,
        "n_trials": len(trials),
        "total_cost_usd": sum(t.cost_usd for t in trials),
        "mean_cost_usd": (sum(t.cost_usd for t in trials) / len(trials)) if trials else None,
        "mean_latency_ms": (sum(t.latency_ms for t in trials) / len(trials)) if trials else None,
        "by_task": tasks,
        "over_time": [
            {"index": i, "task_id": t.task_id, "trial_id": t.trial_id, "cost_usd": t.cost_usd,
             "latency_ms": t.latency_ms, "recorded_at": _iso(t.recorded_at)}
            for i, t in enumerate(ordered)
        ],
    })


def _proportions_by_task(reports: Iterable[ReliabilityReport]) -> dict[str, dict[str, Any]]:
    return {
        r.task_id: {"p_hat": r.p_hat, "ci_low": r.wilson_ci[0], "ci_high": r.wilson_ci[1],
                    "n": r.n_trials, "successes": r.n_success}
        for r in reports
    }


async def compare_campaigns(request: Request) -> JSONResponse:
    repo = _repo(request)
    try:
        before_id = int(request.query_params["before"])
        after_id = int(request.query_params["after"])
    except (KeyError, ValueError) as exc:
        raise HTTPException(400, "Indiquez deux campagnes : ?before=<id>&after=<id>.") from exc
    before = _campaign_or_404(repo, before_id)
    after = _campaign_or_404(repo, after_id)
    before_p = _proportions_by_task(repo.list_reports(before_id))
    after_p = _proportions_by_task(repo.list_reports(after_id))
    rows = []
    for task_id in sorted(set(before_p) | set(after_p)):
        task = repo.get_task(task_id)
        verdict = _compare_verdict(before_p.get(task_id), after_p.get(task_id))
        b, a = before_p.get(task_id), after_p.get(task_id)
        rows.append({
            "task_id": task_id,
            "complexity": task.complexity.value if task else None,
            "complexity_label": COMPLEXITY_LABELS.get(task.complexity.value, "inconnue") if task else "inconnue",
            "before": b,
            "after": a,
            "delta": (a["p_hat"] - b["p_hat"]) if (a and b) else None,
            "verdict": verdict,
            "verdict_label": COMPARE_LABELS[verdict],
        })
    order = {"regression": 0, "baisse": 1, "incomparable": 2, "stable": 3, "progres": 4}
    rows.sort(key=lambda r: (order[r["verdict"]], r["task_id"]))
    return JSONResponse({
        "before": {"id": before.id, "name": before.name, "started_at": _iso(before.started_at)},
        "after": {"id": after.id, "name": after.name, "started_at": _iso(after.started_at)},
        "n_regressions": sum(r["verdict"] == "regression" for r in rows),
        "tasks": rows,
    })


async def index(request: Request) -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


def create_app(database_url: str | None = None, *, background_runs: bool = True) -> Starlette:
    url = database_url or database_url_from_env()
    app = Starlette(
        routes=[
            Route("/", index),
            Route("/api/campaigns", list_campaigns, methods=["GET"]),
            Route("/api/campaigns", create_campaign, methods=["POST"]),
            Route("/api/compare", compare_campaigns),
            Route("/api/campaigns/{campaign_id}", get_campaign),
            Route("/api/campaigns/{campaign_id}/tasks/{task_id}", get_task),
            Route("/api/campaigns/{campaign_id}/trials/{task_id}/{trial_id}", get_trial),
            Route("/api/campaigns/{campaign_id}/incidents", get_incidents),
            Route("/api/campaigns/{campaign_id}/costs", get_costs),
            Mount("/static", StaticFiles(directory=STATIC_DIR), name="static"),
        ],
        exception_handlers={HTTPException: http_error},
    )
    app.state.database_url = url
    app.state.repository = ReliabilityRepository.from_url(url)
    app.state.launcher = CampaignLauncher(url, background=background_runs)
    return app


def main() -> None:  # pragma: no cover - point d'entrée serveur
    import uvicorn

    host = os.environ.get("AGRICAM_API_HOST", "127.0.0.1")
    port = int(os.environ.get("AGRICAM_API_PORT", "8765"))
    uvicorn.run(create_app(), host=host, port=port)
