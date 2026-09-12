#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BLS Espagne (Algérie) — Assistant de pré-remplissage (Anti-Détection / Stealth)
=============================================================================

MODIFICATIONS INTÉGRÉES :
- Utilisation de undetected-chromedriver pour masquer les signaux Selenium (Cloudflare / WAF).
- Saisie réaliste (frappe humaine avec délais variables).
- Intervalle de rafraîchissement avec jitter aléatoire.
- Gestion d'un profil Chrome persistant (conserve cookies et session).
- Détection immédiate d'un créneau (au rendu du calendrier, sans délai fixe),
  présélection automatique du premier jour disponible, fenêtre remise au
  premier plan et alarme sonore. Le choix du créneau horaire et la
  confirmation de réservation restent volontairement manuels.
"""

import os
import re
import sys
import time
import random
import logging
import platform
import subprocess
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
    SessionNotCreatedException,
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

# Version majeure de Chrome (ex: 151). Vide = détection automatique.
# Ouvre chrome://version dans Chrome : le premier nombre est la version
# majeure (ex: 151.0.7922.76 -> 151).
CHROME_VERSION_MAIN = os.getenv("CHROME_VERSION_MAIN", "").strip()

# Présélection automatique du premier jour disponible dès sa détection :
# t'affiche directement l'écran des créneaux horaires. Le choix du créneau
# et la confirmation de réservation restent TOUJOURS manuels.
# AUTO_CLICK_FIRST_DAY=0 pour désactiver.
AUTO_CLICK_FIRST_DAY = (
    os.getenv("AUTO_CLICK_FIRST_DAY", "1").strip().lower() in ("1", "true", "yes", "oui")
)

# URL directe de la page du calendrier des créneaux (OPTIONNEL).
# Si vide, le script essaie automatiquement, dans cet ordre :
#   1. l'URL mémorisée au dernier lancement réussi (appointment_url.txt),
#   2. un clic sur le lien « prendre rendez-vous » après connexion,
#   3. et te laisse enfin naviguer manuellement (comportement d'origine).
APPOINTMENT_URL = os.getenv("APPOINTMENT_URL", "").strip()

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

# Textes/href recherchés pour trouver le lien « prendre rendez-vous »
# (sans accents : translate() XPath ne gère pas les caractères accentués)
BOOKING_LINK_TEXTS = [
    "prendre rendez-vous",
    "prendre un rendez-vous",
    "demander un rendez-vous",
    "nouveau rendez-vous",
    "book appointment",
    "book an appointment",
    "make an appointment",
    "request appointment",
    "request new appointment",
    "schedule an appointment",
    "faire une demande",
    "book now",
    "new appointment",
]
BOOKING_HREF_KEYWORDS = ["appointment", "booking", "schedule", "book", "demande"]

# Chemins ancrés à l'emplacement du script (indépendants du répertoire
# depuis lequel le script est lancé)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(SCRIPT_DIR, "bls_assistant.log")

# Profil Chrome persistant (cookies, session, cache) — jamais commité (.gitignore)
PROFILE_DIR = os.path.join(SCRIPT_DIR, "chrome_profile")

# Fichier mémorisant l'URL du calendrier du dernier lancement réussi
APPOINTMENT_URL_FILE = os.path.join(SCRIPT_DIR, "appointment_url.txt")

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


# ---------- Détection de la version de Chrome installée ----------

def detect_chrome_major_version():
    """
    Détecte la version majeure de Chrome installée (best effort).

    undetected-chromedriver, quand on ne lui passe pas `version_main`,
    télécharge le DERNIER ChromeDriver stable publié — qui ne correspond
    pas forcément au Chrome réellement installé (ex: driver 153 pour un
    Chrome 151 -> SessionNotCreatedException). Cette détection permet de
    lui passer la bonne version majeure.
    """
    # 1. Windows : clé de registre maintenue par Chrome lui-même
    try:
        import winreg  # uniquement disponible sur Windows
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(hive, r"Software\Google\Chrome\BLBeacon") as key:
                    version, _ = winreg.QueryValueEx(key, "version")
                    return int(str(version).split(".")[0])
            except OSError:
                continue
    except ImportError:
        pass

    # 2. Windows : dossier "<...>\\Google\\Chrome\\Application\\<version>"
    app_dirs = [
        os.path.join(base, "Google", "Chrome", "Application")
        for base in (
            os.environ.get("ProgramFiles", ""),
            os.environ.get("ProgramFiles(x86)", ""),
            os.environ.get("LocalAppData", ""),
        )
        if base
    ]
    for app_dir in app_dirs:
        try:
            for entry in os.listdir(app_dir):
                if re.fullmatch(r"\d+(\.\d+){3}", entry):
                    return int(entry.split(".")[0])
        except OSError:
            continue

    # 3. macOS : dossiers de versions du bundle Chrome
    framework_versions = (
        "/Applications/Google Chrome.app/Contents/Frameworks/"
        "Google Chrome Framework.framework/Versions"
    )
    try:
        for entry in os.listdir(framework_versions):
            if re.fullmatch(r"\d+(\.\d+){3}", entry):
                return int(entry.split(".")[0])
    except OSError:
        pass

    # 4. Linux : interroger directement le binaire
    for binary in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        try:
            result = subprocess.run(
                [binary, "--version"], capture_output=True, text=True, timeout=10
            )
            match = re.search(r"(\d+)\.", result.stdout)
            if match:
                return int(match.group(1))
        except Exception:
            continue

    return None


# ---------- Initialisation du Driver Stealth ----------

def create_driver():
    """Crée une instance Chrome configurée pour minimiser les signaux d'automatisation."""
    # Langues cohérentes entre l'interface Chrome et navigator.languages
    lang_prefs = {"intl.accept_languages": "fr-FR,fr,en-US,en"}

    if USE_UNDETECTED:
        logger.info("Démarrage via undetected-chromedriver (mode stealth actif)...")
        # undetected-chromedriver télécharge par défaut le DERNIER
        # ChromeDriver stable, pas celui qui correspond au Chrome installé
        # (-> "This version of ChromeDriver only supports Chrome version X").
        # On lui passe donc la version majeure du Chrome réellement installé.
        chrome_major = None
        if CHROME_VERSION_MAIN:
            try:
                chrome_major = int(CHROME_VERSION_MAIN)
                logger.info("Version Chrome forcée via CHROME_VERSION_MAIN : %d", chrome_major)
            except ValueError:
                logger.warning(
                    "CHROME_VERSION_MAIN=%r invalide (entier attendu, ex: 151). "
                    "Détection automatique utilisée.",
                    CHROME_VERSION_MAIN,
                )
        if chrome_major is None:
            chrome_major = detect_chrome_major_version()
            if chrome_major:
                logger.info("Chrome installé détecté : version majeure %d", chrome_major)
            else:
                logger.warning(
                    "Version de Chrome non détectée : le dernier ChromeDriver stable "
                    "sera téléchargé. En cas d'erreur de version au lancement, fixe "
                    "CHROME_VERSION_MAIN dans le .env (ex: CHROME_VERSION_MAIN=151)."
                )

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
        try:
            driver = uc.Chrome(
                options=options,
                user_data_dir=PROFILE_DIR,
                use_subprocess=True,
                version_main=chrome_major,
                # tue d'éventuels processus chromedriver zombies qui
                # maintiennent un vieux driver en cache
                patcher_force_close=True,
            )
        except SessionNotCreatedException as exc:
            detail = str(exc).splitlines()[0] if str(exc) else exc
            raise RuntimeError(
                f"Échec du lancement de Chrome : {detail}\n"
                "Cause probable : le ChromeDriver téléchargé ne correspond pas à ta "
                "version de Chrome.\n"
                "Solutions :\n"
                "  1. Ajoute CHROME_VERSION_MAIN=<version majeure> dans le .env "
                "(ouvre chrome://version dans Chrome ; ex: 151.0.7922.76 -> 151) ;\n"
                "  2. Ou vide le cache du driver puis relance (PowerShell) :\n"
                "     Remove-Item \"$env:USERPROFILE\\appdata\\roaming\\undetected_chromedriver\" -Recurse -Force\n"
                "  3. Ou mets Chrome à jour (menu ⋮ > Aide > À propos de Google Chrome)."
            ) from exc
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
    beep_alert(times=12)


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

