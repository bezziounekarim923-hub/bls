#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BLS Espagne (Algérie) — Assistant de pré-remplissage (Anti-Détection / Stealth)
=============================================================================

MODIFICATIONS INTÉGRÉES :
- Utilisation de undetected-chromedriver pour contourner la détection WAF/Cloudflare.
- Saisie réaliste (frappe humaine avec délais variables).
- Intervalle de rafraîchissement avec jitter aléatoire.
- Gestion d'un profil Chrome persistant (conserve cookies et session).
"""

import os
import sys
import time
import random
import logging
import platform
from datetime import datetime

# Essai d'import de undetected-chromedriver avec fallback sur selenium classique
try:
    import undetected_chromedriver as uc
    USE_UNDETECTED = True
except ImportError:
    USE_UNDETECTED = False
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    from webdriver_manager.chrome import ChromeDriverManager

from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException,
    NoSuchElementException,
    ElementNotInteractableException,
    ElementClickInterceptedException,
)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


# ============ CONFIGURATION ============

BLS_LOGIN_URL = os.getenv("BLS_LOGIN_URL", "https://algeria.blsinternational.com/")

BLS_EMAIL = os.getenv("BLS_EMAIL", "")
BLS_PASSWORD = os.getenv("BLS_PASSWORD", "")

TARGET_HOUR = int(os.getenv("TARGET_HOUR", "16"))
TARGET_MINUTE = int(os.getenv("TARGET_MINUTE", "55"))

# Intervalle de base entre les vérifications (en secondes)
REFRESH_INTERVAL_SECONDS = float(os.getenv("REFRESH_INTERVAL_SECONDS", "5"))

AVAILABLE_DAY_CSS = (
    "td[class*='rdp-availability_']"
    ":not([data-disabled='true'])"
    ":not([data-outside='true'])"
)

NO_SLOT_PHRASES = [
    "no slots available",
    "no appointment",
    "no appointments available",
    "aucun rendez-vous",
    "aucun creneau",
    "pas de creneau disponible",
    "no slot available",
]

LOGIN_BUTTON_TEXTS = ["login", "log in", "se connecter", "connexion", "sign in"]

# Chemins ancrés à l'emplacement du script (indépendants du répertoire
# depuis lequel le script est lancé)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(SCRIPT_DIR, "bls_assistant.log")

# Profil Chrome persistant (cookies, session, cache) — jamais commité (.gitignore)
PROFILE_DIR = os.path.join(SCRIPT_DIR, "chrome_profile")

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


# ---------- Fonctions utilitaires d'imitation humaine ----------

def sleep_with_jitter(base_seconds: float, variance: float = 1.5):
    """Attend pendant une durée variable autour de la valeur de base."""
    delay = max(1.0, base_seconds + random.uniform(-variance, variance))
    time.sleep(delay)


def human_type(element, text: str, min_delay: float = 0.04, max_delay: float = 0.14):
    """Tape du texte caractère par caractère avec un intervalle aléatoire."""
    element.click()
    time.sleep(random.uniform(0.15, 0.35))
    for char in text:
        element.send_keys(char)
        time.sleep(random.uniform(min_delay, max_delay))


# ---------- Initialisation du Driver Stealth ----------

def create_driver():
    """Crée une instance Chrome configurée pour minimiser les signaux d'automatisation."""
    # Langues cohérentes entre l'interface Chrome et navigator.languages
    lang_prefs = {"intl.accept_languages": "fr-FR,fr,en-US,en"}

    if USE_UNDETECTED:
        logger.info("Démarrage via undetected-chromedriver (mode stealth actif)...")
        options = uc.ChromeOptions()
        options.add_argument("--window-size=1920,1080")
        # Chrome n'accepte qu'un seul locale pour --lang (pas de liste séparée
        # par des virgules) ; la liste complète passe par la pref ci-dessous.
        options.add_argument("--lang=fr-FR")
        options.add_experimental_option("prefs", lang_prefs)

        # Profil Chrome persistant (cookies/session conservés entre les
        # exécutions -> moins de challenges anti-bot récurrents).
        # On utilise le kwarg officiel `user_data_dir` de undetected-chromedriver,
        # ancré au dossier du script.
        os.makedirs(PROFILE_DIR, exist_ok=True)
        driver = uc.Chrome(
            options=options,
            user_data_dir=PROFILE_DIR,
            use_subprocess=True,
        )
    else:
        logger.warning(
            "undetected-chromedriver non installé. Utilisation de Selenium standard "
            "(Installez undetected-chromedriver avec `pip install undetected-chromedriver` pour une meilleure discrétion)."
        )
        options = webdriver.ChromeOptions()
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--window-size=1920,1080")
        options.add_argument("--lang=fr-FR")
        options.add_experimental_option("prefs", lang_prefs)

        # Profil persistant également en mode repli (avec Selenium classique,
        # on passe l'option Chrome brute)
        os.makedirs(PROFILE_DIR, exist_ok=True)
        options.add_argument(f"--user-data-dir={PROFILE_DIR}")

        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)

    driver.maximize_window()
    return driver


