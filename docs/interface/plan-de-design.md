# Interface de vérification AgriCam Reliable Agents — Passe 1 : plan de design

Ce document est la réponse au brief « Interface de vérification ». Il précède
le code (Passe 2, `src/agricam_reliable_agents/api/` et son dossier `static/`)
et reste la référence quand une décision d'interface est discutée.

## Choix technique, dit clairement

**Streamlit est écarté.** Le brief l'anticipe : le thème Streamlit *est*
l'esthétique générique à éviter (cartes arrondies, ombre douce, sidebar
grise, widgets uniformes). On peut le maquiller par CSS, mais on se bat
alors contre chaque composant, on ne contrôle ni la typographie des
chiffres, ni la géométrie des marques, ni le focus clavier, et le résultat
est un compromis silencieux. Le dashboard Streamlit existant
(`dashboard/app.py`) est conservé comme vue d'exploration rapide pour
développeurs ; **l'interface de décision est une application web sur
mesure** : une API JSON (Starlette, déjà dans les dépendances via le SDK
MCP) au-dessus du schéma SQL existant, et un front en HTML/CSS/JavaScript
sans framework ni étape de build, avec des graphiques en SVG écrits à la
main. Aucune bibliothèque de graphiques : leur style par défaut est
précisément le « chrome » qu'on veut éviter.

Le schéma consommé est celui du projet, tel quel : `campaigns`, `tasks`,
`trials` (chaque essai porte déjà son verdict de vérification :
`matches_expected`, `overconfidence_detected`, `actual_state_delta`),
`reports`, `security_incidents`. Le brief cite une table `verifications` :
dans ce projet elle est fusionnée dans `trials`, ce que l'API expose sans
rien inventer.

## Parti pris : la carte de contrôle

Le sujet de l'outil est le contrôle statistique d'un processus (un agent)
: proportion estimée, intervalle de confiance, dérive entre ce qui est
déclaré et ce qui est mesuré. C'est exactement l'objet des **cartes de
contrôle de Shewhart** : une séquence d'observations, une valeur centrale,
des limites, et une règle simple pour dire « le processus est sous
contrôle » ou non. L'interface adopte ce vocabulaire, pas par nostalgie
industrielle, mais parce qu'il répond à la question que se pose
l'utilisateur : *« puis-je déployer ? »* se lit sur une carte de contrôle
comme *« le point est-il dans les limites ? »*.

Un seul geste audacieux : **chaque proportion est dessinée sur une règle
graduée avec son intervalle, et chaque série d'essais est une carte de
contrôle**. Tout le reste (typographie, filets, navigation) est au service
de la lisibilité de ces deux figures.

## Tokens

### Couleur (6 valeurs)

| Nom | Hex | Rôle | Pourquoi |
|---|---|---|---|
| `--papier` | `#E4E9E3` | fond de page | le vert-gris pâle du papier millimétré des cartes de contrôle imprimées ; froid, ni crème ni gris d'écran |
| `--cadran` | `#F7F9F6` | surface des instruments (règles, cartes, tableaux) | le blanc légèrement vert d'un cadran, distinct du papier sans ombre |
| `--encre` | `#14231C` | texte, axes, marques neutres | une encre vert-noir assumée (pas un noir teinté qui hésite) ; cohérente avec l'agriculture sans l'illustrer |
| `--gravure` | `#55635A` | libellés d'échelle, texte secondaire, grille | l'encre diluée ; contraste 4,9:1 sur le papier |
| `--verifie` | `#1F6F4A` | succès **vérifié** par l'état réel | le vert des cartes de contrôle « sous contrôle » ; réservé à ce seul sens |
| `--alerte` | `#B3261E` | sur-confiance, incident non bloqué, régression | le vermillon des points « hors limites » ; réservé aux situations où l'utilisateur doit refuser de déployer |

Pas de couleur pour l'échec honnête (l'agent a échoué et l'a dit) : il est
dessiné en encre, forme vide. Pas de bleu d'interface, pas de dégradé.

### Typographie (deux familles, rôles disjoints)

- **Barlow Condensed** (600) : libellés gravés des instruments : titres
  d'écran, noms d'échelles, en-têtes de tableaux, graduations. Condensée
  comme les inscriptions d'un cadran, jamais en capitales espacées.
- **IBM Plex Sans** (400/600) : texte courant, chiffres, verdicts. Les
  chiffres en colonnes (tableaux, graduations) utilisent
  `font-variant-numeric: tabular-nums` ; les chiffres en phrase restent
  proportionnels. Pas de monospace.

Repli système si les polices ne se chargent pas (usage hors ligne) :
`"Arial Narrow", sans-serif` et `system-ui, sans-serif`.

### Layout

