# Assistant de pré-remplissage BLS Espagne (Algérie)

Automatise les parties répétitives (connexion, surveillance des créneaux) sans
jamais réserver à ta place. **Le clic final « Réserver » reste toujours manuel.**

## Sommaire

- [Installation](#installation)
- [Configuration](#configuration)
- [Lancement](#lancement)
- [Tableau de bord local (viewer)](#tableau-de-bord-local-viewer)
- [Les quatre garde-fous](#les-quatre-garde-fous)
  - [1. Scan du mois suivant](#1-scan-du-mois-suivant)
  - [2. Garde-fou « session expirée »](#2-garde-fou--session-expirée-)
  - [3. Auto-relance de Chrome](#3-auto-relance-de-chrome)
  - [4. Compte à rebours + self-check](#4-compte-à-rebours--self-check)
- [Rafraîchissement et calendrier SPA](#rafraîchissement-et-calendrier-spa)
- [Tous les réglages](#tous-les-réglages)
- [Premier essai : fais-le calmement](#premier-essai-fais-le-calmement-pas-dans-lurgence)
- [Limites importantes](#limites-importantes)
- [Dépannage](#dépannage)

## Installation

```bash
pip install -r requirements.txt
```

Il te faut aussi Google Chrome installé sur ton ordinateur.

Le script démarre par défaut via **undetected-chromedriver** (mode discret :
pas de drapeau `navigator.webdriver`, pas de variables `$cdc_...`, pas de
bannière « Chrome est contrôlé par un logiciel de test automatisé »), et
saisit les identifiants avec une cadence de frappe humaine. Si le paquet
n'est pas installé, il bascule automatiquement sur Selenium classique avec
les options anti-détection de base — moins discret.

Au premier lancement, un dossier `chrome_profile/` est créé à côté du
script : il conserve tes cookies et ta session entre deux exécutions, ce
qui limite les challenges anti-bot récurrents. Ne le supprime pas entre
deux sessions de surveillance. Il n'est jamais commité (exclu via
`.gitignore`).

Le tableau de bord (`bls_viewer.py`) n'a **aucune dépendance** : il n'utilise
que la bibliothèque standard de Python.

## Configuration

1. Copie `.env.example` en `.env` :
   ```bash
   cp .env.example .env
   ```
2. Ouvre `.env` et remplis ton email et ton mot de passe BLS.
3. Ajuste `TARGET_HOUR` / `TARGET_MINUTE` selon le jour où les créneaux
   s'ouvrent (mardi 16h55 ou vendredi 15h55, par exemple).

Toutes les autres variables sont optionnelles : les valeurs par défaut
conviennent pour un usage normal. Elles sont décrites dans
[`.env.example`](.env.example) et récapitulées dans
[Tous les réglages](#tous-les-réglages). Une valeur invalide n'arrête jamais
le script : elle est signalée au démarrage et remplacée par la valeur par
défaut (ou bornée, par exemple un `REFRESH_INTERVAL_SECONDS` trop agressif
est ramené au plancher de 2 s).

**Ne partage jamais ton fichier `.env` et ne le committe jamais** (il est
exclu de git via `.gitignore`). Si cela t'arrive un jour, change
immédiatement ton mot de passe BLS.

## Lancement

```bash
python bls_espagne_prefill.py
```

Le script va :

1. Démarrer le **tableau de bord local** (http://127.0.0.1:8765) et ouvrir
   Chrome.
2. **Attendre l'heure cible** en affichant un compte à rebours (toutes les
   5 min, puis chaque minute, puis toutes les 10 s). Si tu le lances après
   l'heure cible, il démarre la surveillance immédiatement.
3. Faire un **self-check à T-30 min** (session, calendrier, navigateur) et un
   **contrôle léger à T-2 min**.
4. Ouvrir la page de connexion et essayer de remplir automatiquement
   email/mot de passe.
5. Te laisser gérer le CAPTCHA/OTP toi-même si demandé.
6. Naviguer automatiquement jusqu'à l'écran des créneaux (URL mémorisée au
   lancement précédent, ou clic sur le lien « prendre rendez-vous ») ; à
   défaut, te laisser naviguer manuellement — l'URL n'est mémorisée que si le
   calendrier est **réellement détecté**.
7. **Calibrer le rafraîchissement** (rechargement complet ou mode SPA sans
   rechargement), puis rafraîchir automatiquement cette page et, **dès qu'un
   créneau apparaît** (mois affiché **ou mois suivant**) : vérifier
   immédiatement (au rendu du calendrier, sans attendre un délai fixe),
   **présélectionner le premier jour disponible**, remettre la fenêtre Chrome
   au premier plan et déclencher une **alerte sonore**.
8. S'arrêter là — il ne te reste que deux clics : choisir le créneau horaire
   puis « Réserver ».

Pendant toute la surveillance, les garde-fous veillent : session expirée,
Chrome qui ne répond plus, page qui ne charge pas, calendrier introuvable
(voir [Les quatre garde-fous](#les-quatre-garde-fous)).

Tu peux désactiver la présélection du jour avec `AUTO_CLICK_FIRST_DAY=0`
dans le `.env`.

## Tableau de bord local (viewer)

`bls_viewer.py` affiche en temps réel ce que fait le script, dans ton
navigateur : compte à rebours, état de la session, mois affiché/scanné, jours
détectés, compteurs (vérifications, échecs, relances de Chrome,
reconnexions) et journal des derniers événements.

Il démarre **automatiquement** avec le script :

```
Tableau de bord de surveillance (lecture seule) : http://127.0.0.1:8765
```

Trois modes :

| Mode | Commande | Usage |
| --- | --- | --- |
| Intégré | `python bls_espagne_prefill.py` | cas normal, le viewer suit le script en direct |
| Autonome | `python bls_viewer.py --status-file bls_status.json --port 8766` | lire l'état d'un script qui tourne déjà (autre terminal/dossier) |
| Démo | `python bls_viewer.py --demo` | voir l'interface sans Chrome ni identifiants (données simulées) |

Options : `--host`, `--port`, `--status-file`.

Bon à savoir :

- **Lecture seule** : le tableau de bord n'agit jamais sur le site, il ne fait
  que refléter l'état du script. Il ne remplace pas la fenêtre Chrome.
- **Aucun secret publié** : le mot de passe ne quitte jamais le script et
  l'email est masqué (`k****m@example.com`).
- **Par défaut sur `127.0.0.1`** : visible depuis ton ordinateur uniquement.
  Si tu mets `VIEWER_HOST=0.0.0.0`, n'importe qui sur ton réseau peut lire
  l'état de ta surveillance — à ne faire que temporairement.
- Le fichier `bls_status.json` (écrit à côté du script) permet le mode
  autonome ; il est exclu de git.
- `VIEWER_ENABLED=0` désactive complètement le tableau de bord : le script
  continue de fonctionner normalement, et il tourne aussi si `bls_viewer.py`
  est absent.

## Les quatre garde-fous

### 1. Scan du mois suivant

Les créneaux s'ouvrent souvent **pour le mois suivant**, alors que le mois
affiché est déjà saturé. Le script clique donc sur « mois suivant » et vérifie
ce mois-là aussi, une vérification sur `NEXT_MONTH_SCAN_EVERY` (2 par défaut).

Ce scan est sécurisé de trois façons :

- le **changement de mois est vérifié** via le libellé du mois avant de lire
  les jours disponibles : impossible d'attribuer au mois suivant les jours du
  mois courant si le clic n'a pas encore été rendu ;
- le **bouton désactivé** (fin de calendrier) est détecté et ignoré ;
- quand rien n'est disponible au mois suivant, le **retour au mois courant est
  effectué puis revérifié** (`restore_month`), pour que les cycles suivants ne
  soient pas décalés d'un mois.

Si des jours sont trouvés au mois suivant, le script **y reste** : le jour
présélectionné est bien celui du mois suivant, et le mois est indiqué dans le
journal et le tableau de bord. `SCAN_NEXT_MONTH=0` désactive ce scan.

### 2. Garde-fou « session expirée »

Le site peut te déconnecter pendant la surveillance. Le script le détecte sur
trois signaux (du moins coûteux au plus coûteux) :

1. présence d'un champ mot de passe,
2. URL redirigée vers `/login`, `/signin`, …,
3. phrase du type « session expirée » dans le texte visible — vérifiée
   uniquement quand le calendrier ne s'est pas affiché.

La détection est faite **avant ET après** l'attente du rendu React : la
redirection vers la page de connexion arrive souvent après le premier rendu,
et un contrôle unique la ratait.

Ensuite, avec `AUTO_RELOGIN_ON_EXPIRY=1` (défaut) :

1. **reconnexion automatique** avec les identifiants du `.env`, en mode non
   interactif (aucune invite bloquante, frappe humaine conservée) ;
2. si un **CAPTCHA/OTP** apparaît, le script s'arrête là — il ne le contourne
   jamais — et passe à l'**alerte sonore + invite au terminal** : tu le
   résous dans Chrome, tu appuies sur Entrée, la surveillance reprend.

Dans les deux cas, le script revient ensuite tout seul sur la page du
calendrier et mémorise son URL.

### 3. Auto-relance de Chrome

Chrome peut se fermer, planter, ou rester bloqué sur un chargement qui ne
finit jamais. Quatre déclencheurs :

| Déclencheur | Seuil par défaut |
| --- | --- |
| exceptions/échecs de chargement consécutifs | `MAX_CONSECUTIVE_FAILURES=5` |
| chargement qui dépasse le délai maximal | `PAGE_LOAD_TIMEOUT_SECONDS=45` |
| calendrier introuvable de façon répétée | `MAX_NO_CALENDAR_ATTEMPTS=6` |
| driver mort (fenêtre fermée, retour de veille) | vérifié à chaque cycle |

Les délais maximaux sont essentiels : sans eux, une page « pendue » bloque le
script indéfiniment et le compteur d'échecs n'avance jamais.

La relance se fait **sans intervention** : ancien Chrome fermé (avec un délai
de 8 s pour ne pas rester pendu), nouveau Chrome lancé, reconnexion non
interactive, retour sur la page du calendrier. Au-delà de
`MAX_DRIVER_RECOVERIES=3` relances successives, le script considère que le
problème n'est pas transitoire : il sonne, affiche la marche à suivre et te
rend la main au lieu de relancer Chrome en boucle.

### 4. Compte à rebours + self-check

- **Compte à rebours** précis jusqu'à `TARGET_HOUR:TARGET_MINUTE`, journalisé
  toutes les 5 min, puis chaque minute dans les 5 dernières, puis toutes les
  10 s dans la dernière minute. Le démarrage se fait à la seconde près.
- **Self-check à T-30 min** (`PREFLIGHT_MINUTES`) : Chrome est-il vivant
  (sinon relance) ? La session est-elle active (sinon reconnexion
  automatique) ? Le calendrier est-il accessible sur l'URL mémorisée ? Le
  résultat est journalisé et publié dans le tableau de bord. Mieux vaut
  découvrir un problème à T-30 min qu'à l'heure critique.
- **Contrôle final à T-2 min** (`FINAL_CHECK_MINUTES`, `0` pour désactiver) :
  navigateur vivant et session en place, **sans naviguer** pour ne pas
  quitter le calendrier à l'approche de l'ouverture.

## Rafraîchissement et calendrier SPA

Le site BLS est une application React : **le calendrier n'a pas toujours sa
propre URL**. Depuis `/manage-appointments`, par exemple, il faut cliquer
jusqu'à afficher le mois — et l'URL ne change pas. Dans ce cas, recharger
l'URL à chaque vérification **réinitialise l'application sur la vue liste** :
le calendrier disparaît et le script journalise `Calendrier non détecté` en
boucle.

Le script gère ça tout seul, en trois temps :

1. **Calibrage** — au démarrage de la surveillance, il recharge une fois la
   page et observe le résultat :
   - le calendrier revient → mode **`reload`** (rechargement complet à chaque
     vérification, comportement classique) ;
   - le calendrier disparaît → mode **`soft`** : la page n'est plus rechargée
     tant que le calendrier est affiché. Les disponibilités sont re-demandées
     par un **aller-retour de mois** (`mois suivant` → `mois précédent`), ce
     qui force react-day-picker à se re-rendre sans perdre l'état de
     navigation. Une resynchronisation complète a lieu toutes les
     `SOFT_RESYNC_EVERY` vérifications (20 par défaut).
2. **Réparation automatique** — si le calendrier disparaît quand même, le
   script cherche un lien/bouton pour le réafficher (« book appointment »,
   « select date », « continuer »…). Les liens à risque (annuler, supprimer,
   payer, déconnexion) sont **systématiquement ignorés** : « cancel
   appointment » contient aussi le mot « appointment ».
3. **Invite ciblée** — si rien n'y fait, le script te demande de réafficher le
   calendrier dans Chrome (`MAX_MANUAL_PROMPTS` fois). **Relancer Chrome ne
   ramènerait pas un calendrier perdu par une SPA** : la relance n'est donc
   utilisée qu'en dernier recours, ou si la page est vraiment vide/cassée.

En mode `soft`, **ne navigue pas dans Chrome** pendant la surveillance (le
script surveille l'onglet actif) et laisse le mois affiché tel quel.

Tu peux forcer un mode avec `REFRESH_MODE=reload` ou `REFRESH_MODE=soft`, et
chaque diagnostic journalise le contenu réel de la page (titre, URL, taille du
HTML, nombre de cellules `rdp-`, boutons/liens visibles) pour identifier le
blocage.

## Tous les réglages

| Variable | Défaut | Rôle |
| --- | --- | --- |
| `BLS_LOGIN_URL` | site BLS Algérie | page de connexion |
| `BLS_EMAIL` / `BLS_PASSWORD` | — | identifiants (jamais publiés) |
| `TARGET_HOUR` / `TARGET_MINUTE` | 16 / 55 | heure d'ouverture des créneaux |
| `REFRESH_INTERVAL_SECONDS` | 5 | intervalle moyen entre vérifications (plancher 2 s) |
| `AUTO_CLICK_FIRST_DAY` | 1 | présélection du premier jour disponible |
| `APPOINTMENT_URL` | vide | URL directe du calendrier |
| `REFRESH_MODE` | auto | `auto` / `reload` / `soft` (calendrier SPA) |
| `SOFT_RESYNC_EVERY` | 20 | resynchronisations complètes en mode `soft` |
| `MAX_MANUAL_PROMPTS` | 2 | invites « réaffiche le calendrier » avant dernier recours |
| `SCAN_NEXT_MONTH` | 1 | scanner aussi le mois suivant |
| `NEXT_MONTH_SCAN_EVERY` | 2 | scanner le mois suivant 1 fois sur N |
| `AUTO_RELOGIN_ON_EXPIRY` | 1 | reconnexion automatique si session expirée |
| `MAX_CONSECUTIVE_FAILURES` | 5 | échecs avant relance de Chrome |
| `MAX_NO_CALENDAR_ATTEMPTS` | 6 | vérifications sans calendrier avant relance |
| `MAX_DRIVER_RECOVERIES` | 3 | relances automatiques avant de te rendre la main |
| `PAGE_LOAD_TIMEOUT_SECONDS` | 45 | délai maximal de chargement |
| `SCRIPT_TIMEOUT_SECONDS` | 20 | délai maximal d'exécution d'un script |
| `CALENDAR_RENDER_TIMEOUT` | 10 | attente du rendu du calendrier |
| `PREFLIGHT_MINUTES` | 30 | self-check à T-30 min |
| `FINAL_CHECK_MINUTES` | 2 | contrôle léger à T-2 min |
| `VIEWER_ENABLED` | 1 | tableau de bord local |
| `VIEWER_HOST` / `VIEWER_PORT` | 127.0.0.1 / 8765 | adresse du tableau de bord |
| `CHROME_VERSION_MAIN` | vide | version majeure de Chrome forcée |

## Premier essai : fais-le calmement, pas dans l'urgence

La détection d'un créneau disponible repose sur les classes CSS du
calendrier de sélection de date (confirmées par inspection du vrai site) :
- un jour **sans créneau** a l'attribut `data-disabled="true"` et la classe
  `rdp-disabled_unavailable`,
- un jour **avec créneau(x)** a une classe `rdp-availability_high`,
  `rdp-availability_limited` ou `rdp-availability_almost_full`, et pas de
  `data-disabled="true"`.

Le script cherche directement ces classes (`AVAILABLE_DAY_CSS`), ce qui est
plus fiable qu'une recherche de texte. Une liste de phrases de secours
(`NO_SLOT_PHRASES`) reste présente au cas où la structure du site changerait.

Lance quand même le script une première fois hors période de pointe pour
vérifier que tout s'enchaîne bien chez toi (connexion, arrivée sur le
calendrier, détection). Le self-check de T-30 min sert exactement à ça :
lance le script au moins 35 min avant l'ouverture pour en bénéficier.

Pour vérifier le tableau de bord sans toucher au site :

```bash
python bls_viewer.py --demo
```

## Limites importantes

- **BLS interdit l'usage d'automates pour réserver.** Ce script ne
  réserve jamais à ta place — il t'aide seulement à arriver plus vite
  devant l'écran de choix, mais l'utiliser reste à tes risques : un usage
  trop agressif (rafraîchissement très fréquent) pourrait attirer
  l'attention ou faire bloquer ton compte. Garde un `REFRESH_INTERVAL_SECONDS`
  raisonnable (le plancher de 2 s est imposé par le script).
- **Le choix du créneau et la confirmation restent volontairement
  manuels.** Un rendez-vous pris par un bot risque d'être annulé et le
  compte bloqué. Le flux de réservation est en outre multi-étapes
  (créneau horaire, titulaires, confirmation, parfois paiement et CAPTCHA
  à cette étape) et change régulièrement : une automatisation aveugle
  échouerait ou gaspillerait le créneau. Le script t'amène directement
  devant l'écran des créneaux, en quelques secondes — le reste, c'est toi.
- Le site est une application React : les identifiants techniques des
  champs changent avec les mises à jour du site. Si la détection
  automatique échoue un jour, le script te laisse toujours remplir
  manuellement — il ne bloque jamais le processus.
- Aucun contournement de CAPTCHA n'est effectué : c'est toujours toi qui
  le résous. La reconnexion automatique s'arrête net dès qu'un CAPTCHA
  apparaît et te passe la main.
- Le tableau de bord est un outil de confort : s'il ne démarre pas (port
  occupé, module absent), le script continue de surveiller normalement.

## Dépannage

### « This version of ChromeDriver only supports Chrome version X / Current browser version is Y »

Le ChromeDriver téléchargé ne correspond pas à ta version de Chrome.
Le script détecte normalement tout seul la bonne version (registre
Windows, dossiers d'installation, etc.). Si ça échoue :

1. Ouvre `chrome://version` dans Chrome et repère le premier nombre
   (ex: `151.0.7922.76` → `151`).
2. Décommente et adapte dans `.env` : `CHROME_VERSION_MAIN=151`.
3. Si l'erreur persiste, vide le cache du driver puis relance (PowerShell) :
   ```powershell
   Remove-Item "$env:USERPROFILE\appdata\roaming\undetected_chromedriver" -Recurse -Force
   ```

### Page blanche

La page BLS s'affiche vide (le script le détecte et recharge jusqu'à 3 fois,
puis journalise titre + taille du HTML + URL) :

1. Dans la fenêtre Chrome : appuie sur **F5**.
2. Ouvre la console (**F12**) pour voir l'erreur réelle (réseau, blocage
   d'extension, Cloudflare).
3. Vérifie ta connexion et désactive temporairement les extensions.
4. Si le problème vient du profil Chrome, renomme `chrome_profile/` et
   relance (tu devras te reconnecter une fois).

### « SESSION EXPIRÉE » répété

Le site te déconnecte souvent pendant la surveillance :

- laisse `AUTO_RELOGIN_ON_EXPIRY=1` pour que le script se reconnecte seul ;
- si un CAPTCHA apparaît à chaque fois, c'est à toi de le résoudre (le script
  sonne et t'attend) ;
- allonge `REFRESH_INTERVAL_SECONDS` (10 s par exemple) : un rafraîchissement
  trop fréquent déclenche des contrôles anti-bot ;
- ne supprime pas `chrome_profile/` entre deux séances : les cookies limitent
  les challenges.

### Chrome s'est fermé / ne répond plus

C'est géré automatiquement : le script le relance (voir
[Auto-relance de Chrome](#3-auto-relance-de-chrome)). S'il atteint le plafond
de relances (`MAX_DRIVER_RECOVERIES`), il sonne et t'attend :

1. regarde le message affiché dans le terminal et le journal `bls_assistant.log` ;
2. vérifie que Chrome n'a pas été mis à jour (voir l'erreur ChromeDriver ci-dessus) ;
3. vérifie qu'un autre Chrome utilisant le même profil n'est pas ouvert
   (`chrome_profile/` ne peut servir qu'à une instance à la fois) ;
4. appuie sur Entrée pour reprendre, ou relance le script.

### Calendrier non détecté

Le script journalise `Calendrier non détecté (n/6)` avec un diagnostic complet
(titre, URL, taille du HTML, nombre de cellules `rdp-`, boutons/liens visibles) :

- **la page charge lentement** → le script retente tout seul ;
- **le calendrier dépend d'un état de l'application (SPA)** → c'est le cas le
  plus courant (voir
  [Rafraîchissement et calendrier SPA](#rafraîchissement-et-calendrier-spa)).
  Le script passe en mode `soft`, cherche un bouton pour réafficher le
  calendrier, puis te demande de le faire toi-même si besoin. Vérifie que tu
  étais bien allé jusqu'à voir **le mois et ses jours** avant d'appuyer sur
  Entrée lors de la navigation manuelle ;
- **le site a changé de structure** → lis la liste des boutons/liens visibles
  dans le journal : si le calendrier passe par un nouveau bouton, son texte
  peut être ajouté à `BOOKING_LINK_TEXTS` / `BOOKING_STEP_TEXTS` dans le
  script. Dans tous les cas, navigue manuellement jusqu'au calendrier : la
  nouvelle URL est mémorisée dans `appointment_url.txt`.

### Le tableau de bord ne s'affiche pas

- vérifie la ligne `Tableau de bord de surveillance (lecture seule) : …` dans
  le terminal ;
- si le port est occupé : `VIEWER_PORT=8766` dans le `.env`, ou
  `python bls_viewer.py --port 8766 --status-file bls_status.json` ;
- si tu y accèdes depuis un autre appareil, il faut `VIEWER_HOST=0.0.0.0`
  (et le firewall doit laisser passer le port) — mais l'état devient visible
  sur ton réseau ;
- `VIEWER_ENABLED=0` le désactive volontairement.