def already_logged_in(driver) -> bool:
    """
    Détecte si la session est déjà active (profil Chrome persistant).
    Après une première connexion, le site redirige / vers /my-account.
    """
    try:
        url = (driver.current_url or "").lower()
        title = (driver.title or "").strip().lower()
        if "my-account" in url or title in ("my account", "mon compte"):
            return True
    except Exception:
        pass
    return False


def page_diagnostics(driver) -> None:
    """Log titre + taille du HTML + URL pour diagnostiquer une page blanche."""
    try:
        logger.warning(
            "Diagnostic — titre : %r | taille du HTML : %d caractères | URL : %s",
            (driver.title or "").strip(),
            len(driver.page_source or ""),
            driver.current_url,
        )
    except Exception as e:
        logger.warning("Diagnostic impossible : %s", e)


def load_login_page(driver) -> None:
    """Charge la page de connexion, avec rechargement auto si elle sort blanche."""
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        driver.get(BLS_LOGIN_URL)
        sleep_with_jitter(3.0, 0.8)
        try:
            src_len = len((driver.page_source or "").strip())
        except Exception:
            src_len = 0
        title = (driver.title or "").strip()
        logger.info(
            "Page de connexion chargée : %s (titre=%r, HTML=%d caractères)",
            BLS_LOGIN_URL,
            title,
            src_len,
        )
        if src_len > 500:
            return  # la page a du contenu, on continue
        logger.warning(
            "La page semble blanche/vide (tentative %d/%d) — rechargement...",
            attempt,
            max_attempts,
        )
    logger.warning(
        "La page BLS reste blanche après %d essais. Dans la fenêtre Chrome : "
        "appuie sur F5, ou ouvre la console (F12) pour voir l'erreur. "
        "Voir README > Dépannage > « Page blanche ».",
        max_attempts,
    )


