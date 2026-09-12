#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BLS Espagne (Algérie) — Assistant de pré-remplissage (Selenium)
=================================================================

CE QUE FAIT CE SCRIPT :
- Ouvre un vrai navigateur Chrome (visible, pas caché).
- T'aide à te connecter à ton compte BLS (remplissage automatique
  du formulaire quand c'est possible, sinon il te laisse la main).
- Navigue jusqu'à la page de sélection du créneau de rendez-vous.
- Rafraîchit automatiquement cette page et te PRÉVIENT (son + message)
  dès qu'un créneau semble disponible.
- S'ARRÊTE toujours avant la réservation finale — c'est TOI qui choisis
  le créneau et cliques sur "Réserver"/"Confirmer".

CE QUE CE SCRIPT NE FAIT JAMAIS :
- Il ne clique jamais sur le bouton final de réservation à ta place.
  BLS interdit l'usage d'automates pour décrocher un rendez-vous, et un
  bot qui irait jusqu'au bout risquerait l'annulation du rendez-vous ou
  le blocage du compte. Ce script te fait gagner du temps sur la partie
  répétitive (connexion + surveillance), rien de plus.
- Il ne contourne aucun CAPTCHA. S'il y en a un, le script s'arrête et
  te laisse le résoudre toi-même dans la fenêtre Chrome ouverte.

POURQUOI PAS DE SÉLECTEURS CSS FIGÉS :
Le site BLS est une application web moderne (React) : les identifiants
techniques des champs (id, class) sont générés automatiquement et
peuvent changer à chaque mise à jour du site. Ce script cherche donc
les champs par leur TYPE (email, password) et par le TEXTE visible des
boutons, ce qui est beaucoup plus stable dans le temps.

IMPORTANT :
- Je n'ai pas pu tester ce script avec le vrai site (le contenu est
  chargé en JavaScript et une connexion réelle nécessite tes propres
  identifiants). Teste-le une première fois calmement, PAS dans
  l'urgence d'un créneau, pour vérifier que la détection fonctionne
  chez toi et ajuster les listes de mots-clés si besoin (voir
  CONFIGURATION ci-dessous).
- Ne mets jamais ton email/mot de passe en clair dans ce fichier si tu
  comptes le partager ou le mettre sur GitHub. Utilise le fichier
  .env (voir .env.example fourni à côté).

INSTALLATION :
    pip install selenium webdriver-manager python-dotenv

LANCEMENT :
    python bls_espagne_prefill.py
"""

import os
import sys
import time
import logging
import platform
from datetime import datetime

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service
from selenium.common.exceptions import (
    TimeoutException,
    NoSuchElementException,
    ElementNotInteractableException,
)
from webdriver_manager.chrome import ChromeDriverManager

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv optionnel : si absent, on utilise les variables d'env classiques


# ============ CONFIGURATION ============

BLS_LOGIN_URL = os.getenv("BLS_LOGIN_URL", "https://algeria.blsinternational.com/")

# Identifiants : à mettre dans un fichier .env (voir .env.example), jamais en dur ici.
BLS_EMAIL = os.getenv("BLS_EMAIL", "")
BLS_PASSWORD = os.getenv("BLS_PASSWORD", "")

# Heure cible (24h) à laquelle le script doit être prêt et se connecter.
TARGET_HOUR = int(os.getenv("TARGET_HOUR", "16"))
TARGET_MINUTE = int(os.getenv("TARGET_MINUTE", "55"))

# Une fois sur la page de créneaux, intervalle entre deux rafraîchissements.
REFRESH_INTERVAL_SECONDS = int(os.getenv("REFRESH_INTERVAL_SECONDS", "5"))

# Le calendrier de sélection de date marque chaque jour avec des classes CSS :
#   - jours SANS créneau : attribut data-disabled="true" + classe rdp-disabled_unavailable
#   - jours AVEC créneau : classe rdp-availability_high / rdp-availability_limited /
#     rdp-availability_almost_full, et PAS de data-disabled="true"
# On cherche donc directement une cellule <td> qui a une classe "rdp-availability_"
# ET qui n'est pas marquée data-disabled="true" (ni "outside" = hors du mois affiché).
AVAILABLE_DAY_CSS = (
    "td[class*='rdp-availability_']"
    ":not([data-disabled='true'])"
    ":not([data-outside='true'])"
)

# Filet de sécurité textuel, au cas où la structure du calendrier changerait un jour.
NO_SLOT_PHRASES = [
    "no slots available",
    "no appointment",
    "no appointments available",
    "aucun rendez-vous",
    "aucun creneau",
    "pas de creneau disponible",
    "no slot available",
]

# Textes / attributs utilisés pour repérer le bouton de connexion.
LOGIN_BUTTON_TEXTS = ["login", "log in", "se connecter", "connexion", "sign in"]

LOG_FILE = "bls_assistant.log"

# =====================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


# ---------- Alerte sonore multiplateforme ----------

def beep_alert(times: int = 5):
    """Émet un signal sonore répété pour attirer l'attention, quel que soit l'OS."""
    system = platform.system()
    for _ in range(times):
        try:
            if system == "Windows":
                import winsound
                winsound.Beep(1000, 400)
            elif system == "Darwin":  # macOS
                os.system("afplay /System/Library/Sounds/Glass.aiff 2>/dev/null")
            else:  # Linux
                os.system("paplay /usr/share/sounds/freedesktop/stereo/complete.oga 2>/dev/null "
                          "|| printf '\\a'")
        except Exception:
            # Repli universel : bip terminal (fonctionne presque partout)
            print("\a", end="", flush=True)
        time.sleep(0.4)


def alert_slot_found():
    banner = "⚡ CRÉNEAU POTENTIELLEMENT DISPONIBLE — REGARDE LA FENÊTRE CHROME MAINTENANT ⚡"
    logger.info("=" * len(banner))
    logger.info(banner)
    logger.info("=" * len(banner))
    beep_alert(times=6)


# ---------- Attente de l'heure cible ----------

def wait_until_target_time():
    """Attend que l'heure cible soit atteinte avant de continuer."""
    logger.info("En attente de %02d:%02d...", TARGET_HOUR, TARGET_MINUTE)
    while True:
        now = datetime.now()
        if (now.hour, now.minute) >= (TARGET_HOUR, TARGET_MINUTE):
            logger.info("Heure cible atteinte (%02d:%02d), on continue.", now.hour, now.minute)
            break
        time.sleep(10)


