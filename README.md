# Assistant de pré-remplissage BLS Espagne (Algérie)

Automatise les parties répétitives (connexion, surveillance des créneaux) sans
jamais réserver à ta place. **Le clic final "Réserver" reste toujours manuel.**

## Installation

```bash
pip install selenium webdriver-manager python-dotenv
```

Il te faut aussi Google Chrome installé sur ton ordinateur (le driver est géré
automatiquement par `webdriver-manager`).

## Configuration

1. Copie `.env.example` en `.env` :
   ```bash
   cp .env.example .env
   ```
2. Ouvre `.env` et remplis ton email et ton mot de passe BLS.
3. Ajuste `TARGET_HOUR` / `TARGET_MINUTE` selon le jour où les créneaux
   s'ouvrent (mardi 16h55 ou vendredi 15h55, par exemple).

**Ne partage jamais ton fichier `.env`.**

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
5. Rafraîchir automatiquement cette page et te prévenir par un **son** dès
   qu'un créneau semble disponible.
6. S'arrêter — à toi de choisir le créneau et de cliquer sur "Réserver".

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
- Le site est une application React : les identifiants techniques des
  champs changent avec les mises à jour du site. Si la détection
  automatique échoue un jour, le script te laisse toujours remplir
  manuellement — il ne bloque jamais le processus.
- Aucun contournement de CAPTCHA n'est effectué : c'est toujours toi qui
  le résous.
