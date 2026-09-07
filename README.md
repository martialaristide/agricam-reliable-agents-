# AgriCam Reliable Agents — Code source vérifié

Implémentation de référence du cœur technique du projet **AgriCam Reliable
Agents** : modèles de données, outils MCP (serveur réel, transport stdio),
boucle agent ReAct, harnais de fiabilité (pass^k), Success Verifier,
couche de sécurité, persistance SQL des campagnes et dashboard Streamlit.

## État de vérification

Tout le code de ce dépôt a été **réellement exécuté** (pas seulement
rédigé) au moment de la livraison :

- ✅ 124 tests unitaires et d'intégration, tous passants
- ✅ 99 % de couverture de code (`pytest-cov`)
- ✅ 0 avertissement `ruff` (lint complet)
- ✅ Scripts de démonstration et de campagne exécutés bout en bout
- ✅ Dashboard rendu sans erreur via `streamlit.testing.v1.AppTest`

Seul l'appel réseau de `AnthropicLLMClient` (agent/llm_client.py) n'est pas
exercé par les tests : il nécessite une clé `ANTHROPIC_API_KEY` valide. Le
client est couvert à 100 % par des mocks du SDK `anthropic` (retry, backoff,
normalisation des réponses), et toute la logique de la boucle agent est
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
quand même avoir réussi** : le Success Verifier le rattrape. Résultat
typique (`p-step = 0.9`, 30 essais) :

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

## Dashboard (phase 5)

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
├── reliability/
│   ├── stats.py                # Wilson, pass^k
│   ├── harness.py              # evaluate_task / evaluate_reliability
│   ├── repository.py           # Dépôt persistant des campagnes
│   └── campaign.py             # Harnais + dépôt
├── verifier/                   # Success Verifier (diff d'état)
├── security/                   # Policy, guard, sanitizer (OWASP LLM)
├── persistence/engine.py       # Fabrique de moteur (AGRICAM_DATABASE_URL)
└── dashboard/                  # Requêtes pandas + thème Plotly
dashboard/app.py                # Application Streamlit
scripts/demo_full_pipeline.py   # Démonstration numérique sans API
scripts/run_campaign.py         # Campagne persistée (LLM simulé ou Anthropic)
tests/                          # 124 tests, 99 % de couverture
```

## Licence

MIT.