# ---------- Détection robuste de champs ----------

def find_first(driver, locators, description, timeout=15):
    """Essaie plusieurs (By, valeur) dans l'ordre et retourne le premier élément trouvé."""
    wait = WebDriverWait(driver, timeout)
    last_error = None
    for by, value in locators:
        try:
            el = wait.until(EC.presence_of_element_located((by, value)))
            logger.info("Champ '%s' trouvé via %s=%s", description, by, value)
            return el
        except TimeoutException as e:
            last_error = e
            continue
    logger.warning("Impossible de trouver automatiquement le champ '%s'.", description)
    return None


def find_email_field(driver):
    locators = [
        (By.CSS_SELECTOR, "input[type='email']"),
        (By.CSS_SELECTOR, "input[name*='email' i]"),
        (By.CSS_SELECTOR, "input[id*='email' i]"),
        (By.CSS_SELECTOR, "input[placeholder*='email' i]"),
        (By.CSS_SELECTOR, "input[placeholder*='mail' i]"),
        (By.CSS_SELECTOR, "input[autocomplete='username']"),
    ]
    return find_first(driver, locators, "email")


def find_password_field(driver):
    locators = [
        (By.CSS_SELECTOR, "input[type='password']"),
        (By.CSS_SELECTOR, "input[name*='pass' i]"),
        (By.CSS_SELECTOR, "input[id*='pass' i]"),
    ]
    return find_first(driver, locators, "mot de passe")


def find_login_button(driver):
    # 1) Recherche par attributs stables
    locators = [
        (By.CSS_SELECTOR, "button[type='submit']"),
    ]
    btn = find_first(driver, locators, "bouton de connexion (par type)", timeout=5)
    if btn:
        return btn

    # 2) Recherche par texte visible (boutons ET liens)
    for text in LOGIN_BUTTON_TEXTS:
        xpath = (
            f"//button[contains(translate(text(), "
            f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{text}')] "
            f"| //a[contains(translate(text(), "
            f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{text}')]"
        )
        try:
            el = driver.find_element(By.XPATH, xpath)
            logger.info("Bouton de connexion trouvé via le texte '%s'.", text)
            return el
        except NoSuchElementException:
            continue

    logger.warning("Bouton de connexion introuvable automatiquement.")
    return None


# ---------- Étapes principales ----------

def login(driver: webdriver.Chrome) -> None:
    """Se connecte au compte BLS. S'arrête pour toi si un captcha apparaît
    ou si un champ n'est pas trouvé automatiquement."""
    driver.get(BLS_LOGIN_URL)
    logger.info("Page de connexion chargée : %s", BLS_LOGIN_URL)
    time.sleep(3)  # laisser le temps au JS (React) de charger le formulaire

    if not BLS_EMAIL or not BLS_PASSWORD:
        logger.warning(
            "BLS_EMAIL / BLS_PASSWORD non définis (fichier .env manquant ou vide). "
            "Connecte-toi manuellement dans la fenêtre Chrome."
        )
        input(">>> Appuie sur Entrée une fois connecté manuellement...")
        return

    email_field = find_email_field(driver)
    password_field = find_password_field(driver)

    if email_field is None or password_field is None:
        logger.warning(
            "Un ou plusieurs champs n'ont pas été trouvés automatiquement. "
            "Remplis le formulaire toi-même dans la fenêtre Chrome."
        )
        input(">>> Appuie sur Entrée une fois connecté manuellement...")
        return

    try:
        email_field.clear()
        email_field.send_keys(BLS_EMAIL)
        password_field.clear()
        password_field.send_keys(BLS_PASSWORD)
        logger.info("Identifiants saisis automatiquement.")
    except ElementNotInteractableException:
        logger.warning("Champs non interactifs pour le moment. Remplis-les toi-même.")
        input(">>> Appuie sur Entrée une fois les identifiants saisis...")

    logger.info(
        "Si un CAPTCHA ou un code OTP est demandé, résous/saisis-le maintenant "
        "à la main dans la fenêtre Chrome."
    )
    input(">>> Appuie sur Entrée ici une fois le CAPTCHA/OTP géré (ou tout de suite s'il n'y en a pas)...")

    login_button = find_login_button(driver)
    if login_button:
        try:
            login_button.click()
            logger.info("Connexion envoyée.")
        except ElementNotInteractableException:
            logger.warning("Impossible de cliquer automatiquement. Clique toi-même sur 'Login'.")
            input(">>> Appuie sur Entrée une fois connecté...")
    else:
        try:
            password_field.send_keys(Keys.RETURN)
            logger.info("Connexion envoyée via la touche Entrée.")
        except Exception:
            logger.warning("Clique toi-même sur le bouton de connexion.")
            input(">>> Appuie sur Entrée une fois connecté...")

    time.sleep(3)


