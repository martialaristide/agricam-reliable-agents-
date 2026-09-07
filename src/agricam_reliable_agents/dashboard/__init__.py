"""
Dashboard de fiabilité (phase 5).

- `queries` : lecture du dépôt de campagnes sous forme de DataFrames
  pandas et d'agrégats prêts à afficher (testables sans Streamlit) ;
- `theme` : palette et gabarit Plotly (clair/sombre) ;
- l'application Streamlit elle-même vit dans `dashboard/app.py` à la
  racine du dépôt : `PYTHONPATH=src streamlit run dashboard/app.py`.

pandas et plotly ne sont importés que par ce paquet : le cœur du projet
(harnais, vérificateur, sécurité) n'en dépend pas.
"""