**Alignement à gauche partout**, y compris les chiffres dans le texte ;
seuls les nombres en colonne sont alignés à droite. Une **règle
verticale** fixe à gauche (48 px sur mobile, 224 px au-delà) porte la
navigation, comme le sélecteur de canaux d'un instrument. Le contenu est
posé sur une grille de 8 px ; les zones sont séparées par des **filets**
d'encre (1 px) et des filets de gravure, jamais par des cartes ombrées ni
des coins arrondis. Le papier millimétré (grille fine en CSS) n'apparaît
**que sous les figures**, là où il sert à lire une valeur.

#### 1. Campagnes

```
┌────────┬──────────────────────────────────────────────────────────────┐
│ RÈGLE  │ Campagnes                                  [Lancer une campagne]│
│        │ ────────────────────────────────────────────────────────────── │
│ Camp.  │ n°  Nom                 Date       Tâches  Essais  p̂ (IC)  Sur-conf. Sécurité  État │
│ Tâches │ 2   simulated-p0.97     07/09 02:45   3     90   ▐━━━┿━━▌ 0,88  2      0 non bloqué  terminée│
│ Sécur. │ 1   simulated-p0.9      07/09 02:36   3     90   ▐━┿━━━━▌ 0,73  24     0 non bloqué  terminée│
│ Coût   │ ────────────────────────────────────────────────────────────── │
│ Comp.  │ (état vide) Aucune campagne. Lancez-en une : 30 essais simulés │
│        │ prennent moins d'une minute.               [Lancer une campagne]│
└────────┴──────────────────────────────────────────────────────────────┘
```
Une phrase : *un registre de campagnes où la fiabilité se lit déjà comme
une règle graduée, avant même d'ouvrir la campagne.*

#### 2. Tâche (le cœur)

```
┌────────┬──────────────────────────────────────────────────────────────┐
│        │ Campagne 1  ›  T2-treat-notify            complexité 3-5 étapes│
│        │ « Consulte les capteurs de P-003, applique … »                 │
│        │ ────────────────────────────────────────────────────────────── │
│        │ Proportion de succès vérifiés                 22 / 30 essais   │
│        │ 0%        25%        50%        75%       100%                  │
│        │ ├─────────┼──────────┼──────────┼──────────┤                    │
│        │                   ▐━━━━━━┿━━━━━━━▌  p̂ 0,73  IC 95 % [0,56 ; 0,86]│
│        │ ────────────────────────────────────────────────────────────── │
│        │ Fiabilité répétée pass^k        seuil de décision [90 %]        │
│        │ 100% ┤●                                                          │
│        │  90% ┼───●─── - - - - - - - - - - - - - - - - - - seuil          │
│        │      │  ░░░●░░░░░░░░░░░░░░░░░░░░░░░░ zone hors contrôle          │
│        │   0% ┴─1──2──3──4──5──6──7──8──9──10  k                           │
│        │ ────────────────────────────────────────────────────────────── │
│        │ Carte de contrôle des essais      ● vérifié  ○ échec avoué  ■ sur-confiance │
│        │ ● ● ● ○ ■ ● ● ● ■ ● ● ● ● ○ ● ■ ● ● ● ● ● ■ ● ● ● ● ■ ● ● ●   │
│        │ n°  vérifié  déclaré  verdict          outils  latence  coût  ›  │
└────────┴──────────────────────────────────────────────────────────────┘
```
Une phrase : *trois instruments empilés (règle de Wilson, courbe pass^k
avec sa limite, carte de contrôle des essais) qui répondent dans l'ordre à
« quelle fiabilité ? », « tient-elle dans la durée ? », « où ça casse ? ».*

#### 3. Essai (drill-down)

```
│ Campagne 1 › T2-treat-notify › essai n° 5                                 │
│ Verdict : SUR-CONFIANCE — l'agent a déclaré un succès, l'état réel dit non│
│ ─────────────────────────────────────────────────────────────────────────│
│ Appels d'outils (2)                 │ État attendu        │ État observé  │
│ 1  get_sensor_data  {parcel P-003}  │ diagnostic.D-42     │               │
│ 2  recommend_treatment {D-404} ✕    │  status = treated   │ (inchangé)  ✕ │
│    Erreur outil : diagnostic inconnu│ product.PRD-7       │               │
│                                     │  stock_qty = 24     │ (inchangé)  ✕ │
│ Réponse finale de l'agent           │ farmer.F-001        │               │
│ « Traitement appliqué et … »        │  notified_count = 1 │ (inchangé)  ✕ │
```
Une phrase : *la preuve en deux colonnes, attendu contre observé, ligne à
ligne, avec la séquence d'outils qui explique l'écart.*

#### 4. Sécurité

```
│ Incidents de sécurité — campagne 1              6 incidents, 6 bloqués     │
│ ■ NON BLOQUÉS (0) — rien à signaler                                         │
│ ─────────────────────────────────────────────────────────────────────────  │
│ Bloqués (6)                                                                │
│ LLM08 Excessive Agency   essai 4   {'farmer_id': 'F-404', …}   bloqué      │
```
Une phrase : *les incidents non bloqués passent en tête, en alerte ; les
bloqués sont un journal.*