def login(driver) -> None:
    load_login_page(driver)

    # Session conservée par le profil Chrome persistant -> inutile de
    # remplir le formulaire (et les champs n'existent d'ailleurs plus).
    if already_logged_in(driver):
        logger.info(
            "Déjà connecté (session conservée par le profil Chrome) — "
            "étape de connexion ignorée."
        )
        return

    if not BLS_EMAIL or not BLS_PASSWORD:
        logger.warning("BLS_EMAIL / BLS_PASSWORD non définis dans le fichier .env.")
        input(">>> Appuie sur Entrée une fois connecté manuellement...")
        return

    email_field = find_email_field(driver)
    password_field = find_password_field(driver)

    if email_field is None or password_field is None:
        if already_logged_in(driver):
            logger.info("Connecté entre-temps — étape de connexion ignorée.")
            return
        logger.warning("Champs introuvables automatiquement. Remplis le formulaire toi-même.")
        page_diagnostics(driver)
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
    """Secours manuel : tu navigues toi-même jusqu'au calendrier."""
    logger.info(
        "Navigation automatique impossible — navigue toi-même dans Chrome "
        "jusqu'au calendrier des créneaux."
    )
    input(">>> Appuie sur Entrée une fois arrivé sur le calendrier des créneaux...")
    appointment_url = driver.current_url
    logger.info("URL des créneaux mémorisée : %s", appointment_url)
    return appointment_url


# ---------- Navigation automatique vers le calendrier ----------

def calendar_is_rendered(driver) -> bool:
    """Vrai si le calendrier (react-day-picker) est présent dans la page."""
    try:
        return bool(
            driver.find_elements(
                By.CSS_SELECTOR, "td[class*='rdp-'], [class*='rdp-month']"
            )
        )
    except Exception:
        return False