def navigate_to_appointment_page(driver: webdriver.Chrome) -> None:
    """Te laisse naviguer jusqu'à la page de sélection du créneau,
    puis retient l'URL pour les rafraîchissements automatiques."""
    logger.info(
        "Arrivé sur le tableau de bord. Navigue maintenant toi-même jusqu'au "
        "menu 'Prendre un rendez-vous' / 'Book an appointment'."
    )
    input(">>> Appuie sur Entrée une fois arrivé sur l'écran de sélection du créneau...")
    appointment_url = driver.current_url
    logger.info("URL de la page de créneaux mémorisée : %s", appointment_url)
    return appointment_url


def find_available_dates(driver):
    """Retourne la liste des cellules <td> du calendrier qui représentent un
    jour avec créneau(x) disponible(s) (voir AVAILABLE_DAY_CSS)."""
    try:
        return driver.find_elements(By.CSS_SELECTOR, AVAILABLE_DAY_CSS)
    except Exception as e:
        logger.debug("Erreur pendant la recherche de jours disponibles : %s", e)
        return []


def refresh_until_slot_appears(driver: webdriver.Chrome, appointment_url: str) -> None:
    """Rafraîchit la page de sélection jusqu'à détecter un jour du calendrier
    avec un créneau disponible, puis s'arrête et t'alerte — c'est à TOI de
    choisir le jour/l'heure et de cliquer sur 'Réserver'."""
    logger.info(
        "Rafraîchissement automatique en cours (toutes les %ss). "
        "Dès qu'un jour disponible apparaît dans le calendrier, tu seras alerté par un son.",
        REFRESH_INTERVAL_SECONDS,
    )
    attempt = 0
    while True:
        attempt += 1
        try:
            driver.get(appointment_url)
        except Exception as e:
            logger.warning("Erreur lors du rafraîchissement (%s), nouvel essai...", e)
            time.sleep(REFRESH_INTERVAL_SECONDS)
            continue

        time.sleep(REFRESH_INTERVAL_SECONDS)

        available_days = find_available_dates(driver)

        # Filet de sécurité : si jamais la structure du calendrier a changé,
        # on regarde aussi le texte de la page.
        page_text = driver.page_source.lower()
        no_slot_text = any(phrase in page_text for phrase in NO_SLOT_PHRASES)

        if attempt % 12 == 0:  # log de vie toutes les ~1 min (avec l'intervalle par défaut)
            logger.info("Toujours en surveillance... (%d rafraîchissements effectués)", attempt)

        if available_days:
            dates_found = [d.get_attribute("data-day") for d in available_days]
            logger.info("Jour(s) disponible(s) détecté(s) : %s", ", ".join(dates_found))
            alert_slot_found()
            logger.info(
                "Le script s'arrête ici. Vérifie la fenêtre Chrome, choisis ton "
                "créneau et clique sur 'Réserver' toi-même, MAINTENANT."
            )
            break
        elif not no_slot_text and page_text.strip():
            logger.debug(
                "Aucun jour disponible détecté via CSS, et aucun texte "
                "d'indisponibilité connu trouvé non plus. Poursuite de la surveillance."
            )


def main():
    logger.info("Préparation du navigateur Chrome...")
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service)
    driver.maximize_window()

    try:
        wait_until_target_time()
        login(driver)
        appointment_url = navigate_to_appointment_page(driver)
        refresh_until_slot_appears(driver, appointment_url)

        logger.info("Le navigateur reste ouvert — termine la réservation manuellement.")
        input(">>> Appuie sur Entrée ici seulement quand tu as fini, pour arrêter le script "
              "(le navigateur restera ouvert).")

    except KeyboardInterrupt:
        logger.info("Arrêt manuel demandé (Ctrl+C).")
    finally:
        logger.info("Script terminé. Le navigateur Chrome reste ouvert pour toi.")


if __name__ == "__main__":
    main()
