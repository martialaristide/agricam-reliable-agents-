"""
Interface de vérification : API JSON (Starlette) + front statique.

- `app.py` : fabrique `create_app(database_url)` et les routes `/api/...`.
- `static/` : l'interface (HTML, CSS, JavaScript, SVG dessinés à la main),
  servie par la même application sur `/`.

Lancement : `PYTHONPATH=src python -m agricam_reliable_agents.api`
(variables : `AGRICAM_DATABASE_URL`, `AGRICAM_API_HOST`, `AGRICAM_API_PORT`).
Le plan de design qui justifie cette interface est dans
`docs/interface/plan-de-design.md`.
"""