# ---------- Alerte sonore multiplateforme ----------

def beep_alert(times: int = 5):
    system = platform.system()
    for _ in range(times):
        try:
            if system == "Windows":
                import winsound
                winsound.Beep(1000, 400)
            elif system == "Darwin":
                os.system("afplay /System/Library/Sounds/Glass.aiff 2>/dev/null")
            else:
                os.system("paplay /usr/share/sounds/freedesktop/stereo/complete.oga 2>/dev/null || printf '\\a'")
        except Exception:
            print("\a", end="", flush=True)
        time.sleep(0.4)


def alert_slot_found():
    banner = "⚡ CRÉNEAU POTENTIELLEMENT DISPONIBLE — REGARDE LA FENÊTRE CHROME MAINTENANT ⚡"
    logger.info("=" * len(banner))
    logger.info(banner)
    logger.info("=" * len(banner))
    beep_alert(times=6)


def wait_until_target_time():
    logger.info("En attente de %02d:%02d...", TARGET_HOUR, TARGET_MINUTE)
    while True:
        now = datetime.now()
        if (now.hour, now.minute) >= (TARGET_HOUR, TARGET_MINUTE):
            logger.info("Heure cible atteinte (%02d:%02d), on continue.", now.hour, now.minute)
            break
        time.sleep(5)


# ---------- Détection robuste de champs ----------

def find_first(driver, locators, description, timeout=15):
    wait = WebDriverWait(driver, timeout)
    for by, value in locators:
        try:
            el = wait.until(EC.presence_of_element_located((by, value)))
            logger.info("Champ '%s' trouvé via %s=%s", description, by, value)
            return el
        except TimeoutException:
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
    locators = [(By.CSS_SELECTOR, "button[type='submit']")]
    btn = find_first(driver, locators, "bouton de connexion (par type)", timeout=5)
    if btn:
        return btn

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