def goto_appointment_page(driver, url: str) -> bool:
    """Charge l'URL des créneaux et vérifie que le calendrier s'affiche."""
    try:
        driver.get(url)
    except Exception as e:
        logger.warning("Chargement de la page des créneaux impossible (%s).", e)
        return False
    if wait_for_calendar_render(driver, timeout=8):
        logger.info("Calendrier des créneaux affiché : %s", url)
        return True
    return False


def load_saved_appointment_url():
    """URL du calendrier : variable .env prioritaire, puis fichier mémorisé."""
    if APPOINTMENT_URL:
        return APPOINTMENT_URL
    try:
        with open(APPOINTMENT_URL_FILE, encoding="utf-8") as f:
            url = f.read().strip()
        return url or None
    except OSError:
        return None


def save_appointment_url(url: str) -> None:
    """Mémorise l'URL pour les prochains lancements."""
    try:
        with open(APPOINTMENT_URL_FILE, "w", encoding="utf-8") as f:
            f.write(url)
    except OSError as e:
        logger.debug("Impossible de mémoriser l'URL des créneaux : %s", e)


def click_booking_link(driver) -> bool:
    """
    Cherche et clique un lien/bouton de prise de rendez-vous après la
    connexion (best effort — le site change souvent de structure).
    """
    # 1. Liens/boutons contenant un texte connu
    for text in BOOKING_LINK_TEXTS:
        xpath = (
            f"//a[contains(translate(., "
            f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{text}')] "
            f"| //button[contains(translate(., "
            f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{text}')]"
        )
        try:
            elements = driver.find_elements(By.XPATH, xpath)
        except Exception:
            continue
        for el in elements:
            try:
                el.click()
                sleep_with_jitter(2.5, 0.5)
                if calendar_is_rendered(driver):
                    logger.info("Lien « %s » cliqué — calendrier affiché.", text)
                    return True
                logger.debug("Clic sur « %s » effectué mais pas de calendrier.", text)
            except Exception as e:
                logger.debug("Clic impossible sur « %s » : %s", text, e)

    # 2. Liens dont l'URL contient un mot-clé connu
    for kw in BOOKING_HREF_KEYWORDS:
        try:
            links = driver.find_elements(By.CSS_SELECTOR, f"a[href*='{kw}']")
        except Exception:
            continue
        for el in links:
            try:
                el.click()
                sleep_with_jitter(2.5, 0.5)
                if calendar_is_rendered(driver):
                    logger.info("Lien d'URL contenant « %s » cliqué — calendrier affiché.", kw)
                    return True
            except Exception as e:
                logger.debug("Clic impossible sur le lien « %s » : %s", kw, e)

    return False


def try_auto_navigate(driver):
    """
    Tente d'atteindre le calendrier sans intervention humaine.
    Retourne l'URL en cas de succès, None sinon (-> secours manuel).
    """
    saved = load_saved_appointment_url()
    if saved:
        logger.info("Tentative d'accès direct au calendrier (URL connue)...")
        if goto_appointment_page(driver, saved):
            return saved
        logger.warning(
            "L'URL connue n'affiche pas le calendrier (session expirée ou page déplacée)."
        )

    logger.info("Tentative de clic automatique sur le lien de prise de rendez-vous...")
    if click_booking_link(driver):
        url = driver.current_url
        logger.info("Page des créneaux atteinte : %s", url)
        return url

    return None


def find_available_dates(driver):
    try:
        return driver.find_elements(By.CSS_SELECTOR, AVAILABLE_DAY_CSS)
    except Exception as e:
        logger.debug("Erreur lors de la recherche des jours : %s", e)
        return []


def wait_for_calendar_render(driver, timeout: float = 10) -> bool:
    """
    Attend que le calendrier (react-day-picker) soit rendu dans la page.

    Bien plus réactif qu'un délai fixe : la présence des cellules du
    calendrier est vérifiée en continu, donc un créneau est détecté dès
    qu'il apparaît dans le DOM, pas plusieurs secondes plus tard.
    """
    try:
        WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, "td[class*='rdp-'], [class*='rdp-month']")
            )
        )
        return True
    except TimeoutException:
        return False