#### 5. Coût et latence

```
│ Coût et latence — campagne 1                                               │
│ Par tâche         coût moyen   latence moyenne                             │
│ T1-treat          ▮ 0,004 $     ▮▮ 3 ms                                    │
│ T3-full-workflow  ▮▮▮▮ 0,021 $  ▮▮▮▮▮ 11 ms                                │
│ Dans le temps (90 essais)    ─╲_╱╲__╱╲╱───╲_╱  latence ms                   │
```

#### 6. Comparaison

```
│ Comparer   avant [1 simulated-p0.9 ▾]   après [2 simulated-p0.97 ▾]        │
│ Tâche              avant ───────────● après       verdict                  │
│ T3-full-workflow   0,50 ●━━━━━━━━━━━━━━━━━━► 0,80   progrès                │
│ T1-treat           0,97 ●━► 1,00                    stable                 │
│ (régressions en tête, en alerte, avec l'IC qui justifie le mot)           │
```

### Principes (ce qui rend l'interface reconnaissable)

1. **Aucun chiffre nu.** Une proportion est toujours dessinée sur une
   règle avec son intervalle et son effectif ; une moyenne est toujours
   suivie de son n. Si l'incertitude ne tient pas à l'écran, on retire le
   chiffre, pas l'intervalle.
2. **La forme porte l'état, la couleur le confirme.** Disque plein vert =
   vérifié, disque vide encre = échec avoué, carré rouge = sur-confiance.
   Sans couleur (impression, daltonisme), tout reste lisible.
3. **Des filets, pas des cartes.** L'écran est une feuille d'instruments
   séparés par des traits ; rien ne flotte, rien n'a d'ombre.
4. **Le seuil est dessiné.** La limite de décision (pass^k ≥ 90 % par
   défaut, réglable) est une ligne sur la courbe, et ce qui est en dessous
   est hachuré : la décision se voit avant de se lire.

## Auto-critique contre le brief

Ce que le premier jet contenait et qui a été corrigé :

- **Rangée de KPI en cartes** en tête de campagne : c'est le réflexe SaaS.
  Remplacée par un **bandeau d'instrument** : une seule ligne, chiffres à
  gauche, chacun suivi de son intervalle ou de son n, séparés par des
  filets verticaux, pas de cartes.
- **Libellés en capitales espacées** au-dessus des sections et **points
  médians** entre métadonnées : supprimés. Les sections ont un titre en
  Barlow Condensed bas-de-casse ; les métadonnées sont des phrases.
- **Monospace pour les chiffres** : refusé. Les chiffres tabulaires de
  Plex Sans alignent les colonnes sans tirer l'interface vers le
  « terminal ».
- **Noir #0B0B0B** : remplacé par une encre vert-noir choisie (`#14231C`).
- **Fond crème + terracotta** : le fond est un vert-gris froid et l'accent
  est un vermillon réservé à l'alerte, jamais décoratif.
- **Sidebar de navigation gris clair** : c'est devenu une règle verticale
  en encre, avec le nom de la campagne courante gravé en tête ; les
  écrans sont des « canaux ».
- **Survol animé sur chaque ligne / fondu en cascade** : aucun. Les seules
  transitions sont celles déclenchées par un clic, et
  `prefers-reduced-motion` les supprime.
- **Accessoire retiré avant de sortir** : le papier millimétré couvrait
  toute la page dans le premier jet. Il ne reste que sous les figures, où
  la grille aide à lire une valeur ; ailleurs il n'était que décor.

## Contenu écrit

Voix active, point de vue de l'utilisateur. Les états sont nommés comme
il les comprend : « vérifié », « échec avoué », « sur-confiance »,
« bloqué », « non bloqué », « régression ». L'état vide de la liste des
campagnes dit ce qu'il faut faire et combien de temps ça prend. Les
erreurs de l'API sont des phrases : « Cette campagne n'existe pas. »

## Accessibilité

- Focus clavier visible : anneau de 2 px en encre sur fond cadran.
- `prefers-reduced-motion` : aucune transition.
- Contrastes : encre/papier 13:1, gravure/papier 4,9:1, vérifié/cadran
  5,6:1, alerte/cadran 5,9:1 ; les zones de décision (verdicts) sont en
  encre sur cadran, et jamais en couleur seule.
- Chaque figure SVG a un `role="img"` et un `<title>` ; les mêmes données
  existent en tableau HTML sous la figure.
- Responsive : sous 720 px la règle se réduit à des icônes-lettres, les
  tableaux défilent horizontalement dans leur conteneur, les figures se
  redimensionnent (SVG en `viewBox`).
