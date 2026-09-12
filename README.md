# Assistant de pré-remplissage BLS Espagne (Algérie)

Automatise les parties répétitives (connexion, surveillance des créneaux) sans
jamais réserver à ta place. **Le clic final "Réserver" reste toujours manuel.**

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

## Configuration

1. Copie `.env.example` en `.env` :
   ```bash
   cp .env.example .env
   ```
2. Ouvre `.env` et remplis ton email et ton mot de passe BLS.
3. Ajuste `TARGET_HOUR` / `TARGET_MINUTE` selon le jour où les créneaux
   s'ouvrent (mardi 16h55 ou vendredi 15h55, par exemple).

**Ne partage jamais ton fichier `.env` et ne le committe jamais** (il est
exclu de git via `.gitignore`). Si cela t'arrive un jour, change
immédiatement ton mot de passe BLS.

## Lancement

```bash
python bls_espagne_prefill.py
```

Le script va :
1. Attendre l'heure cible.
2. Ouvrir Chrome et essayer de remplir automatiquement email/mot de passe.
3. Te laisser gérer le CAPTCHA/OTP toi-même si demandé.
4. Te laisser naviguer manuellement jusqu'à l'écran des créneaux (le site
   change souvent de structure, donc c'est plus fiable ainsi).
5. Rafraîchir automatiquement cette page et, **dès qu'un créneau apparaît** :
   vérifier immédiatement (au rendu du calendrier, sans attendre un délai
   fixe), **présélectionner le premier jour disponible**, remettre la
   fenêtre Chrome au premier plan et déclencher une **alerte sonore**.
6. S'arrêter là — il ne te reste que deux clics : choisir le créneau
   horaire puis « Réserver ».

Tu peux désactiver la présélection du jour avec `AUTO_CLICK_FIRST_DAY=0`
dans le `.env`.

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
calendrier, détection).

## Limites importantes

- **BLS interdit l'usage d'automates pour réserver.** Ce script ne
  réserve jamais à ta place — il t'aide seulement à arriver plus vite
  devant l'écran de choix, mais l'utiliser reste à tes risques : un usage
  trop agressif (rafraîchissement très fréquent) pourrait attirer
  l'attention ou faire bloquer ton compte. Garde un `REFRESH_INTERVAL_SECONDS`
  raisonnable.
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
  le résous.

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