def login(driver) -> None:
    driver.get(BLS_LOGIN_URL)
    logger.info("Page de connexion chargée : %s", BLS_LOGIN_URL)
    sleep_with_jitter(3.0, 0.8)

    if not BLS_EMAIL or not BLS_PASSWORD:
        logger.warning("BLS_EMAIL / BLS_PASSWORD non définis dans le fichier .env.")
        input(">>> Appuie sur Entrée une fois connecté manuellement...")
        return

    email_field = find_email_field(driver)
    password_field = find_password_field(driver)

    if email_field is None or password_field is None:
        logger.warning("Champs introuvables automatiquement. Remplis le formulaire toi-même.")
        input(">>> Appuie sur Entrée une fois connecté manuellement...")
        return

    try:
        email_field.clear()
        human_type(email_field, BLS_EMAIL)
        
        sleep_with_jitter(0.5, 0.2)
        
        password_field.clear()
        human_type(password_field, BLS_PASSWORD)
        logger.info("Identifiants saisis avec cadence humaine.")
    except (ElementNotInteractableException, ElementClickInterceptedException):
        logger.warning("Champs non interactifs (ou masqués par un bandeau). Saisis tes identifiants à la main.")
        input(">>> Appuie sur Entrée une fois les identifiants saisis...")

    logger.info("Si un CAPTCHA/OTP est demandé, résous-le manuellement dans Chrome.")
    input(">>> Appuie sur Entrée ici dès que l'étape CAPTCHA/OTP est franchie...")

    login_button = find_login_button(driver)
    if login_button:
        try:
            sleep_with_jitter(0.3, 0.1)
            login_button.click()
            logger.info("Connexion envoyée.")
        except (ElementNotInteractableException, ElementClickInterceptedException):
            logger.warning("Clique toi-même sur le bouton de connexion.")
            input(">>> Appuie sur Entrée une fois connecté...")
    else:
        try:
            password_field.send_keys(Keys.RETURN)
            logger.info("Connexion envoyée via Entrée.")
        except Exception:
            logger.warning("Clique toi-même sur le bouton de connexion.")
            input(">>> Appuie sur Entrée une fois connecté...")

    sleep_with_jitter(3.0, 0.5)


def navigate_to_appointment_page(driver) -> str:
    logger.info("Navigue jusqu'à la page des créneaux dans la fenêtre Chrome.")
    input(">>> Appuie sur Entrée une fois arrivé sur le calendrier des créneaux...")
    appointment_url = driver.current_url
    logger.info("URL des créneaux mémorisée : %s", appointment_url)
    return appointment_url


def find_available_dates(driver):
    try:
        return driver.find_elements(By.CSS_SELECTOR, AVAILABLE_DAY_CSS)
    except Exception as e:
        logger.debug("Erreur lors de la recherche des jours : %s", e)
        return []


def refresh_until_slot_appears(driver, appointment_url: str) -> None:
    logger.info(
        "Surveillance active (intervalle moyen ~%.1fs avec jitter). "
        "Une alerte sonore sera déclenchée dès qu'un créneau apparaît.",
        REFRESH_INTERVAL_SECONDS,
    )
    attempt = 0
    while True:
        attempt += 1
        try:
            driver.get(appointment_url)
        except Exception as e:
            logger.warning("Erreur réseau (%s), nouvel essai...", e)
            sleep_with_jitter(REFRESH_INTERVAL_SECONDS, 1.5)
            continue

        sleep_with_jitter(REFRESH_INTERVAL_SECONDS, 1.5)

        available_days = find_available_dates(driver)
        page_text = driver.page_source.lower()
        no_slot_text = any(phrase in page_text for phrase in NO_SLOT_PHRASES)

        if attempt % 10 == 0:
            logger.info("Surveillance en cours... (%d vérifications)", attempt)

        if available_days:
            dates_found = [d.get_attribute("data-day") or "Date non précisée" for d in available_days]
            logger.info("Jour(s) disponible(s) détecté(s) : %s", ", ".join(dates_found))
            alert_slot_found()
            logger.info("Le script s'arrête. Réserve immédiatement dans la fenêtre Chrome.")
            break
        elif not no_slot_text and page_text.strip():
            logger.debug("Aucun jour ni message d'indisponibilité explicite.")


def main():
    logger.info("Préparation du navigateur Chrome...")
    driver = create_driver()

    try:
        wait_until_target_time()
        login(driver)
        appointment_url = navigate_to_appointment_page(driver)
        refresh_until_slot_appears(driver, appointment_url)

        input(">>> Appuie sur Entrée quand tu as terminé pour fermer l'assistant...")

    except KeyboardInterrupt:
        logger.info("Arrêt demandé par l'utilisateur.")
    finally:
        logger.info("Script terminé. Le navigateur reste ouvert.")


if __name__ == "__main__":
    main()
