# AgriCam Reliable Agents — Code source vérifié

Implémentation de référence du cœur technique du projet **AgriCam Reliable
Agents** : modèles de données, outils MCP (serveur réel, transport stdio),
boucle agent ReAct, harnais de fiabilité (pass^k), Success Verifier,
couche de sécurité, persistance SQL des campagnes, dashboard Streamlit,
interface de vérification sur mesure, et **Connecteur d'Agent Générique**
(brancher n'importe quel agent — AgriCam ou tiers — sur ce harnais).

## État de vérification

Tout le code de ce dépôt a été **réellement exécuté** (pas seulement
rédigé) au moment de la livraison :

- ✅ 356 tests unitaires et d'intégration, tous passants
- ✅ 99 % de couverture de code (`pytest-cov`), chaque module du
  connecteur individuellement ≥ 98 %
- ✅ 0 avertissement `ruff` (lint complet)
- ✅ Scripts de démonstration et de campagne exécutés bout en bout
- ✅ Dashboard rendu sans erreur via `streamlit.testing.v1.AppTest`
- ✅ Interface de vérification : API testée par `TestClient`, écrans rendus
  en Chrome headless et inspectés
- ✅ Audit expert indépendant (7 septembre 2026) : 2 défauts bloquants,
  9 importants et 11 mineurs relevés, tous corrigés et verrouillés par
  `tests/test_audit_fixes.py`
- ✅ Connecteur d'Agent Générique (12 tâches, Phases A à E) : chaque
  garantie de sécurité (validation d'oracle, mode dry-run) verrouillée
  par un test qui tente explicitement de la contourner par appel direct
  de fonction Python

Seul l'appel réseau de `AnthropicLLMClient` (agent/llm_client.py) n'est pas
exercé par les tests : il nécessite une clé `ANTHROPIC_API_KEY` valide. Le
client est couvert à 100 % par des mocks du SDK `anthropic` : retry et
backoff, normalisation de **tous** les blocs `tool_use` d'une réponse
(appel d'outils parallèle, actif par défaut), rejeu du contenu brut du
tour assistant (blocs `thinking` du raisonnement adaptatif d'Opus 5
inclus), troncature `max_tokens`. Toute la logique de la boucle agent est
testée avec des clients LLM factices (`tests/test_agent_loop.py`).

## Installation

```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows : .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # puis renseigner les valeurs
```

## Lancer les tests

```bash
PYTHONPATH=src pytest tests/ --cov=agricam_reliable_agents --cov-report=term-missing
```

## Démonstration numérique (sans clé API, sans base)

```bash
PYTHONPATH=src python3 scripts/demo_full_pipeline.py
```

Fait tourner un agent simulé « à plat » (taux de succès 0.95 / 0.90 / 0.80)
dans le harnais et affiche p̂, l'intervalle de Wilson et pass^k(1, 3, 5, 10) :
l'effondrement de la fiabilité avec la répétition, en chiffres.

## Campagne de fiabilité persistée (sans clé API par défaut)

```bash
PYTHONPATH=src python scripts/run_campaign.py --n-trials 30 --p-step 0.9
```

Contrairement à la démonstration, ce script exécute la **vraie boucle
agent** (`run_agent`) avec les **vrais outils MCP** sur un store SQL, sous
la garde de sécurité, sur trois tâches de complexité croissante (1, 3 et
6 étapes). Un LLM simulé suit le plan de chaque tâche et, à chaque étape,
se trompe d'identifiant avec probabilité `1 − p-step`, puis **déclare
quand même avoir réussi** : le Success Verifier le rattrape. Ce qui est
jugé est l'état réel, pas le respect du plan : une dérive sur la lecture
finale de la tâche à 6 étapes laisse l'état conforme, donc le taux attendu
de cette tâche est `p-step^5`. Résultat typique (`p-step = 0.9`, 30 essais) :

```
Tâche              Complexité   n succès     p̂   IC Wilson 95 %  pass^1  pass^3  pass^5  pass^10 sur-conf.
T1-treat           1-2_steps   30     29  0.967   [0.833, 0.994]   0.967   0.903   0.844    0.712      1/30
T2-treat-notify    3-5_steps   30     22  0.733   [0.556, 0.858]   0.733   0.394   0.212    0.045      8/30
T3-full-workflow   6+_steps    30     15  0.500   [0.332, 0.668]   0.500   0.125   0.031    0.001     15/30

Incidents de sécurité journalisés : 6 (bloqués : 6)
```

Chaque essai, chaque rapport et chaque incident (ex. notification hors
périmètre bloquée par `ActionPolicy`, OWASP LLM08) est archivé dans la base
`--database-url` (défaut : `AGRICAM_DATABASE_URL`, sinon `sqlite:///agricam.db`).
Le store métier AgriCam est réinitialisé avant chaque essai et vit par
défaut en mémoire (`--store-url sqlite://`).

Avec une clé API, `--llm anthropic` remplace le LLM simulé par
`AnthropicLLMClient` (modèle `--model` ou `AGRICAM_MODEL`). Chaque essai
consomme de vrais appels API.

## Interface de vérification (application web sur mesure)

```bash
PYTHONPATH=src python -m agricam_reliable_agents.api
# puis ouvrir http://127.0.0.1:8765/
```

L'écran que l'on regarde pour **décider si l'agent peut être déployé**.
Six écrans, dessinés comme des instruments de contrôle statistique
(plan de design et auto-critique : `docs/interface/plan-de-design.md`) :

1. **Campagnes** : registre des campagnes, fiabilité déjà lisible sur une
   règle graduée, lancement d'une campagne simulée depuis l'interface
   (exécutée sur le serveur, suivie « en cours » puis « terminée »).
2. **Tâche** : règle de Wilson (p̂ et son intervalle), courbe pass^k avec
   seuil de décision réglable, bande d'incertitude et zone hors contrôle
   hachurée, carte de contrôle des essais (● vérifié, ○ échec avoué,
   ■ sur-confiance), tableau des essais.
3. **Essai** : verdict du Success Verifier, appels d'outils avec leur
   statut (exécuté, refusé par la garde, rejeté par l'outil), état attendu
   contre état observé ligne à ligne.
4. **Sécurité** : incidents par catégorie OWASP LLM, non bloqués en tête.
5. **Coût et latence** : par tâche et dans le temps.
6. **Comparer** : deux campagnes côte à côte, régressions en tête (la
   borne haute de l'intervalle « après » passe sous le p̂ « avant »).

Stack : API JSON Starlette (`src/agricam_reliable_agents/api/app.py`, huit
routes sur le schéma SQL existant, testées dans `tests/test_api.py`) et
front HTML/CSS/JavaScript sans framework ni build, graphiques en SVG
dessinés à la main (`api/static/`). Streamlit a été écarté pour cette
interface : son thème est l'esthétique générique que le brief interdit.

## Dashboard Streamlit (exploration rapide)

```bash
PYTHONPATH=src streamlit run dashboard/app.py
```

Lit la base des campagnes (`AGRICAM_DATABASE_URL`, modifiable dans la barre
latérale) et présente, par campagne :

- une ligne de KPI : essais vérifiés, p̂ global, taux de sur-confiance,
  incidents bloqués, coût estimé ;
- la fiabilité par tâche (p̂ avec IC de Wilson), triée par complexité ;
- les courbes pass^k = p̂^k par tâche (k réglable) ;
- la comparaison de p̂ entre campagnes (régression ou progrès entre deux
  versions d'agent) ;
- le tableau des essais (filtre « sur-confiance uniquement ») et des
  incidents de sécurité.

La logique de requêtes (`agricam_reliable_agents/dashboard/queries.py`)
est indépendante de Streamlit et testée par pytest ; la palette
(`dashboard/theme.py`) est validée pour le daltonisme et le contraste en
modes clair et sombre.

## Persistance (SQLite / PostgreSQL)

Deux schémas indépendants, construits sur SQLAlchemy 2.x et configurés par
`AGRICAM_DATABASE_URL` (`persistence/engine.py`) :

- **Données métier AgriCam** (`mcp_tools/sql_store.py`, `SqlAlchemyDataStore`) :
  capteurs, diagnostics, produits, exploitants, notifications. Même
  contrat que le store en mémoire (`DataStore`, protocole défini dans
  `mcp_tools/data_store.py`) : les outils MCP et la boucle agent
  fonctionnent sans modification sur l'un ou l'autre. Le décrément de
  stock est un `UPDATE` conditionnel atomique.
- **Résultats de campagne** (`reliability/repository.py`,
  `ReliabilityRepository`) : campagnes, tâches, essais, rapports,
  incidents. Fournit les callbacks à brancher sur le harnais
  (`trial_observer`) et sur la garde (`incident_sink`), et relit tout sous
  forme de dataclasses immuables.

`reliability/campaign.py` (`run_persisted_campaign`) relie les deux :
harnais + dépôt, avec un hook `before_trial` pour remettre le store dans
un état connu avant chaque essai.

Pour PostgreSQL, installer un pilote (ex. `pip install "psycopg[binary]"`)
et utiliser `postgresql+psycopg://user:password@host:5432/agricam`.

## Serveur MCP (protocole réel, transport stdio)

Les 5 outils AgriCam sont exposés via un serveur conforme au SDK officiel
`mcp` (`src/agricam_reliable_agents/mcp_tools/server.py`) :

```bash
PYTHONPATH=src python -m agricam_reliable_agents.mcp_tools.server
```

Sans `AGRICAM_DATABASE_URL`, le serveur démarre sur un store en mémoire
pré-rempli. Avec la variable, il utilise le store SQL (semé une seule fois
si la base est vide) et les actions de l'agent persistent entre deux
sessions.

### Connexion à Claude Desktop

Ajouter dans `claude_desktop_config.json` (menu *Settings → Developer →
Edit Config*) :

```json
{
  "mcpServers": {
    "agricam": {
      "command": "/chemin/vers/.venv/bin/python",
      "args": ["-m", "agricam_reliable_agents.mcp_tools.server"],
      "env": {
        "PYTHONPATH": "/chemin/vers/agricam/src",
        "AGRICAM_DATABASE_URL": "sqlite:////chemin/vers/agricam.db"
      }
    }
  }
}
```

Sous Windows, utiliser `C:\\chemin\\vers\\.venv\\Scripts\\python.exe`.
Redémarrer Claude Desktop : les outils `get_sensor_data`,
`get_diagnostic_history`, `recommend_treatment`, `check_marketplace_stock`
et `notify_farmer` apparaissent dans le sélecteur d'outils.

### Connexion via l'API Claude (connecteur MCP)

Le connecteur MCP de l'API Messages n'accepte que des serveurs HTTP
distants. Pour exposer ce serveur au-delà de la machine locale, le
brancher derrière le transport `streamable_http` du SDK (voir
`Server.streamable_http_app()`) et le déclarer côté API avec
`mcp_servers=[{"type": "url", "url": ..., "name": "agricam"}]` et
`tools=[{"type": "mcp_toolset", "mcp_server_name": "agricam"}]`.

### Test automatisé

`tests/test_mcp_server.py` vérifie `list_tools` (5 outils) et des appels
réels en mémoire et en sous-processus stdio ; `tests/test_mcp_server_env.py`
vérifie la sélection du store par `AGRICAM_DATABASE_URL`, y compris en
sous-processus avec persistance dans un fichier SQLite.

## Connecter un agent tiers

Le harnais de fiabilité (`reliability/harness.py`) était déjà générique :
il ne dépend que de deux contrats abstraits (`AgentRunner`,
`StateSnapshotFn`). Le **Connecteur d'Agent Générique**
(`src/agricam_reliable_agents/connector/`) fournit l'outillage qui
manquait pour lui brancher n'importe quel agent — AgriCam ou tiers,
déployé ou non — sans modifier le harnais, le Success Verifier, les
statistiques ni la garde de sécurité.

Quatre adaptateurs (`AgentConnector`, contrat `send(prompt, trial_id) ->
RawAgentReply`) : appel direct d'un modèle (`direct_model_adapter.py`),
REST/HTTP (`rest_adapter.py`), client MCP (`mcp_client_adapter.py`),
sous-processus isolé (`cli_adapter.py`, `shell=False` systématique,
timeout obligatoire). Format exact attendu par chacun, oracle par
approbation de diff, environnements et mode dry-run :
`src/agricam_reliable_agents/connector/README.md`.

Exemple minimal, exécuté tel quel (aucune clé API requise) :

```python
from agricam_reliable_agents.agent.llm_client import LLMResponse
from agricam_reliable_agents.connector.bridge import agent_connector_as_runner
from agricam_reliable_agents.connector.direct_model_adapter import DirectModelAdapter
from agricam_reliable_agents.models.data_models import Task, TaskComplexity
from agricam_reliable_agents.reliability.harness import HarnessConfig, evaluate_task


class MonAgentTiersFactice:
    """Un agent tiers imaginaire : répond toujours la même chose."""

    def generate(self, messages: list[dict], tool_schemas: list[dict]) -> LLMResponse:
        return LLMResponse(tool_call=None, final_text="Bonjour, tout va bien.")


tache = Task(
    id="T-DEMO", prompt="Dis bonjour.", complexity=TaskComplexity.LEVEL_1,
    category="demo", expected_state_delta={}, verification_query="q-demo",
)

connecteur = DirectModelAdapter(MonAgentTiersFactice(), system_prompt="Tu es poli.")
agent_runner = agent_connector_as_runner(connecteur)

rapport = evaluate_task(
    tache, agent_runner, state_snapshot_fn=lambda t: {},
    config=HarnessConfig(n_trials=5, k_values=(1, 3)),
)
print(f"p_hat = {rapport.p_hat:.3f}, pass^3 = {rapport.pass_k[3]:.3f}")
```

Pour un agent qui affecte un vrai état (pas seulement une réponse
texte), définir l'oracle par **approbation de diff** plutôt que d'écrire
`expected_state_delta` de mémoire — voir `connector/README.md`, section
« Définir un oracle ».

### Fondations et garanties (corrections de la Tâche 0)

Avant d'ajouter `agent_connections`/`task_oracles`, trois fondations SQL
ont été corrigées (`persistence/engine.py`, `reliability/repository.py`) :
contrainte d'unicité sur les essais, `PRAGMA foreign_keys=ON` réellement
appliqué sur SQLite (les clés étrangères existantes étaient jusque-là
purement décoratives), `cost_usd` en `Numeric(12, 6)` pour une agrégation
de coûts sans dérive flottante.

Deux garanties vérifiées par des tests qui tentent explicitement de les
contourner par appel direct de fonction Python (jamais seulement via une
interface) :

- **Aucune campagne sans oracle validé** : `connector/orchestration.py`
  rejette toute tâche dont `task_oracles.validated_by_human` n'est pas
  vrai, y compris si on appelle `run_gated_campaign` directement.
- **Aucun effet réel en mode dry-run** : `connector/environment.py`
  neutralise les outils à effet de bord tout en laissant l'agent croire
  à un succès — vérifié par une assertion d'état avant/après, pas
  seulement par l'absence d'exception.

L'audit de l'oracle par mutation ciblée (`connector/oracle_mutation.py`)
prouve, lui, qu'un oracle approuvé DÉTECTE réellement une régression
injectée à la frontière des outils — pas seulement qu'il « tourne sans
erreur » : un oracle délibérément incomplet laisse un mutant survivre
dans les tests, la preuve que l'audit fonctionne pour de vrai.

## Corrections issues de l'audit expert

Un audit indépendant du code (7 septembre 2026) a relevé et fait corriger :

- **bloquants (mode LLM réel)** : un seul `tool_use` retenu par réponse et
  blocs `thinking` supprimés de l'historique, qui provoquaient des 400 dès
  qu'Opus 5 appelait plusieurs outils ou raisonnait (`raw_content` rejoué
  tel quel, tous les `tool_result` dans un seul message) ;
- **importants** : `TypeError`/`ValueError` d'un appel d'outil mal formé
  qui faisait tomber la campagne ; `stop_reason == "max_tokens"` archivé
  comme échec d'agent ; détection de succès trop étroite (« marqué comme
  traité et notifié » non reconnu) ; périmètre de parcelle non appliqué à
  `recommend_treatment` ; contrats `seed_demo_data`/`reset` divergents
  entre les stores ; `is_empty` partiel ; définition de tâche réécrite
  entre campagnes (désormais figée dans chaque essai) ; appels refusés ou
  en erreur absents de l'audit (désormais archivés avec leur statut) ;
  incidents sans identifiant de tâche ;
- **mineurs** : détection des URL SQLite mémoire, `limit <= 0` et quantité
  négative, second traitement qui consommait le stock, pass^k sans
  intervalle (désormais l'image de l'intervalle de Wilson), tarif du
  modèle, charge d'incident en repr Python, longueur du message de
  notification, robustesse du dashboard.

Les bases SQLite créées avant ces corrections n'ont pas les nouvelles
colonnes (`trials.complexity`, `trials.expected_state_delta`,
`security_incidents.task_id`) : supprimez le fichier `.db` et relancez une
campagne.

## Structure

```
src/agricam_reliable_agents/
├── models/data_models.py       # Contrats de données (dataclasses)
├── mcp_tools/
│   ├── data_store.py           # Store en mémoire + protocole DataStore
│   ├── sql_store.py            # Store persistant SQLAlchemy
│   ├── tools.py                # Les 5 outils MCP (schémas + implémentations)
│   └── server.py               # Serveur MCP stdio (SDK officiel)
├── agent/                      # Boucle ReAct + client LLM découplé
├── api/                        # Interface de vérification (Starlette + static/)
├── reliability/
│   ├── stats.py                # Wilson, pass^k
│   ├── harness.py              # evaluate_task / evaluate_reliability
│   ├── repository.py           # Dépôt persistant des campagnes
│   ├── campaign.py             # Harnais + dépôt
│   └── simulation.py           # Campagne simulée bout en bout (CLI et API)
├── verifier/                   # Success Verifier (diff d'état)
├── security/                   # Policy, guard, sanitizer (OWASP LLM)
├── persistence/engine.py       # Fabrique de moteur (AGRICAM_DATABASE_URL)
├── connector/                  # Connecteur d'Agent Générique (voir son README)
│   ├── base.py, bridge.py      # AgentConnector, pont vers AgentRunner
│   ├── direct_model_adapter.py, rest_adapter.py,
│   │   mcp_client_adapter.py, cli_adapter.py  # Les 4 adaptateurs
│   ├── oracle_repository.py    # agent_connections, task_oracles
│   ├── oracle_approval.py      # Oracle par approbation de diff
│   ├── oracle_mutation.py      # Audit de l'oracle par mutation ciblée
│   ├── orchestration.py        # Garde validated_by_human
│   ├── environment.py          # Environnements + mode dry-run
│   └── contract_inference.py   # Inférence de contrat (mode assisté)
└── dashboard/                  # Requêtes pandas + thème Plotly
dashboard/app.py                # Application Streamlit
scripts/demo_full_pipeline.py   # Démonstration numérique sans API
scripts/run_campaign.py         # Campagne persistée (LLM simulé ou Anthropic)
docs/interface/plan-de-design.md # Passe 1 du brief interface (tokens, wireframes, auto-critique)
tests/                          # 356 tests, 99 % de couverture
```

## Licence

MIT.