def bring_window_to_front(driver) -> None:
    """Tente de remettre la fenêtre Chrome au premier plan (best effort)."""
    try:
        driver.switch_to.window(driver.current_window_handle)
        driver.set_window_position(0, 0)
        driver.maximize_window()
    except Exception as e:
        logger.debug("Impossible de remettre la fenêtre au premier plan : %s", e)


def preselect_first_available_day(driver, available_days, dates_found) -> None:
    """
    Clique sur le premier jour disponible pour afficher directement
    l'écran des créneaux horaires (navigation seulement — le choix du
    créneau et la confirmation de réservation restent manuels).
    """
    if not AUTO_CLICK_FIRST_DAY:
        return
    try:
        first_day = available_days[0]
        driver.execute_script(
            "arguments[0].scrollIntoView({block: 'center'});", first_day
        )
        first_day.click()
        logger.info(
            "Premier jour disponible présélectionné (%s) — les créneaux horaires "
            "sont affichés. Choisis ton créneau et confirme : la réservation "
            "reste manuelle.",
            dates_found[0],
        )
    except Exception as e:
        logger.warning(
            "Présélection automatique impossible (%s) — clique sur le jour "
            "toi-même, il est bien disponible.",
            e,
        )


def refresh_until_slot_appears(driver, appointment_url: str) -> None:
    logger.info(
        "Surveillance active (intervalle moyen ~%.1fs avec jitter). "
        "Dès qu'un créneau apparaît : présélection du jour, fenêtre au "
        "premier plan et alerte sonore.",
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

        # Attend le rendu du calendrier (React) au lieu d'un délai fixe :
        # un créneau est vérifié dès qu'il apparaît, pas ~5 s plus tard.
        rendered = wait_for_calendar_render(driver)
        if not rendered and attempt % 5 == 0:
            logger.warning(
                "Calendrier non détecté à la tentative %d — la page charge "
                "lentement ou le site a changé de structure.",
                attempt,
            )
            page_diagnostics(driver)

        available_days = find_available_dates(driver)

        if attempt % 10 == 0:
            logger.info("Surveillance en cours... (%d vérifications)", attempt)

        if available_days:
            dates_found = [d.get_attribute("data-day") or "Date non précisée" for d in available_days]
            logger.info("Jour(s) disponible(s) détecté(s) : %s", ", ".join(dates_found))

            # Affiche directement l'écran des créneaux horaires :
            # il ne te restera que le choix du créneau + la confirmation.
            preselect_first_available_day(driver, available_days, dates_found)

            bring_window_to_front(driver)
            alert_slot_found()
            logger.info(
                "Le script s'arrête là. Choisis ton créneau et clique "
                "« Réserver » immédiatement dans la fenêtre Chrome."
            )
            break

        page_text = driver.page_source.lower()
        no_slot_text = any(phrase in page_text for phrase in NO_SLOT_PHRASES)
        if not no_slot_text and page_text.strip():
            logger.debug("Aucun jour ni message d'indisponibilité explicite.")

        sleep_with_jitter(REFRESH_INTERVAL_SECONDS, 1.5)


def main():
    logger.info("Préparation du navigateur Chrome...")
    driver = create_driver()

    try:
        wait_until_target_time()
        login(driver)

        # Navigation automatique vers le calendrier (URL mémorisée ou clic
        # sur le lien de prise de rendez-vous), secours manuel sinon.
        appointment_url = try_auto_navigate(driver)
        if appointment_url is None:
            appointment_url = navigate_to_appointment_page(driver)
        save_appointment_url(appointment_url)

        refresh_until_slot_appears(driver, appointment_url)

        input(">>> Appuie sur Entrée quand tu as terminé pour fermer l'assistant...")

    except KeyboardInterrupt:
        logger.info("Arrêt demandé par l'utilisateur.")
    finally:
        logger.info("Script terminé. Le navigateur reste ouvert.")


if __name__ == "__main__":
    main()
