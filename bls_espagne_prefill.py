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
- SCAN DU MOIS SUIVANT : les créneaux s'ouvrent souvent sur le mois suivant,
  qui est vérifié aussi (libellé du mois contrôlé avant lecture du DOM, et
  retour garanti au mois courant quand rien n'y est disponible).
- GARDE-FOU SESSION EXPIRÉE : détection avant ET après le rendu React,
  tentative de reconnexion automatique avec les identifiants du .env, puis
  alerte sonore + invite si un CAPTCHA bloque.
- AUTO-RELANCE DE CHROME : déclenchée par les exceptions, par un driver mort,
  par un calendrier introuvable de façon répétée et par les chargements qui
  dépassent le délai maximal (plafond de relances + alerte au-delà).
- COMPTE À REBOURS + SELF-CHECK : attente précise de l'heure cible,
  vérification complète à T-30 min (session, calendrier, navigateur vivant)
  et contrôle léger à T-2 min.
- TABLEAU DE BORD LOCAL (bls_viewer.py) : compte à rebours, état de la
  session, compteurs et journal, visibles dans le navigateur (lecture seule).
"""

import os
import re
import sys
import time
import random
import logging
import platform
import subprocess
import threading
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

# Les réglages sont lus avant que la journalisation soit initialisée : les
# valeurs invalides sont mémorisées ici puis signalées juste après.
_CONFIG_WARNINGS = []

_TRUE_VALUES = ("1", "true", "yes", "oui", "on")
_FALSE_VALUES = ("0", "false", "no", "non", "off")


def _env_bool(name: str, default: bool) -> bool:
    """Lit un booléen depuis l'environnement (1/0, true/false, oui/non)."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    _CONFIG_WARNINGS.append(
        f"{name}={raw!r} invalide (1 ou 0 attendu) — "
        f"valeur par défaut {'1' if default else '0'} conservée."
    )
    return default


def _env_number(name: str, default, cast, minimum=None, maximum=None):
    """Lit un nombre depuis l'environnement, en le bornant si nécessaire."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = cast(raw.strip())
    except (TypeError, ValueError):
        _CONFIG_WARNINGS.append(
            f"{name}={raw!r} invalide (nombre attendu) — valeur par défaut {default} conservée."
        )
        return default
    if minimum is not None and value < minimum:
        _CONFIG_WARNINGS.append(f"{name}={value} trop petit — ramené à {minimum}.")
        value = cast(minimum)
    if maximum is not None and value > maximum:
        _CONFIG_WARNINGS.append(f"{name}={value} trop grand — ramené à {maximum}.")
        value = cast(maximum)
    return value


def _env_int(name: str, default: int, minimum=None, maximum=None) -> int:
    return _env_number(name, default, int, minimum, maximum)


def _env_float(name: str, default: float, minimum=None, maximum=None) -> float:
    return _env_number(name, default, float, minimum, maximum)


BLS_LOGIN_URL = os.getenv("BLS_LOGIN_URL", "https://algeria.blsinternational.com/")

BLS_EMAIL = os.getenv("BLS_EMAIL", "")
BLS_PASSWORD = os.getenv("BLS_PASSWORD", "")

TARGET_HOUR = _env_int("TARGET_HOUR", 16, minimum=0, maximum=23)
TARGET_MINUTE = _env_int("TARGET_MINUTE", 55, minimum=0, maximum=59)

# Intervalle de base entre les vérifications (en secondes).
# Plancher à 2 s : un rafraîchissement plus agressif fait courir un risque
# réel de blocage du compte (voir README > Limites importantes).
REFRESH_INTERVAL_SECONDS = _env_float(
    "REFRESH_INTERVAL_SECONDS", 5.0, minimum=2.0, maximum=600.0
)

# Version majeure de Chrome (ex: 151). Vide = détection automatique.
# Ouvre chrome://version dans Chrome : le premier nombre est la version
# majeure (ex: 151.0.7922.76 -> 151).
CHROME_VERSION_MAIN = os.getenv("CHROME_VERSION_MAIN", "").strip()

# Présélection automatique du premier jour disponible dès sa détection :
# t'affiche directement l'écran des créneaux horaires. Le choix du créneau
# et la confirmation de réservation restent TOUJOURS manuels.
# AUTO_CLICK_FIRST_DAY=0 pour désactiver.
AUTO_CLICK_FIRST_DAY = _env_bool("AUTO_CLICK_FIRST_DAY", True)

# URL directe de la page du calendrier des créneaux (OPTIONNEL).
# Si vide, le script essaie automatiquement, dans cet ordre :
#   1. l'URL mémorisée au dernier lancement réussi (appointment_url.txt),
#   2. un clic sur le lien « prendre rendez-vous » après connexion,
#   3. et te laisse enfin naviguer manuellement (comportement d'origine).
APPOINTMENT_URL = os.getenv("APPOINTMENT_URL", "").strip()

# ---- Scan du mois suivant -------------------------------------------------
# Les créneaux s'ouvrent souvent pour le mois suivant (le mois affiché est
# déjà saturé). SCAN_NEXT_MONTH=0 pour désactiver.
SCAN_NEXT_MONTH = _env_bool("SCAN_NEXT_MONTH", True)

# Scan du mois suivant une tentative sur N (limite les requêtes au site)
NEXT_MONTH_SCAN_EVERY = _env_int("NEXT_MONTH_SCAN_EVERY", 2, minimum=1, maximum=50)

# ---- Garde-fou « session expirée » ---------------------------------------
# Tente une reconnexion automatique avec BLS_EMAIL/BLS_PASSWORD avant de
# te passer la main (le CAPTCHA, lui, reste toujours à résoudre à la main).
AUTO_RELOGIN_ON_EXPIRY = _env_bool("AUTO_RELOGIN_ON_EXPIRY", True)

# ---- Auto-relance de Chrome ----------------------------------------------
# Échecs de chargement consécutifs avant relance automatique de Chrome
MAX_CONSECUTIVE_FAILURES = _env_int("MAX_CONSECUTIVE_FAILURES", 5, minimum=1, maximum=100)

# Tentatives consécutives sans calendrier rendu avant relance de Chrome
# (page blanche, structure du site changée, onglet planté…)
MAX_NO_CALENDAR_ATTEMPTS = _env_int("MAX_NO_CALENDAR_ATTEMPTS", 6, minimum=1, maximum=100)

# Nombre maximal de relances automatiques avant de te redonner la main
# (évite de relancer Chrome en boucle sur un problème qu'il ne sait pas régler)
MAX_DRIVER_RECOVERIES = _env_int("MAX_DRIVER_RECOVERIES", 3, minimum=1, maximum=50)

# Invites manuelles (« réaffiche le calendrier dans Chrome ») avant de passer
# au dernier recours. Utile quand le calendrier dépend d'un état de
# l'application React : relancer Chrome ne le ferait pas revenir.
MAX_MANUAL_PROMPTS = _env_int("MAX_MANUAL_PROMPTS", 2, minimum=0, maximum=20)

# Délais maximaux (secondes) : sans eux, un chargement « pendu » bloque le
# script indéfiniment et l'auto-relance ne se déclenche jamais.
PAGE_LOAD_TIMEOUT_SECONDS = _env_float("PAGE_LOAD_TIMEOUT_SECONDS", 45.0, minimum=10.0, maximum=300.0)
SCRIPT_TIMEOUT_SECONDS = _env_float("SCRIPT_TIMEOUT_SECONDS", 20.0, minimum=5.0, maximum=120.0)
CALENDAR_RENDER_TIMEOUT = _env_float("CALENDAR_RENDER_TIMEOUT", 10.0, minimum=2.0, maximum=60.0)

# ---- Compte à rebours et self-check --------------------------------------
# Self-check complet (session, calendrier, navigateur) à T-<PREFLIGHT_MINUTES>
PREFLIGHT_MINUTES = _env_int("PREFLIGHT_MINUTES", 30, minimum=1, maximum=240)
# Contrôle léger (navigateur vivant, session) à T-<FINAL_CHECK_MINUTES>
FINAL_CHECK_MINUTES = _env_int("FINAL_CHECK_MINUTES", 2, minimum=0, maximum=30)

# ---- Rafraîchissement de la page (site React / SPA) -----------------------
# Le calendrier BLS n'a pas toujours sa propre URL : il s'affiche après un
# clic, dans une application React. Dans ce cas, recharger l'URL à chaque
# cycle RÉINITIALISE l'application sur la vue précédente et le calendrier
# disparaît (« Calendrier non détecté » en boucle).
#   auto   : le script teste un rechargement puis choisit tout seul (recommandé)
#   reload : rechargement complet à chaque vérification (page avec URL propre)
#   soft   : jamais de rechargement tant que le calendrier est affiché ; les
#            disponibilités sont re-demandées par un aller-retour de mois
REFRESH_MODE = (os.getenv("REFRESH_MODE", "auto").strip().lower() or "auto")
if REFRESH_MODE not in ("auto", "reload", "soft"):
    _CONFIG_WARNINGS.append(
        f"REFRESH_MODE={REFRESH_MODE!r} inconnu (auto, reload ou soft) — « auto » conservé."
    )
    REFRESH_MODE = "auto"

# En mode soft, resynchronisation complète (rechargement + re-navigation)
# toutes les N vérifications. 0 = jamais.
SOFT_RESYNC_EVERY = _env_int("SOFT_RESYNC_EVERY", 20, minimum=0, maximum=500)

# Mots-clés à ne JAMAIS cliquer lors de la recherche du lien de réservation :
# un lien « cancel appointment » contient aussi le mot « appointment ».
DANGEROUS_LINK_WORDS = (
    "cancel",
    "delete",
    "remove",
    "withdraw",
    "reject",
    "decline",
    "annuler",
    "supprimer",
    "resilier",
    "pay",
    "payment",
    "checkout",
    "invoice",
    "logout",
    "log out",
    "sign out",
    "deconnexion",
    "déconnexion",
)

# ---- Tableau de bord local (bls_viewer.py) -------------------------------
VIEWER_ENABLED = _env_bool("VIEWER_ENABLED", True)
VIEWER_HOST = (os.getenv("VIEWER_HOST", "127.0.0.1").strip() or "127.0.0.1")
VIEWER_PORT = _env_int("VIEWER_PORT", 8765, minimum=1, maximum=65535)


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

# Sélecteurs du calendrier react-day-picker
CALENDAR_CSS = "td[class*='rdp-'], [class*='rdp-month']"
MONTH_LABEL_CSS = (
    "[class*='rdp-month'][aria-label], [class*='rdp-caption'], "
    "[class*='rdp-months'] > *, [role='grid'][aria-label]"
)

# Garde-fou « session expirée » : mots-clés d'URL et phrases visibles
LOGIN_URL_KEYWORDS = ("login", "signin", "sign-in", "sign_in", "connexion", "authenticate")
SESSION_EXPIRED_PHRASES = [
    "session expired",
    "session has expired",
    "your session has",
    "please login again",
    "please log in again",
    "sign in again",
    "session expirée",
    "votre session a expiré",
    "veuillez vous reconnecter",
    "connexion expirée",
]

# Détection d'un CAPTCHA (jamais contourné : c'est toujours toi qui le résous)
CAPTCHA_SELECTORS = [
    "iframe[src*='recaptcha']",
    "iframe[src*='hcaptcha']",
    "iframe[title*='challenge' i]",
    ".g-recaptcha",
    "[class*='grecaptcha']",
    "[id*='captcha' i]",
    "[class*='captcha' i]",
    "[data-sitekey]",
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

# Textes supplémentaires testés pour retrouver le calendrier après un
# rechargement (le parcours BLS passe souvent par un bouton d'étape)
BOOKING_STEP_TEXTS = [
    "continue to slot selection",
    "slot selection",
    "select slot",
    "choose slot",
    "continue to booking",
    "book slot",
    "select date",
    "choose date",
    "select appointment date",
    "appointment date",
    "pick a date",
    "book",
    "continue",
    "next",
    "suivant",
    "continuer",
    "choisir une date",
    "sélectionner une date",
    "date de rendez-vous",
]

# ---- Menu déroulant « More actions » (Radix UI) ---------------------------
# Sur /manage-appointments, le calendrier s'atteint en DEUX clics :
#   1. le bouton « More actions » (déclencheur de menu déroulant),
#   2. l'entrée « Continue to slot selection » du menu.
# Cette entrée est un <div role="menuitem"> qui n'existe dans le DOM qu'une
# fois le menu ouvert : la chercher sans ouvrir le menu ne sert à rien.
DROPDOWN_TRIGGER_CSS = (
    "[data-slot='dropdown-menu-trigger'], [aria-haspopup='menu'], "
    "[aria-haspopup='listbox'], button[aria-expanded][data-state]"
)
DROPDOWN_TRIGGER_TEXTS = (
    "more actions",
    "actions",
    "options",
    "more",
    "plus d'actions",
    "actions supplémentaires",
)
MENU_ITEM_CSS = (
    "[data-slot='dropdown-menu-item'], [role='menuitem'], "
    "[role='menuitemradio'], [role='menuitemcheckbox']"
)
# Entrées de menu menant à la sélection de créneau
SLOT_MENU_ITEM_TEXTS = (
    "continue to slot selection",
    "slot selection",
    "select slot",
    "choose slot",
    "continue to booking",
    "book slot",
    "select date",
    "choose date",
    "continuer vers la sélection",
    "sélection de créneau",
    "choisir un créneau",
    "choisir le créneau",
)
# Nombre maximal de déclencheurs « More actions » essayés (un par rendez-vous)
MAX_DROPDOWN_TRIGGERS = _env_int("MAX_DROPDOWN_TRIGGERS", 4, minimum=1, maximum=20)

# Chemins ancrés à l'emplacement du script (indépendants du répertoire
# depuis lequel le script est lancé)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(SCRIPT_DIR, "bls_assistant.log")

# Profil Chrome persistant (cookies, session, cache) — jamais commité (.gitignore)
PROFILE_DIR = os.path.join(SCRIPT_DIR, "chrome_profile")

# Fichier mémorisant l'URL du calendrier du dernier lancement réussi
APPOINTMENT_URL_FILE = os.path.join(SCRIPT_DIR, "appointment_url.txt")

# État publié pour le tableau de bord local (bls_viewer.py) — jamais commité
STATUS_FILE = os.path.join(SCRIPT_DIR, "bls_status.json")

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

for _warning in _CONFIG_WARNINGS:
    logger.warning("Configuration : %s", _warning)


# ---------- Tableau de bord local (optionnel, sans dépendance) ----------

class _NullStatus:
    """Repli si bls_viewer.py est absent : mêmes méthodes, aucun effet."""

    url = ""

    def set(self, **fields):
        return None

    def incr(self, name, amount=1):
        return None

    def reset(self, *names):
        return None

    def event(self, message, level="info"):
        return None

    def slot_found(self, dates, month=""):
        return None

    def flush(self, force=True):
        return None

    def start(self):
        return None

    def stop(self):
        return None


try:
    from bls_viewer import StatusHub  # type: ignore
except Exception as _viewer_exc:  # pragma: no cover - module absent/cassé
    StatusHub = None
    if VIEWER_ENABLED:
        logger.warning(
            "Tableau de bord indisponible (%s) — le script continue sans lui.",
            _viewer_exc,
        )

status = (
    StatusHub(
        enabled=VIEWER_ENABLED,
        host=VIEWER_HOST,
        port=VIEWER_PORT,
        status_file=STATUS_FILE,
        account_email=BLS_EMAIL,
    )
    if StatusHub is not None
    else _NullStatus()
)
status.set(target_time=f"{TARGET_HOUR:02d}:{TARGET_MINUTE:02d}", state="DEMARRAGE")


# ---------- Fonctions utilitaires d'imitation humaine ----------

def jittered_delay(base_seconds: float, variance: float = 1.5) -> float:
    """Durée variable autour de la valeur de base (évite un motif périodique fixe)."""
    return max(1.0, base_seconds + random.uniform(-variance, variance))


def sleep_with_jitter(base_seconds: float, variance: float = 1.5):
    """Attend pendant une durée variable autour de la valeur de base."""
    delay = jittered_delay(base_seconds, variance)
    time.sleep(delay)
    return delay


def format_duration(seconds) -> str:
    """Formate une durée en texte lisible (« 2h05 », « 4 min 30 s », « 45 s »)."""
    try:
        seconds = max(0, int(round(float(seconds))))
    except (TypeError, ValueError):
        return "?"
    if seconds >= 3600:
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}"
    if seconds >= 60:
        return f"{seconds // 60} min {seconds % 60:02d} s"
    return f"{seconds} s"


def safe_title(driver) -> str:
    """Titre de la page courante, sans lever d'exception si le driver est mort."""
    try:
        return (driver.title or "").strip()
    except Exception:
        return ""


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
    configure_driver_timeouts(driver)
    return driver


def configure_driver_timeouts(driver) -> None:
    """
    Borde la durée des chargements et des scripts.

    Sans ces délais, une page qui ne finit jamais de charger bloque le
    script indéfiniment : le compteur d'échecs n'avance pas et
    l'auto-relance de Chrome ne se déclenche jamais.
    """
    try:
        driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT_SECONDS)
    except Exception as e:
        logger.debug("Délai de chargement de page non appliqué : %s", e)
    try:
        driver.set_script_timeout(SCRIPT_TIMEOUT_SECONDS)
    except Exception as e:
        logger.debug("Délai d'exécution de script non appliqué : %s", e)


def driver_is_alive(driver) -> bool:
    """Vrai si le driver répond encore (fenêtre ouverte, session WebDriver valide)."""
    if driver is None:
        return False
    try:
        _ = driver.current_url
        _ = driver.title
        return True
    except Exception:
        return False


def safe_get(driver, url: str) -> bool:
    """
    Charge une URL sans jamais lever d'exception.

    Retourne True si le chargement s'est terminé dans le délai imparti.
    Un dépassement de délai est traité comme un échec (compté pour
    l'auto-relance) plutôt que comme un blocage silencieux.
    """
    try:
        driver.get(url)
        return True
    except TimeoutException:
        logger.warning(
            "Chargement trop lent (> %.0f s) : %s", PAGE_LOAD_TIMEOUT_SECONDS, url
        )
        return False
    except Exception as e:
        logger.warning("Chargement impossible (%s) : %s", e, url)
        return False


def current_url(driver) -> str:
    """URL courante, ou chaîne vide si le driver ne répond pas."""
    try:
        return driver.current_url or ""
    except Exception:
        return ""


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
    status.event("CRÉNEAU DÉTECTÉ — à toi de jouer dans Chrome", "slot")
    beep_alert(times=12)


def run_preflight_check(driver, minutes_before: int = PREFLIGHT_MINUTES):
    """
    Self-check complet avant l'ouverture : navigateur vivant, session
    active, calendrier accessible. Mieux vaut découvrir un problème à
    T-30 min qu'à l'heure critique.

    Retourne (driver, pret) — le driver peut avoir été relancé ici.
    """
    logger.info("=" * 60)
    logger.info(
        "SELF-CHECK (T-%d min) — vérification avant l'ouverture des créneaux",
        minutes_before,
    )
    status.set(state="SELF_CHECK", state_detail=f"T-{minutes_before} min")
    status.event(f"SELF-CHECK (T-{minutes_before} min) lancé")

    session_ok = None
    calendar_ok = None
    message = ""

    # 1. Le navigateur répond-il encore ? (Chrome fermé, veille, crash…)
    if not driver_is_alive(driver):
        logger.warning("SELF-CHECK : Chrome ne répond plus — relance automatique…")
        status.event("SELF-CHECK : Chrome ne répond plus — relance", "warn")
        driver, _url = recover_driver(driver, reason="self-check : Chrome ne répondait plus")

    # 2. La session est-elle toujours active ?
    try:
        if safe_get(driver, BLS_LOGIN_URL):
            sleep_with_jitter(2.5, 0.5)
            if already_logged_in(driver):
                session_ok = True
                logger.info("SELF-CHECK : session toujours active ✔")
                status.event("SELF-CHECK : session toujours active ✔", "ok")
                status.set(session="active")
            elif looks_like_login_page(driver, deep=True):
                session_ok = False
                logger.warning("SELF-CHECK : session NON active — reconnexion nécessaire.")
                status.event("SELF-CHECK : session non active", "warn")
                status.set(session="expirée")
                if AUTO_RELOGIN_ON_EXPIRY and BLS_EMAIL and BLS_PASSWORD:
                    logger.info("SELF-CHECK : tentative de reconnexion automatique…")
                    if login(driver, interactive=False):
                        session_ok = True
                        status.incr("relogins")
                        status.set(session="active")
                        logger.info("SELF-CHECK : reconnexion automatique réussie ✔")
                        status.event("SELF-CHECK : reconnexion automatique réussie ✔", "ok")
            else:
                logger.warning(
                    "SELF-CHECK : état de la session indéterminé (page %r) — "
                    "vérifie la fenêtre Chrome.",
                    safe_title(driver),
                )
                status.event("SELF-CHECK : état de session indéterminé", "warn")
        else:
            session_ok = False
            logger.warning("SELF-CHECK : page de connexion injoignable (réseau ?).")
            status.event("SELF-CHECK : page de connexion injoignable", "error")
    except Exception as e:
        session_ok = False
        logger.warning("SELF-CHECK : vérification de session impossible (%s).", e)
        status.event(f"SELF-CHECK : échec session ({e})", "error")

    if session_ok is False:
        message = "connecte-toi dans Chrome avant l'ouverture"
        beep_alert(times=3)
        logger.warning(
            "SELF-CHECK : %s — connecte-toi MAINTENANT dans la fenêtre Chrome "
            "(email/mot de passe + CAPTCHA si demandé).",
            message,
        )

    # 3. Le calendrier est-il accessible ?
    saved = load_saved_appointment_url()
    if saved:
        try:
            calendar_ok = bool(safe_get(driver, saved)) and wait_for_calendar_render(
                driver, timeout=CALENDAR_RENDER_TIMEOUT
            )
        except Exception as e:
            calendar_ok = False
            logger.warning("SELF-CHECK : test du calendrier impossible (%s).", e)
        if calendar_ok:
            logger.info("SELF-CHECK : calendrier accessible ✔ (%s)", saved)
            status.event("SELF-CHECK : calendrier accessible ✔", "ok")
            status.set(month_displayed=visible_month_label(driver), current_url=saved)
        else:
            logger.warning(
                "SELF-CHECK : calendrier non détecté sur l'URL mémorisée — la "
                "navigation automatique s'en chargera après la connexion."
            )
            status.event("SELF-CHECK : calendrier non détecté", "warn")
            page_diagnostics(driver)
    else:
        logger.info(
            "SELF-CHECK : pas encore d'URL de calendrier mémorisée — le script "
            "y naviguera automatiquement après la connexion."
        )
        status.event("SELF-CHECK : aucune URL de calendrier mémorisée")

    ready = session_ok is not False and calendar_ok is not False
    status.set(
        preflight={
            "done": True,
            "session_ok": session_ok,
            "calendar_ok": calendar_ok,
            "message": message or f"T-{minutes_before} min",
        },
        state="COMPTE_A_REBOURS",
        state_detail="",
    )
    logger.info(
        "SELF-CHECK terminé : session=%s, calendrier=%s → %s",
        {True: "OK", False: "KO", None: "?"}[session_ok],
        {True: "OK", False: "KO", None: "?"}[calendar_ok],
        "prêt" if ready else "ATTENTION, voir les avertissements ci-dessus",
    )
    logger.info("=" * 60)
    return driver, ready


def run_final_check(driver):
    """
    Contrôle léger à T-<FINAL_CHECK_MINUTES> : SANS naviguer (pour ne pas
    quitter le calendrier), vérifie que Chrome répond et que la session
    tient toujours. Retourne le driver (éventuellement relancé).
    """
    if FINAL_CHECK_MINUTES <= 0:
        return driver
    logger.info("CONTRÔLE FINAL (T-%d min)…", FINAL_CHECK_MINUTES)
    status.event(f"Contrôle final (T-{FINAL_CHECK_MINUTES} min)")

    if not driver_is_alive(driver):
        logger.warning("CONTRÔLE FINAL : Chrome ne répond plus — relance automatique…")
        status.event("CONTRÔLE FINAL : Chrome ne répond plus — relance", "warn")
        new_driver, _url = recover_driver(
            driver, reason="contrôle final : Chrome ne répondait plus"
        )
        return new_driver

    if looks_like_login_page(driver, deep=True):
        logger.warning("CONTRÔLE FINAL : session expirée juste avant l'ouverture !")
        status.set(session="expirée", state="SESSION_EXPIREE")
        status.event("CONTRÔLE FINAL : session expirée", "warn")
        beep_alert(times=3)
        if AUTO_RELOGIN_ON_EXPIRY and BLS_EMAIL and BLS_PASSWORD:
            status.set(state="RECONNEXION")
            if login(driver, interactive=False):
                status.incr("relogins")
                status.set(session="active", state="COMPTE_A_REBOURS")
                logger.info("CONTRÔLE FINAL : reconnexion automatique réussie ✔")
                status.event("CONTRÔLE FINAL : reconnexion automatique réussie ✔", "ok")
                return driver
        input(">>> Appuie sur Entrée une fois reconnecté dans Chrome…")
        status.set(session="active", state="COMPTE_A_REBOURS")
    else:
        logger.info("CONTRÔLE FINAL : navigateur vivant, session en place ✔")
        status.event("Contrôle final : tout est prêt ✔", "ok")
    return driver


def wait_until_target_time(driver=None):
    """
    Attend l'heure cible avec compte à rebours, self-check à
    T-<PREFLIGHT_MINUTES> et contrôle léger à T-<FINAL_CHECK_MINUTES>.

    Retourne le driver : il peut avoir été relancé pendant l'attente.
    """
    now = datetime.now()
    target_dt = now.replace(hour=TARGET_HOUR, minute=TARGET_MINUTE, second=0, microsecond=0)

    if now >= target_dt:
        # Heure cible déjà passée aujourd'hui : on surveille tout de suite
        # (comportement voulu — un lancement à 20h pour une cible à 16h55
        # ne doit pas rester bloqué jusqu'au lendemain).
        logger.info(
            "Heure cible déjà passée (il est %02d:%02d, cible %02d:%02d) — "
            "démarrage immédiat de la surveillance, sans compte à rebours.",
            now.hour,
            now.minute,
            TARGET_HOUR,
            TARGET_MINUTE,
        )
        status.event("Heure cible déjà passée — démarrage immédiat", "warn")
        return driver

    wait_window = (target_dt - now).total_seconds()
    logger.info(
        "En attente de %02d:%02d (dans %s)…",
        TARGET_HOUR,
        TARGET_MINUTE,
        format_duration(wait_window),
    )
    status.set(
        state="COMPTE_A_REBOURS",
        target_time=f"{TARGET_HOUR:02d}:{TARGET_MINUTE:02d}",
        target_ts=target_dt.timestamp(),
        target_window_seconds=wait_window,
        seconds_remaining=wait_window,
    )
    status.event(f"En attente de {TARGET_HOUR:02d}:{TARGET_MINUTE:02d}")

    preflight_done = False
    final_check_done = False
    last_countdown_log = None

    while True:
        now = datetime.now()
        remaining = (target_dt - now).total_seconds()
        if remaining <= 0:
            logger.info("Heure cible atteinte (%02d:%02d), on continue.", now.hour, now.minute)
            status.set(seconds_remaining=0)
            status.event("Heure cible atteinte — passage en surveillance", "ok")
            break
        status.set(seconds_remaining=remaining)

        if driver is not None:
            # Self-check complet, une seule fois, à ~T-PREFLIGHT_MINUTES
            if not preflight_done and remaining <= PREFLIGHT_MINUTES * 60:
                preflight_done = True
                driver, _ready = run_preflight_check(driver, PREFLIGHT_MINUTES)

            # Contrôle léger, une seule fois, à ~T-FINAL_CHECK_MINUTES
            if (
                FINAL_CHECK_MINUTES > 0
                and not final_check_done
                and remaining <= FINAL_CHECK_MINUTES * 60
            ):
                final_check_done = True
                driver = run_final_check(driver)

        # Compte à rebours : toutes les 5 min, toutes les minutes dans les
        # 5 dernières, puis toutes les 10 s dans la dernière minute.
        log_interval = 300.0
        if remaining <= 5 * 60:
            log_interval = 60.0
        if remaining <= 60:
            log_interval = 10.0
        if (
            last_countdown_log is None
            or (now - last_countdown_log).total_seconds() >= log_interval
        ):
            last_countdown_log = now
            logger.info(
                "Ouverture des créneaux dans %s — ne ferme ni Chrome ni ce terminal.",
                format_duration(remaining),
            )

        # Sommeil court en fin de compte à rebours pour démarrer à la seconde
        time.sleep(min(5.0, max(0.2, remaining)))

    return driver


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


def looks_like_login_page(driver, deep: bool = False) -> bool:
    """
    Vrai si la page affichée est une page de connexion — le site a
    probablement expiré la session pendant la surveillance.

    Trois signaux, du moins coûteux au plus coûteux :
      1. présence d'un champ mot de passe,
      2. URL redirigée vers /login, /signin, …
      3. (deep=True uniquement) phrase « session expirée » dans le texte
         visible de la page — réservé aux cas où le calendrier ne s'est
         pas affiché, pour ne pas ralentir chaque cycle.
    """
    try:
        if driver.find_elements(By.CSS_SELECTOR, "input[type='password']"):
            return True
    except Exception:
        return False

    try:
        url = (driver.current_url or "").lower()
        if any(keyword in url for keyword in LOGIN_URL_KEYWORDS):
            return True
    except Exception:
        return False

    if not deep:
        return False

    try:
        bodies = driver.find_elements(By.TAG_NAME, "body")
        text = (bodies[0].text if bodies else "").lower()
    except Exception:
        return False
    return any(phrase in text for phrase in SESSION_EXPIRED_PHRASES)


def captcha_detected(driver) -> bool:
    """
    Vrai si un CAPTCHA est affiché. Jamais contourné : sert seulement à
    expliquer pourquoi une reconnexion automatique s'arrête là.
    """
    for selector in CAPTCHA_SELECTORS:
        try:
            if driver.find_elements(By.CSS_SELECTOR, selector):
                return True
        except Exception:
            continue
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


def load_login_page(driver) -> bool:
    """Charge la page de connexion, avec rechargement auto si elle sort blanche."""
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        loaded = safe_get(driver, BLS_LOGIN_URL)
        sleep_with_jitter(3.0, 0.8)
        try:
            src_len = len((driver.page_source or "").strip())
        except Exception:
            src_len = 0
        title = safe_title(driver)
        logger.info(
            "Page de connexion chargée : %s (titre=%r, HTML=%d caractères)",
            BLS_LOGIN_URL,
            title,
            src_len,
        )
        if loaded and src_len > 500:
            status.set(current_url=current_url(driver))
            return True  # la page a du contenu, on continue
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
    status.event(f"Page de connexion blanche après {max_attempts} essais", "error")
    page_diagnostics(driver)
    return False


def wait_for_login_result(driver, timeout: float = 25.0) -> bool:
    """
    Attend la fin de la connexion : disparition du formulaire de connexion
    ou arrivée sur l'espace compte. Retourne True si la session est active.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if already_logged_in(driver):
            return True
        if not looks_like_login_page(driver):
            # Le formulaire a disparu : laisser le temps à la redirection
            sleep_with_jitter(1.5, 0.3)
            return already_logged_in(driver) or not looks_like_login_page(driver, deep=True)
        time.sleep(0.5)
    return already_logged_in(driver)


def login(driver, interactive: bool = True) -> bool:
    """
    Connecte le script à BLS. Retourne True si la session est active.

    interactive=True  : premier lancement — le script t'attend au terminal
                        pour le CAPTCHA/OTP et te rend la main si besoin.
    interactive=False : reconnexion automatique (garde-fou « session
                        expirée », self-check, relance de Chrome). Ne bloque
                        JAMAIS sur une invite : si un CAPTCHA apparaît, il
                        s'arrête et retourne False, et c'est l'alerte sonore
                        qui prend le relais.
    """
    status.set(state="CONNEXION")
    load_login_page(driver)

    # Session conservée par le profil Chrome persistant -> inutile de
    # remplir le formulaire (et les champs n'existent d'ailleurs plus).
    if already_logged_in(driver):
        logger.info(
            "Déjà connecté (session conservée par le profil Chrome) — "
            "étape de connexion ignorée."
        )
        status.set(session="active")
        status.event("Session déjà active (profil Chrome)", "ok")
        return True

    if not interactive and captcha_detected(driver):
        logger.warning(
            "CAPTCHA présent sur la page de connexion — reconnexion automatique "
            "impossible (le CAPTCHA reste à résoudre à la main)."
        )
        status.event("CAPTCHA détecté : reconnexion auto impossible", "warn")
        return False

    if not BLS_EMAIL or not BLS_PASSWORD:
        logger.warning("BLS_EMAIL / BLS_PASSWORD non définis dans le fichier .env.")
        status.set(session="inconnue")
        if interactive:
            input(">>> Appuie sur Entrée une fois connecté manuellement...")
            return already_logged_in(driver)
        status.event("Identifiants absents du .env : connexion auto impossible", "warn")
        return False

    email_field = find_email_field(driver)
    password_field = find_password_field(driver)

    if email_field is None or password_field is None:
        if already_logged_in(driver):
            logger.info("Connecté entre-temps — étape de connexion ignorée.")
            status.set(session="active")
            return True
        logger.warning("Champs introuvables automatiquement. Remplis le formulaire toi-même.")
        page_diagnostics(driver)
        status.event("Champs de connexion introuvables", "warn")
        if interactive:
            input(">>> Appuie sur Entrée une fois connecté manuellement...")
            return already_logged_in(driver)
        return False

    try:
        email_field.clear()
        human_type(email_field, BLS_EMAIL)

        sleep_with_jitter(0.5, 0.2)

        password_field.clear()
        human_type(password_field, BLS_PASSWORD)
        logger.info("Identifiants saisis avec cadence humaine.")
        status.event("Identifiants saisis (cadence humaine)")
    except (ElementNotInteractableException, ElementClickInterceptedException):
        logger.warning(
            "Champs non interactifs (ou masqués par un bandeau). "
            "Saisis tes identifiants à la main."
        )
        if interactive:
            input(">>> Appuie sur Entrée une fois les identifiants saisis...")
        else:
            status.event("Champs non interactifs : saisie auto impossible", "warn")
            return False
    except Exception as e:
        logger.warning("Saisie des identifiants impossible (%s).", e)
        if interactive:
            input(">>> Appuie sur Entrée une fois les identifiants saisis...")
        else:
            status.event(f"Saisie des identifiants impossible ({e})", "warn")
            return False

    # CAPTCHA visible avant l'envoi : en mode interactif on t'attend, en mode
    # automatique on s'arrête là (aucun contournement).
    if captcha_detected(driver):
        if interactive:
            logger.info("CAPTCHA détecté — résous-le dans la fenêtre Chrome.")
            input(">>> Appuie sur Entrée une fois le CAPTCHA résolu...")
        else:
            logger.warning("CAPTCHA/OTP demandé — reconnexion automatique interrompue.")
            status.event("CAPTCHA/OTP demandé : intervention manuelle", "warn")
            return False
    else:
        logger.info("Si un CAPTCHA/OTP est demandé, résous-le manuellement dans Chrome.")

    submitted = False
    login_button = find_login_button(driver)
    if login_button:
        try:
            sleep_with_jitter(0.3, 0.1)
            login_button.click()
            logger.info("Connexion envoyée.")
            submitted = True
        except (ElementNotInteractableException, ElementClickInterceptedException):
            logger.warning("Clique toi-même sur le bouton de connexion.")
            if interactive:
                input(">>> Appuie sur Entrée une fois connecté...")
                return already_logged_in(driver)
    else:
        try:
            password_field.send_keys(Keys.RETURN)
            logger.info("Connexion envoyée via Entrée.")
            submitted = True
        except Exception:
            logger.warning("Clique toi-même sur le bouton de connexion.")
            if interactive:
                input(">>> Appuie sur Entrée une fois connecté...")
                return already_logged_in(driver)

    logged_in = (
        wait_for_login_result(driver, timeout=45.0 if interactive else 25.0)
        if submitted
        else already_logged_in(driver)
    )

    if not logged_in and interactive:
        # OTP par email, CAPTCHA après envoi, site lent… -> la main à l'humain
        logger.warning("Connexion non confirmée automatiquement.")
        input(">>> Appuie sur Entrée une fois connecté dans Chrome...")
        logged_in = already_logged_in(driver)

    sleep_with_jitter(1.5, 0.5)
    if logged_in:
        logger.info("Connexion confirmée — session active.")
        status.set(session="active")
        status.event("Connexion confirmée ✔", "ok")
    else:
        logger.warning("Connexion non confirmée (CAPTCHA, OTP ou site lent).")
        status.set(session="inconnue")
        status.event("Connexion non confirmée", "warn")
    return logged_in


def navigate_to_appointment_page(driver) -> str:
    """
    Secours manuel : tu navigues toi-même jusqu'au calendrier.

    L'URL n'est mémorisée que si le calendrier est RÉELLEMENT détecté :
    sinon le script surveillerait une page (ex: la liste des rendez-vous)
    où aucun calendrier n'apparaît jamais.
    """
    logger.info(
        "Navigation automatique impossible — navigue toi-même dans Chrome "
        "jusqu'au calendrier des créneaux."
    )
    logger.info(
        "Important : va jusqu'à voir le MOIS avec ses jours (pas seulement la "
        "liste de tes rendez-vous)."
    )
    status.event("Navigation manuelle requise — prends la main dans Chrome", "warn")

    appointment_url = current_url(driver)
    for attempt in range(1, 4):
        input(
            ">>> Appuie sur Entrée une fois le calendrier des créneaux affiché "
            f"(essai {attempt}/3)..."
        )
        appointment_url = current_url(driver)
        if calendar_is_rendered(driver):
            logger.info("Calendrier détecté — URL mémorisée : %s", appointment_url)
            status.set(current_url=appointment_url, month_displayed=visible_month_label(driver))
            status.event("Navigation manuelle : calendrier détecté", "ok")
            return appointment_url
        logger.warning(
            "Aucun calendrier détecté sur cette page (titre=%r, URL=%s). "
            "Clique sur l'élément qui affiche le mois et ses jours, "
            "puis appuie à nouveau sur Entrée.",
            safe_title(driver),
            appointment_url,
        )
        calendar_diagnostics(driver)
        status.event("Navigation manuelle : calendrier toujours absent", "warn")

    logger.warning(
        "Aucun calendrier détecté après 3 essais — l'URL %s est mémorisée quand "
        "même. La surveillance démarre et te préviendra si le calendrier "
        "n'apparaît pas.",
        appointment_url,
    )
    status.set(current_url=appointment_url)
    return appointment_url


# ---------- Navigation automatique vers le calendrier ----------

def calendar_is_rendered(driver) -> bool:
    """Vrai si le calendrier (react-day-picker) est présent dans la page."""
    try:
        return bool(driver.find_elements(By.CSS_SELECTOR, CALENDAR_CSS))
    except Exception:
        return False


def goto_appointment_page(driver, url: str) -> bool:
    """Charge l'URL des créneaux et vérifie que le calendrier s'affiche."""
    if not safe_get(driver, url):
        return False
    if wait_for_calendar_render(driver, timeout=8):
        logger.info("Calendrier des créneaux affiché : %s", url)
        status.set(current_url=url, month_displayed=visible_month_label(driver))
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


def is_dangerous_element(element) -> bool:
    """
    Vrai si un lien/bouton ressemble à une action destructrice (annuler un
    rendez-vous, supprimer, payer, se déconnecter…).

    Indispensable : la recherche du lien « prendre rendez-vous » se fait par
    mot-clé, et « cancel appointment » contient aussi le mot « appointment ».
    """
    try:
        text = (element.text or "").lower()
    except Exception:
        text = ""
    try:
        href = (element.get_attribute("href") or "").lower()
    except Exception:
        href = ""
    try:
        label = (element.get_attribute("aria-label") or "").lower()
    except Exception:
        label = ""
    haystack = f"{text} {href} {label}"
    return any(word in haystack for word in DANGEROUS_LINK_WORDS)


def element_label(element) -> str:
    """Libellé lisible d'un élément (texte visible, sinon aria-label)."""
    for getter in (lambda: element.text, lambda: element.get_attribute("aria-label")):
        try:
            value = (getter() or "").strip()
        except Exception:
            return ""
        if value:
            return " ".join(value.split())[:80]
    return ""


def click_element(driver, element) -> bool:
    """
    Clique un élément de façon robuste.

    Les menus Radix (comme « More actions » / « Continue to slot selection »)
    sont rendus dans un portail et peuvent être masqués par un overlay au
    premier essai : on recule donc du clic natif vers un clic après
    recentrage, puis vers un clic JavaScript.
    """
    try:
        element.click()
        return True
    except Exception as first_error:
        logger.debug("Clic natif impossible (%s) — recentrage puis nouvel essai.", first_error)
    try:
        driver.execute_script(
            "arguments[0].scrollIntoView({block: 'center', inline: 'center'});", element
        )
        time.sleep(0.25)
        element.click()
        return True
    except Exception as second_error:
        logger.debug("Clic après recentrage impossible (%s) — clic JavaScript.", second_error)
    try:
        driver.execute_script("arguments[0].click();", element)
        return True
    except Exception as third_error:
        logger.debug("Clic JavaScript impossible : %s", third_error)
    return False


def press_escape(driver) -> None:
    """Referme un menu déroulant ouvert (pour essayer le déclencheur suivant)."""
    try:
        body = driver.find_elements(By.TAG_NAME, "body")
        if body:
            body[0].send_keys(Keys.ESCAPE)
            time.sleep(0.3)
    except Exception as e:
        logger.debug("Fermeture du menu par Échap impossible : %s", e)


def looks_like_action_menu_trigger(element) -> bool:
    """
    Vrai pour un déclencheur de menu d'actions (« More actions »).

    Filtre important : `aria-expanded` + `data-state` se retrouvent aussi sur
    des boutons sans rapport (sélecteur de langue, filtre de liste, accordéon)
    qu'il ne faut surtout pas cliquer à l'aveugle.
    """
    try:
        slot = (element.get_attribute("data-slot") or "").lower()
        if "dropdown-menu-trigger" in slot:
            return True
    except Exception:
        pass
    try:
        haspopup = (element.get_attribute("aria-haspopup") or "").lower()
        if haspopup in ("menu", "true"):
            return True
    except Exception:
        pass

    label = element_label(element).lower()
    if any(word in label for word in DROPDOWN_TRIGGER_TEXTS):
        return True

    # Icône « … » (lucide-ellipsis-vertical / more-vertical / kebab)
    try:
        html = (element.get_attribute("innerHTML") or "").lower()
    except Exception:
        html = ""
    return any(icon in html for icon in ("ellipsis", "more-vertical", "more-horizontal", "kebab"))


def find_dropdown_triggers(driver):
    """
    Boutons ouvrant un menu d'actions (« More actions », actions, options…).

    Un bouton qui n'est clairement pas un menu d'actions (sélecteur de
    langue, filtre…) est écarté, pour ne pas cliquer n'importe quoi.
    """
    candidates = []
    try:
        candidates.extend(driver.find_elements(By.CSS_SELECTOR, DROPDOWN_TRIGGER_CSS))
    except Exception:
        candidates = []

    # Repli : bouton dont le libellé évoque un menu d'actions
    if not candidates:
        try:
            candidates.extend(driver.find_elements(By.CSS_SELECTOR, "button"))
        except Exception:
            return []

    # Dédupliquer (les sélecteurs peuvent se recouper) en conservant l'ordre,
    # puis ne garder que les vrais déclencheurs de menu d'actions
    unique = []
    seen = set()
    for trigger in candidates:
        try:
            key = trigger.id
        except Exception:
            continue
        if key in seen:
            continue
        seen.add(key)
        if looks_like_action_menu_trigger(trigger):
            unique.append(trigger)
        else:
            logger.debug(
                "Bouton « %s » écarté : pas un menu d'actions.",
                element_label(trigger) or "?",
            )
    return unique


def find_slot_menu_item(driver, texts=None):
    """
    Cherche dans un menu ouvert l'entrée menant à la sélection de créneau
    (« Continue to slot selection »). Retourne None si absente.
    """
    wanted = tuple(texts or SLOT_MENU_ITEM_TEXTS)
    try:
        items = driver.find_elements(By.CSS_SELECTOR, MENU_ITEM_CSS)
    except Exception:
        return None
    for item in items:
        label = element_label(item).lower()
        if not label:
            continue
        if is_dangerous_element(item):
            logger.debug("Entrée de menu ignorée (action à risque) : %r", label)
            continue
        if any(text in label for text in wanted):
            return item
    return None


def open_dropdown_and_click_slot_item(driver) -> bool:
    """
    Parcours réel vers le calendrier sur /manage-appointments :
    ouvre le menu « More actions » puis clique « Continue to slot selection ».

    Plusieurs rendez-vous = plusieurs déclencheurs : on les essaie un par un
    (jusqu'à MAX_DROPDOWN_TRIGGERS) jusqu'à ce que le calendrier s'affiche,
    en refermant le menu entre deux essais.
    """
    triggers = find_dropdown_triggers(driver)
    if not triggers:
        logger.debug("Aucun déclencheur de menu déroulant trouvé.")
        return False

    logger.info(
        "%d menu(s) « More actions » trouvé(s) — ouverture et recherche de "
        "« Continue to slot selection »…",
        len(triggers[:MAX_DROPDOWN_TRIGGERS]),
    )
    for index, trigger in enumerate(triggers[:MAX_DROPDOWN_TRIGGERS], start=1):
        trigger_label = element_label(trigger) or "More actions"
        if is_dangerous_element(trigger):
            logger.debug("Déclencheur ignoré (action à risque) : %r", trigger_label)
            continue
        if not click_element(driver, trigger):
            continue
        sleep_with_jitter(1.2, 0.3)

        item = find_slot_menu_item(driver)
        if item is None:
            logger.debug(
                "Menu %d/%d ouvert (%r) mais aucune entrée de sélection de créneau.",
                index,
                len(triggers),
                trigger_label,
            )
            press_escape(driver)
            continue

        item_label = element_label(item) or "Continue to slot selection"
        if not click_element(driver, item):
            press_escape(driver)
            continue
        sleep_with_jitter(2.5, 0.5)

        if calendar_is_rendered(driver):
            logger.info(
                "Parcours réussi : « %s » → « %s » — calendrier affiché.",
                trigger_label,
                item_label,
            )
            status.event(f"Calendrier ouvert via « {trigger_label} » → « {item_label} »", "ok")
            return True

        logger.debug("« %s » cliqué mais pas de calendrier.", item_label)
        press_escape(driver)

    return False


def click_booking_link(driver, extra_texts=None) -> bool:
    """
    Cherche et clique un lien/bouton menant au calendrier des créneaux
    (best effort — le site change souvent de structure).

    Sert aussi à RETROUVER le calendrier après un rechargement qui a
    réinitialisé l'application React sur sa vue précédente.

    Ordre des tentatives (du plus fiable/spécifique au plus générique) :
      1. entrée de menu déjà ouverte (« Continue to slot selection »),
      2. ouverture du menu « More actions » puis clic sur cette entrée,
      3. liens/boutons dont le texte est connu,
      4. liens dont l'URL contient un mot-clé connu.

    Le parcours par menu est testé EN PREMIER : c'est le chemin réel sur BLS,
    il coûte une requête de sélecteurs au lieu d'une trentaine de recherches
    XPath par texte, et il évite de cliquer un lien homonyme au hasard.

    Les éléments dont le texte/l'URL évoque une action destructrice
    (annuler, supprimer, payer, déconnexion) sont systématiquement ignorés.
    """
    texts = list(BOOKING_LINK_TEXTS) + list(extra_texts or [])

    # 1. Entrée de menu déjà ouverte (le menu peut être ouvert à l'écran)
    item = find_slot_menu_item(driver)
    if item is not None:
        label = element_label(item) or "entrée de menu"
        if click_element(driver, item):
            sleep_with_jitter(2.5, 0.5)
            if calendar_is_rendered(driver):
                logger.info("Entrée de menu « %s » cliquée — calendrier affiché.", label)
                status.event(f"Calendrier retrouvé via l'entrée « {label} »", "ok")
                return True

    # 2. Parcours réel BLS : ouvrir « More actions » puis
    #    cliquer « Continue to slot selection »
    if open_dropdown_and_click_slot_item(driver):
        return True

    # 3. Liens/boutons contenant un texte connu
    for text in texts:
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
        for element in elements:
            if is_dangerous_element(element):
                logger.debug("Lien « %s » ignoré (action à risque).", text)
                continue
            try:
                element.click()
            except Exception as e:
                logger.debug("Clic impossible sur « %s » : %s", text, e)
                continue
            sleep_with_jitter(2.5, 0.5)
            if calendar_is_rendered(driver):
                logger.info("Lien « %s » cliqué — calendrier affiché.", text)
                status.event(f"Calendrier retrouvé via « {text} »", "ok")
                return True
            logger.debug("Clic sur « %s » effectué mais pas de calendrier.", text)

    # 4. Liens dont l'URL contient un mot-clé connu
    for keyword in BOOKING_HREF_KEYWORDS:
        try:
            links = driver.find_elements(By.CSS_SELECTOR, f"a[href*='{keyword}']")
        except Exception:
            continue
        for element in links:
            if is_dangerous_element(element):
                logger.debug("Lien d'URL « %s » ignoré (action à risque).", keyword)
                continue
            try:
                element.click()
            except Exception as e:
                logger.debug("Clic impossible sur le lien « %s » : %s", keyword, e)
                continue
            sleep_with_jitter(2.5, 0.5)
            if calendar_is_rendered(driver):
                logger.info("Lien d'URL contenant « %s » cliqué — calendrier affiché.", keyword)
                status.event(f"Calendrier retrouvé via un lien « {keyword} »", "ok")
                return True

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
            status.event("Calendrier atteint via l'URL mémorisée", "ok")
            return saved
        logger.warning(
            "L'URL connue n'affiche pas le calendrier (session expirée ou page déplacée)."
        )
        status.event("URL mémorisée : calendrier non affiché", "warn")

    logger.info("Tentative de clic automatique sur le lien de prise de rendez-vous...")
    if click_booking_link(driver):
        url = current_url(driver)
        logger.info("Page des créneaux atteinte : %s", url)
        status.event("Calendrier atteint via le lien de prise de rendez-vous", "ok")
        status.set(current_url=url)
        return url

    status.event("Navigation automatique impossible", "warn")
    return None


def find_available_dates(driver):
    """Jours disponibles du calendrier (classes `rdp-availability_*`)."""
    try:
        return driver.find_elements(By.CSS_SELECTOR, AVAILABLE_DAY_CSS)
    except Exception as e:
        logger.debug("Erreur lors de la recherche des jours : %s", e)
        return []


def day_label(element) -> str:
    """Libellé lisible d'un jour du calendrier (date ISO de préférence)."""
    for attribute in ("data-day", "aria-label"):
        try:
            value = element.get_attribute(attribute)
        except Exception:
            return "?"
        if value and str(value).strip():
            return str(value).strip()
    try:
        text = (element.text or "").strip()
        if text:
            return text.splitlines()[0]
    except Exception:
        pass
    return "?"


def wait_for_calendar_render(driver, timeout: float = None) -> bool:
    """
    Attend que le calendrier (react-day-picker) soit rendu dans la page.

    Bien plus réactif qu'un délai fixe : la présence des cellules du
    calendrier est vérifiée en continu, donc un créneau est détecté dès
    qu'il apparaît dans le DOM, pas plusieurs secondes plus tard.
    """
    if timeout is None:
        timeout = CALENDAR_RENDER_TIMEOUT
    try:
        WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, CALENDAR_CSS))
        )
        return True
    except TimeoutException:
        return False
    except Exception as e:
        logger.debug("Attente du rendu du calendrier interrompue : %s", e)
        return False


def visible_month_label(driver) -> str:
    """
    Mois actuellement affiché par le calendrier (« septembre 2026 »).

    Sert à vérifier qu'un clic sur « mois suivant » a bien changé de mois
    AVANT de lire les jours disponibles : sinon on lirait le DOM de
    l'ancien mois en croyant scanner le suivant.
    """
    try:
        elements = driver.find_elements(By.CSS_SELECTOR, MONTH_LABEL_CSS)
    except Exception:
        return ""
    for element in elements[:6]:
        for attribute in ("aria-label",):
            try:
                value = (element.get_attribute(attribute) or "").strip()
            except Exception:
                value = ""
            if value:
                return value
        try:
            text = (element.text or "").strip()
        except Exception:
            continue
        if text:
            return text.splitlines()[0].strip()

    # Repli : libellé des boutons de navigation (« Go to next month (septembre 2026) »)
    try:
        buttons = driver.find_elements(By.CSS_SELECTOR, "button[class*='rdp-nav']")
    except Exception:
        return ""
    for button in buttons:
        try:
            label = button.get_attribute("aria-label") or ""
        except Exception:
            continue
        match = re.search(r"\(([^)]{3,})\)", label)
        if match:
            return match.group(1).strip()
    return ""


def wait_month_change(driver, previous_label: str, timeout: float = 4.0) -> str:
    """
    Attend que le mois affiché change après un clic de navigation.
    Retourne le nouveau libellé (identique à l'ancien si rien n'a bougé).
    """
    deadline = time.time() + timeout
    label = previous_label
    while time.time() < deadline:
        label = visible_month_label(driver)
        if label and label != previous_label:
            return label
        time.sleep(0.2)
    return label or previous_label


def month_button_usable(button) -> bool:
    """Vrai si un bouton de navigation de mois est cliquable (non désactivé)."""
    if button is None:
        return False
    try:
        if not button.is_enabled() or not button.is_displayed():
            return False
        for attribute in ("disabled", "aria-disabled"):
            value = button.get_attribute(attribute)
            if value is not None and str(value).lower() not in ("", "false", "none"):
                return False
        return True
    except Exception:
        return False


def find_month_nav_buttons(driver):
    """
    Retourne (bouton mois précédent, bouton mois suivant) du calendrier
    react-day-picker, ou (None, None) s'ils sont introuvables.
    """
    try:
        buttons = driver.find_elements(
            By.CSS_SELECTOR,
            "button[class*='rdp-nav'], button[aria-label*='month' i], "
            "button[aria-label*='mois' i]",
        )
    except Exception:
        return None, None

    prev_btn = next_btn = None
    for btn in buttons:
        try:
            label = (btn.get_attribute("aria-label") or "").lower()
            cls = (btn.get_attribute("class") or "").lower()
        except Exception:
            continue
        if (
            "next" in label
            or "suivant" in label
            or "nav_button_next" in cls
            or "chevron_right" in cls
        ):
            next_btn = btn
        elif (
            "previous" in label
            or "precedent" in label
            or "précédent" in label
            or "nav_button_previous" in cls
            or "chevron_left" in cls
        ):
            prev_btn = btn

    # Filet de sécurité : react-day-picker affiche [précédent, suivant]
    if (prev_btn is None or next_btn is None) and len(buttons) == 2:
        prev_btn, next_btn = buttons[0], buttons[1]
    return prev_btn, next_btn


def restore_month(driver, expected_label: str, attempts: int = 2, use: str = "prev") -> bool:
    """
    Revenir au mois `expected_label` (après un scan du mois suivant ou un
    rafraîchissement SPA).

    Indispensable : si on restait bloqué sur un autre mois, les cycles
    suivants croiraient scanner le mois courant alors qu'ils regardent déjà
    le mois d'après. `use="prev"` clique sur « mois précédent » (retour
    normal après un « suivant ») ; `use="next"` fait l'inverse. Le sens
    opposé est essayé en secours si le bouton voulu est inutilisable.

    Retourne True si le mois de départ est bien réaffiché.
    """
    if not expected_label:
        return True
    for _ in range(max(1, attempts)):
        if visible_month_label(driver) == expected_label:
            return True
        prev_btn, next_btn = find_month_nav_buttons(driver)
        button = prev_btn if use == "prev" else next_btn
        if not month_button_usable(button):
            button = next_btn if use == "prev" else prev_btn
            if not month_button_usable(button):
                return False
        try:
            button.click()
        except Exception as e:
            logger.debug("Retour au mois %r impossible : %s", expected_label, e)
            return False
        sleep_with_jitter(0.7, 0.2)
    return visible_month_label(driver) == expected_label


def scan_next_month(driver):
    """
    Clique sur « mois suivant » et vérifie les créneaux de ce mois.

    Les créneaux s'ouvrent souvent pour le mois suivant (le mois affiché
    étant déjà saturé). Retourne (jours_disponibles, libellé_du_mois) :

    - si des jours y sont disponibles, on RESTE sur le mois suivant (les
      éléments restent valides pour la présélection) ;
    - sinon on revient au mois courant, et ce retour est VÉRIFIÉ via le
      libellé du mois (sinon les cycles suivants seraient décalés).

    Le changement de mois est également vérifié avant la lecture du DOM,
    pour ne jamais attribuer au mois suivant les jours du mois courant.
    """
    base_label = visible_month_label(driver)
    if not SCAN_NEXT_MONTH:
        return [], base_label

    _, next_btn = find_month_nav_buttons(driver)
    if next_btn is None:
        logger.debug("Scan du mois suivant : boutons de navigation introuvables.")
        return [], base_label
    if not month_button_usable(next_btn):
        logger.debug("Scan du mois suivant : bouton « suivant » désactivé (fin de calendrier).")
        return [], base_label

    try:
        next_btn.click()
        # Attendre que le mois ait réellement changé avant de lire les jours
        new_label = wait_month_change(driver, base_label, timeout=4.0)
        if base_label and new_label == base_label:
            logger.debug(
                "Scan du mois suivant : le mois affiché n'a pas changé (%r).",
                base_label,
            )
            sleep_with_jitter(0.8, 0.2)
            new_label = visible_month_label(driver)

        status.incr("next_month_scans")
        status.set(month_scanned=new_label or base_label)
        days = find_available_dates(driver)
        if days:
            logger.info(
                "Créneau(x) détecté(s) sur le MOIS SUIVANT (%s) : %s",
                new_label or "libellé inconnu",
                ", ".join(day_label(d) for d in days),
            )
            status.event(
                f"Mois suivant ({new_label or '?'}) : {len(days)} jour(s) disponible(s)",
                "slot",
            )
            return days, (new_label or base_label)

        logger.debug("Aucun créneau sur le mois suivant (%s).", new_label or "?")
        # Rien au mois suivant -> revenir au mois courant (retour vérifié)
        if not restore_month(driver, base_label):
            logger.warning(
                "Retour au mois courant non confirmé après le scan du mois "
                "suivant — le prochain cycle le corrigera."
            )
    except Exception as e:
        logger.debug("Scan du mois suivant impossible : %s", e)
        status.event(f"Scan du mois suivant impossible ({e})", "warn")
        restore_month(driver, base_label)
    return [], base_label


def page_is_blank(driver) -> bool:
    """Vrai si la page est vide/cassée (et pas seulement dépourvue de calendrier)."""
    try:
        source = driver.page_source or ""
    except Exception:
        return True
    return len(source.strip()) < 500


def calendar_diagnostics(driver) -> None:
    """
    Journalise ce que la page contient réellement quand le calendrier est
    introuvable.

    Sert à distinguer trois situations très différentes : page vide/cassée,
    application React revenue sur sa vue précédente (le calendrier n'a pas
    sa propre URL), ou structure du site changée.
    """
    try:
        logger.warning(
            "Diagnostic calendrier — titre : %r | URL : %s | HTML : %d caractères",
            safe_title(driver),
            current_url(driver),
            len(driver.page_source or ""),
        )
    except Exception as e:
        logger.warning("Diagnostic calendrier impossible : %s", e)
        return

    probes = (
        ("cellules du calendrier (td[class*='rdp-'])", "td[class*='rdp-']"),
        ("blocs react-day-picker ([class*='rdp-'])", "[class*='rdp-']"),
        ("jours désactivés ([data-disabled])", "[data-disabled]"),
        ("calendrier générique ([class*='calendar'])", "[class*='calendar' i]"),
        ("boutons de navigation de mois", "button[class*='rdp-nav']"),
        ("champs mot de passe (page de connexion)", "input[type='password']"),
    )
    for label, selector in probes:
        try:
            count = len(driver.find_elements(By.CSS_SELECTOR, selector))
        except Exception:
            count = -1
        logger.warning("  · %s : %s", label, count)

    try:
        clickables = driver.find_elements(By.CSS_SELECTOR, "button, a[href]")
    except Exception:
        clickables = []
    labels = []
    for element in clickables[:40]:
        try:
            text = (element.text or "").strip().replace("\n", " ")
        except Exception:
            continue
        if text and len(text) < 60:
            labels.append(text)
    if labels:
        logger.warning(
            "  · boutons/liens visibles (40 premiers) : %s",
            " | ".join(list(dict.fromkeys(labels))[:20]),
        )


def soft_refresh_calendar(driver) -> bool:
    """
    Rafraîchit les disponibilités SANS recharger la page.

    Sur une application React où le calendrier n'a pas sa propre URL, un
    `driver.get()` réinitialise l'application et fait disparaître le
    calendrier. Un aller-retour « mois suivant → mois précédent » force au
    contraire react-day-picker à se re-rendre et l'application à re-demander
    les disponibilités, en conservant l'état de navigation.

    Retourne True si le calendrier est toujours affiché après coup.
    """
    base_label = visible_month_label(driver)
    prev_btn, next_btn = find_month_nav_buttons(driver)

    if month_button_usable(next_btn):
        try:
            next_btn.click()
            wait_month_change(driver, base_label, timeout=3.0)
        except Exception as e:
            logger.debug("Rafraîchissement SPA : clic « mois suivant » impossible (%s).", e)
        restore_month(driver, base_label, use="prev")
    elif month_button_usable(prev_btn):
        try:
            prev_btn.click()
            wait_month_change(driver, base_label, timeout=3.0)
        except Exception as e:
            logger.debug("Rafraîchissement SPA : clic « mois précédent » impossible (%s).", e)
        restore_month(driver, base_label, use="next")
    else:
        logger.debug(
            "Rafraîchissement SPA : aucun bouton de mois utilisable — "
            "les disponibilités affichées ne peuvent pas être re-demandées."
        )

    return calendar_is_rendered(driver)


def calibrate_refresh_mode(driver, appointment_url: str) -> str:
    """
    Détermine si un rechargement complet conserve le calendrier.

    Retourne "reload" (l'URL affiche directement le calendrier : on peut
    recharger à chaque vérification) ou "soft" (le calendrier dépend d'un
    état de l'application React : il ne faut PAS recharger, sinon on le
    perd à chaque cycle).
    """
    if REFRESH_MODE != "auto":
        logger.info("Mode de rafraîchissement forcé via REFRESH_MODE=%s.", REFRESH_MODE)
        status.set(refresh_mode=REFRESH_MODE)
        return REFRESH_MODE

    logger.info(
        "Calibrage du rafraîchissement : test d'un rechargement complet de la page…"
    )
    status.event("Calibrage : test d'un rechargement complet")

    if not safe_get(driver, appointment_url):
        logger.warning(
            "Calibrage : rechargement impossible — mode « soft » choisi par précaution."
        )
        status.set(refresh_mode="soft")
        return "soft"

    if wait_for_calendar_render(driver, timeout=CALENDAR_RENDER_TIMEOUT):
        logger.info(
            "Calibrage : le rechargement complet affiche le calendrier — mode « reload »."
        )
        status.event("Mode de rafraîchissement : reload (l'URL affiche le calendrier)", "ok")
        status.set(refresh_mode="reload", month_displayed=visible_month_label(driver))
        return "reload"

    if click_booking_link(driver, extra_texts=BOOKING_STEP_TEXTS):
        # Le parcours de clics peut mener à une autre URL (route interne du
        # calendrier) : la mémoriser pour le prochain lancement.
        reached_url = current_url(driver)
        if reached_url and reached_url != appointment_url:
            logger.info("Calibrage : URL du calendrier découverte : %s", reached_url)
            save_appointment_url(reached_url)
            status.set(current_url=reached_url)

        logger.info(
            "Calibrage : le rechargement fait perdre le calendrier, mais le "
            "parcours de clics le retrouve automatiquement — mode « soft » "
            "(rechargements évités, resynchronisation toutes les %d vérifications).",
            SOFT_RESYNC_EVERY,
        )
        status.event("Mode de rafraîchissement : soft (SPA, parcours de clics connu)", "warn")
        status.set(refresh_mode="soft", month_displayed=visible_month_label(driver))
        return "soft"

    logger.warning(
        "Calibrage : le rechargement fait perdre le calendrier et aucun lien "
        "automatique ne le retrouve — mode « soft ». Le calendrier va devoir "
        "être réaffiché manuellement une fois."
    )
    calendar_diagnostics(driver)
    status.event("Mode de rafraîchissement : soft (SPA, calendrier perdu au rechargement)", "warn")
    status.set(refresh_mode="soft")
    return "soft"


def ensure_calendar_visible(driver, appointment_url: str, interactive: bool = True):
    """
    S'assure que le calendrier est affiché avant/après une perturbation.

    Retourne (calendrier_affiché, url_à_surveiller). Essaie d'abord de le
    retrouver tout seul (liens/boutons de réservation et d'étape), puis —
    en mode interactif — te demande de le réafficher dans Chrome.
    """
    if calendar_is_rendered(driver):
        return True, current_url(driver) or appointment_url

    logger.info("Calendrier absent — tentative de le retrouver automatiquement…")
    if click_booking_link(driver, extra_texts=BOOKING_STEP_TEXTS):
        url = current_url(driver) or appointment_url
        return True, url

    if not interactive:
        return False, appointment_url

    logger.warning("=" * 60)
    logger.warning(
        "LE CALENDRIER A DISPARU de la page (l'application React est revenue "
        "sur sa vue précédente)."
    )
    logger.warning(
        "Dans la fenêtre Chrome : clique à nouveau jusqu'à réafficher le "
        "calendrier des créneaux (le mois avec les jours)."
    )
    logger.warning("=" * 60)
    status.event("Calendrier perdu : navigation manuelle requise", "warn")
    beep_alert(times=3)
    input(">>> Appuie sur Entrée une fois le calendrier réaffiché dans Chrome…")

    if calendar_is_rendered(driver):
        url = current_url(driver) or appointment_url
        logger.info("Calendrier réaffiché — surveillance reprise.")
        status.event("Calendrier réaffiché manuellement — reprise", "ok")
        return True, url

    logger.warning("Toujours aucun calendrier détecté.")
    calendar_diagnostics(driver)
    return False, appointment_url


def bring_window_to_front(driver) -> None:
    """Tente de remettre la fenêtre Chrome au premier plan (best effort)."""
    try:
        driver.switch_to.window(driver.current_window_handle)
        driver.set_window_position(0, 0)
        driver.maximize_window()
    except Exception as e:
        logger.debug("Impossible de remettre la fenêtre au premier plan : %s", e)


def preselect_first_available_day(driver, available_days, dates_found, month_label: str = "") -> None:
    """
    Clique sur le premier jour disponible pour afficher directement
    l'écran des créneaux horaires (navigation seulement — le choix du
    créneau et la confirmation de réservation restent manuels).
    """
    if not AUTO_CLICK_FIRST_DAY:
        return
    where = f" [{month_label}]" if month_label else ""
    try:
        first_day = available_days[0]
        driver.execute_script(
            "arguments[0].scrollIntoView({block: 'center'});", first_day
        )
        first_day.click()
        logger.info(
            "Premier jour disponible présélectionné (%s%s) — les créneaux horaires "
            "sont affichés. Choisis ton créneau et confirme : la réservation "
            "reste manuelle.",
            dates_found[0],
            where,
        )
        status.event(f"Jour présélectionné : {dates_found[0]}{where}", "ok")
    except Exception as e:
        logger.warning(
            "Présélection automatique impossible (%s) — clique sur le jour "
            "toi-même, il est bien disponible.",
            e,
        )
        status.event(f"Présélection impossible ({e}) — clique le jour à la main", "warn")


def quit_driver(driver) -> None:
    """
    Ferme un driver sans risquer de bloquer le script : `quit()` peut rester
    pendu si Chrome est déjà mort, ce qui empêcherait toute relance.
    """
    if driver is None:
        return

    def _quit():
        try:
            driver.quit()
        except Exception:
            pass

    thread = threading.Thread(target=_quit, daemon=True)
    thread.start()
    thread.join(timeout=8)
    if thread.is_alive():
        logger.warning(
            "Fermeture de l'ancien Chrome non confirmée après 8 s — un "
            "processus chrome/chromedriver zombie peut subsister."
        )


def recover_driver(old_driver, appointment_url=None, reason: str = ""):
    """
    Relance Chrome (crash, fermeture accidentelle, chargements pendus,
    calendrier introuvable…). Retourne (nouveau_driver, url_du_calendrier).

    La reconnexion se fait en mode NON interactif : aucune invite bloquante
    pendant une relance automatique, sinon le script resterait pendu à
    attendre une touche alors que personne n'est devant l'écran.
    """
    logger.warning("=" * 60)
    logger.warning(
        "RELANCE AUTOMATIQUE DE CHROME…%s", f" (motif : {reason})" if reason else ""
    )
    logger.warning("=" * 60)
    status.set(state="CHROME_RECOVERY", state_detail=reason)
    status.event(f"Relance de Chrome — {reason}" if reason else "Relance de Chrome", "warn")

    quit_driver(old_driver)
    time.sleep(2)

    driver = create_driver()
    status.incr("recoveries")
    status.event("Chrome relancé — reconnexion en cours", "ok")

    # Session conservée par le profil -> cette étape est généralement sautée
    login(driver, interactive=False)
    url = try_auto_navigate(driver)
    if url:
        save_appointment_url(url)
    else:
        url = appointment_url
    logger.info("Chrome relancé — reprise de la surveillance.")
    status.set(state="SURVEILLANCE", state_detail="")
    return driver, url


def refresh_until_slot_appears(driver, appointment_url: str, refresh_mode: str = None) -> str:
    """
    Boucle de surveillance : rafraîchit la page des créneaux, détecte un jour
    disponible (mois courant ET mois suivant), le présélectionne et alerte.

    `refresh_mode` : "reload" (rechargement complet à chaque vérification),
    "soft" (aucun rechargement tant que le calendrier est affiché — nécessaire
    quand le calendrier dépend d'un état de l'application React) ou None pour
    laisser le script calibrer tout seul.

    Retourne la raison de l'arrêt : "slot" (créneau trouvé).
    """
    mode = refresh_mode
    if mode is None:
        mode = calibrate_refresh_mode(driver, appointment_url)
        # Le calibrage a pu découvrir une URL menant directement au calendrier
        # (parcours « More actions » → créneaux) et la mémoriser sur disque.
        discovered = load_saved_appointment_url()
        if discovered and discovered != appointment_url:
            logger.info("URL du calendrier mise à jour après calibrage : %s", discovered)
            appointment_url = discovered
            status.set(current_url=discovered)

    logger.info(
        "Surveillance active (intervalle moyen ~%.1fs avec jitter, mode %s). "
        "Dès qu'un créneau apparaît : présélection du jour, fenêtre au "
        "premier plan et alerte sonore.",
        REFRESH_INTERVAL_SECONDS,
        mode,
    )
    logger.info(
        "Garde-fous actifs : session expirée (reconnexion auto %s), "
        "relance de Chrome après %d échecs ou %d calendriers manquants "
        "(max %d relances), scan du mois suivant 1 fois sur %d.",
        "activée" if AUTO_RELOGIN_ON_EXPIRY else "désactivée",
        MAX_CONSECUTIVE_FAILURES,
        MAX_NO_CALENDAR_ATTEMPTS,
        MAX_DRIVER_RECOVERIES,
        NEXT_MONTH_SCAN_EVERY,
    )
    if mode == "soft":
        logger.info(
            "Mode « soft » : la page n'est PAS rechargée tant que le calendrier "
            "est affiché (les disponibilités sont re-demandées par un "
            "aller-retour de mois). Ne navigue pas dans Chrome pendant la "
            "surveillance."
        )
    status.set(
        state="SURVEILLANCE",
        state_detail=f"intervalle ~{REFRESH_INTERVAL_SECONDS:.0f} s · mode {mode}",
        current_url=appointment_url,
        refresh_mode=mode,
    )
    status.event(f"Surveillance active (mode {mode})")

    counters = {
        "attempt": 0,
        "failures": 0,
        "no_calendar": 0,
        "recoveries": 0,
        "expiries": 0,
        "manual_prompts": 0,
    }

    def wait_before_next_check() -> None:
        """Pause avec jitter, publiée au tableau de bord (prochain scan)."""
        delay = jittered_delay(REFRESH_INTERVAL_SECONDS, 1.5)
        status.set(next_check_in=delay)
        time.sleep(delay)

    def note_failure(message: str, level: str = "warn") -> None:
        counters["failures"] += 1
        status.set(failures=counters["failures"])
        status.event(message, level)
        logger.warning(
            "%s — échec consécutif %d/%d",
            message,
            counters["failures"],
            MAX_CONSECUTIVE_FAILURES,
        )

    def trigger_recovery(reason: str) -> None:
        """Relance Chrome, avec plafond : au-delà, on te rend la main."""
        nonlocal driver, appointment_url, mode
        counters["recoveries"] += 1
        counters["failures"] = 0
        counters["no_calendar"] = 0
        status.reset("failures")

        if counters["recoveries"] > MAX_DRIVER_RECOVERIES:
            logger.error(
                "%d relances de Chrome successives — le problème n'est pas "
                "transitoire (driver, réseau, ou structure du site changée).",
                counters["recoveries"] - 1,
            )
            logger.error(
                "Interviens : vérifie Chrome/ChromeDriver (README > Dépannage), "
                "ta connexion, puis relance la surveillance."
            )
            status.set(state="ERREUR", state_detail="plafond de relances atteint")
            status.event("Plafond de relances atteint — intervention requise", "error")
            beep_alert(times=8)
            input(">>> Une fois le problème réglé, appuie sur Entrée pour reprendre…")
            counters["recoveries"] = 1
            status.set(state="CHROME_RECOVERY", state_detail="reprise manuelle")

        try:
            new_driver, url = recover_driver(driver, appointment_url, reason=reason)
        except Exception as exc:
            logger.error("Relance de Chrome impossible (%s) — nouvel essai dans 20 s.", exc)
            status.event(f"Relance de Chrome impossible : {exc}", "error")
            beep_alert(times=5)
            time.sleep(20)
            return
        driver = new_driver
        if url:
            appointment_url = url
            status.set(current_url=url)
        # Un nouveau Chrome repart de zéro : recalibrer le rafraîchissement
        mode = calibrate_refresh_mode(driver, appointment_url)

    def handle_expired_session() -> None:
        """
        Garde-fou « session expirée » : reconnexion automatique d'abord,
        alerte sonore + intervention manuelle ensuite (CAPTCHA/OTP).
        """
        nonlocal appointment_url, mode
        counters["expiries"] += 1
        logger.warning("=" * 60)
        logger.warning("SESSION EXPIRÉE — le site t'a déconnecté pendant la surveillance !")
        logger.warning("=" * 60)
        status.set(state="SESSION_EXPIREE", session="expirée")
        status.event(f"Session expirée (n°{counters['expiries']})", "warn")

        def resume() -> None:
            nonlocal appointment_url, mode
            url = try_auto_navigate(driver) or appointment_url
            if url:
                save_appointment_url(url)
                appointment_url = url
            mode = calibrate_refresh_mode(driver, appointment_url)
            counters["no_calendar"] = 0
            counters["expiries"] = 0
            status.set(session="active", state="SURVEILLANCE",
                       state_detail=f"intervalle ~{REFRESH_INTERVAL_SECONDS:.0f} s · mode {mode}")

        if AUTO_RELOGIN_ON_EXPIRY and BLS_EMAIL and BLS_PASSWORD:
            logger.info("Tentative de reconnexion automatique avec les identifiants du .env…")
            status.set(state="RECONNEXION", state_detail="automatique")
            if login(driver, interactive=False):
                status.incr("relogins")
                logger.info("Reconnexion automatique réussie — surveillance reprise.")
                status.event("Reconnexion automatique réussie ✔", "ok")
                beep_alert(times=2)
                resume()
                return
            logger.warning(
                "Reconnexion automatique échouée (CAPTCHA, OTP ou site lent)."
            )
            status.event("Reconnexion auto échouée — intervention manuelle", "warn")

        beep_alert(times=5)
        logger.warning("Reconnecte-toi dans la fenêtre Chrome, puis reviens ici.")
        status.set(state="RECONNEXION", state_detail="manuelle")
        input(">>> Appuie sur Entrée une fois reconnecté dans Chrome…")
        resume()

    while True:
        counters["attempt"] += 1
        attempt = counters["attempt"]
        status.set(checks=attempt)

        # Chrome encore vivant ? (fenêtre fermée, retour de veille, crash)
        if not driver_is_alive(driver):
            logger.warning("Le navigateur ne répond plus (fenêtre fermée ou crash).")
            trigger_recovery("navigateur ne répond plus")
            continue

        # --- Rafraîchissement : rechargement complet OU rafraîchissement SPA ---
        must_reload = (
            mode == "reload"
            or not calendar_is_rendered(driver)
            or bool(SOFT_RESYNC_EVERY)
            and attempt % SOFT_RESYNC_EVERY == 0
        )
        if must_reload:
            if mode == "soft" and attempt > 1:
                logger.info("Resynchronisation complète (rechargement + re-navigation)…")
                status.event("Resynchronisation complète (SPA)")
            if not safe_get(driver, appointment_url):
                note_failure("Erreur de chargement de la page des créneaux")
                if counters["failures"] >= MAX_CONSECUTIVE_FAILURES:
                    trigger_recovery(
                        f"{MAX_CONSECUTIVE_FAILURES} échecs de chargement consécutifs"
                    )
                else:
                    wait_before_next_check()
                continue
            # En mode soft, le rechargement fait perdre le calendrier :
            # rejouer tout de suite le parcours de clics qui y mène
            # (« More actions » → « Continue to slot selection »).
            if mode == "soft" and not calendar_is_rendered(driver):
                restored, restored_url = ensure_calendar_visible(
                    driver, appointment_url, interactive=False
                )
                if restored:
                    appointment_url = restored_url
                    save_appointment_url(restored_url)
                    logger.info("Resynchronisation : calendrier réaffiché automatiquement.")
        else:
            soft_refresh_calendar(driver)

        counters["failures"] = 0
        status.set(
            failures=0,
            last_check_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            current_url=current_url(driver) or appointment_url,
        )

        # Garde-fou session expirée — 1er passage, avant le rendu React
        if looks_like_login_page(driver):
            handle_expired_session()
            continue

        try:
            # Attend le rendu du calendrier (React) au lieu d'un délai fixe :
            # un créneau est vérifié dès qu'il apparaît, pas ~5 s plus tard.
            rendered = wait_for_calendar_render(
                driver, timeout=3.0 if not must_reload else None
            )
            if not rendered:
                # Garde-fou session expirée — 2e passage : la redirection vers
                # la page de connexion arrive parfois APRÈS le rendu initial.
                if looks_like_login_page(driver, deep=True):
                    handle_expired_session()
                    continue

                counters["no_calendar"] += 1
                logger.warning(
                    "Calendrier non détecté (%d/%d) — la page charge lentement, "
                    "l'application est revenue sur sa vue précédente, ou le site "
                    "a changé de structure.",
                    counters["no_calendar"],
                    MAX_NO_CALENDAR_ATTEMPTS,
                )
                status.event(
                    f"Calendrier non détecté ({counters['no_calendar']}/{MAX_NO_CALENDAR_ATTEMPTS})",
                    "warn",
                )
                if counters["no_calendar"] == 1 or counters["no_calendar"] % 3 == 0:
                    calendar_diagnostics(driver)

                # 1) Essayer de retrouver le calendrier sans te déranger
                recovered_calendar, url = ensure_calendar_visible(
                    driver, appointment_url, interactive=False
                )
                if recovered_calendar:
                    logger.info("Calendrier retrouvé automatiquement — surveillance reprise.")
                    status.event("Calendrier retrouvé automatiquement", "ok")
                    appointment_url = url
                    save_appointment_url(url)
                    counters["no_calendar"] = 0
                    continue

                # 2) Au-delà du seuil : page vide -> relance Chrome ;
                #    sinon c'est un état SPA -> on te demande de réafficher le calendrier
                if counters["no_calendar"] >= MAX_NO_CALENDAR_ATTEMPTS:
                    if page_is_blank(driver):
                        trigger_recovery("page vide/cassée et calendrier introuvable")
                        continue

                    counters["manual_prompts"] += 1
                    if counters["manual_prompts"] <= MAX_MANUAL_PROMPTS:
                        logger.warning(
                            "Le calendrier ne revient pas tout seul (%d/%d) — "
                            "relance de Chrome inutile ici, c'est l'état de "
                            "l'application qu'il faut restaurer.",
                            counters["manual_prompts"],
                            MAX_MANUAL_PROMPTS,
                        )
                        recovered_calendar, url = ensure_calendar_visible(
                            driver, appointment_url, interactive=True
                        )
                        if recovered_calendar:
                            appointment_url = url
                            save_appointment_url(url)
                            counters["no_calendar"] = 0
                            mode = calibrate_refresh_mode(driver, appointment_url)
                            continue
                    else:
                        logger.error(
                            "Calendrier introuvable et %d invites manuelles sans "
                            "succès — dernier recours : relance complète de Chrome.",
                            MAX_MANUAL_PROMPTS,
                        )
                        trigger_recovery(
                            f"calendrier introuvable {MAX_NO_CALENDAR_ATTEMPTS} fois "
                            "et navigation manuelle sans effet"
                        )
                        continue
                wait_before_next_check()
                continue

            counters["no_calendar"] = 0
            month_label = visible_month_label(driver)
            status.set(month_displayed=month_label)

            available_days = find_available_dates(driver)
            found_month = month_label

            # Les créneaux s'ouvrent souvent pour le MOIS SUIVANT :
            # le vérifier aussi (une tentative sur NEXT_MONTH_SCAN_EVERY).
            if (
                not available_days
                and SCAN_NEXT_MONTH
                and attempt % NEXT_MONTH_SCAN_EVERY == 0
            ):
                available_days, next_label = scan_next_month(driver)
                if available_days:
                    found_month = next_label

            if attempt % 10 == 0:
                logger.info(
                    "Surveillance en cours… (%d vérifications, mode %s, mois affiché : %s)",
                    attempt,
                    mode,
                    month_label or "inconnu",
                )

            if available_days:
                dates_found = [day_label(day) for day in available_days]
                logger.info(
                    "Jour(s) disponible(s) détecté(s) [%s] : %s",
                    found_month or "mois courant",
                    ", ".join(dates_found),
                )

                # Affiche directement l'écran des créneaux horaires :
                # il ne te restera que le choix du créneau + la confirmation.
                status.slot_found(dates_found, found_month)
                preselect_first_available_day(driver, available_days, dates_found, found_month)

                bring_window_to_front(driver)
                alert_slot_found()
                logger.info(
                    "Le script s'arrête là. Choisis ton créneau et clique "
                    "« Réserver » immédiatement dans la fenêtre Chrome."
                )
                return "slot"

            # Filet de secours : message d'indisponibilité explicite
            try:
                page_text = (driver.page_source or "").lower()
            except Exception:
                page_text = ""
            if page_text and not any(phrase in page_text for phrase in NO_SLOT_PHRASES):
                logger.debug("Aucun jour ni message d'indisponibilité explicite.")
        except Exception as e:
            note_failure(f"Erreur pendant la vérification ({e})")
            if counters["failures"] >= MAX_CONSECUTIVE_FAILURES:
                trigger_recovery("erreurs répétées pendant la vérification")
            else:
                wait_before_next_check()
            continue

        wait_before_next_check()

def main():
    viewer_url = status.start()
    if viewer_url:
        logger.info("Tableau de bord de surveillance (lecture seule) : %s", viewer_url)
        logger.info(
            "Ouvre cette adresse dans ton navigateur pour suivre le compte à "
            "rebours, l'état de la session et les compteurs en direct."
        )
    status.set(state="DEMARRAGE")
    status.event("Préparation du navigateur Chrome…")
    logger.info("Préparation du navigateur Chrome...")

    driver = None
    slot_found = False
    fatal_error = None
    try:
        driver = create_driver()
        status.event("Chrome démarré", "ok")
        status.set(session="inconnue")

        # Compte à rebours + self-check (peut relancer Chrome lui-même)
        driver = wait_until_target_time(driver)

        logged_in = login(driver)
        status.set(session="active" if logged_in else "inconnue")

        # Navigation automatique vers le calendrier (URL mémorisée ou clic
        # sur le lien de prise de rendez-vous), secours manuel sinon.
        status.set(state="NAVIGATION")
        appointment_url = try_auto_navigate(driver)
        if appointment_url is None:
            appointment_url = navigate_to_appointment_page(driver)
        save_appointment_url(appointment_url)
        status.set(
            current_url=appointment_url,
            month_displayed=visible_month_label(driver),
        )

        slot_found = refresh_until_slot_appears(driver, appointment_url) == "slot"

        if slot_found:
            logger.info(
                "La suite se passe dans Chrome : choix du créneau horaire puis "
                "« Réserver » (ces deux clics restent manuels)."
            )
        input(">>> Appuie sur Entrée quand tu as terminé pour fermer l'assistant...")

    except KeyboardInterrupt:
        logger.info("Arrêt demandé par l'utilisateur.")
        status.event("Arrêt demandé (Ctrl+C)", "warn")
    except Exception as exc:
        fatal_error = exc
        logger.exception("Erreur fatale : %s", exc)
        status.set(state="ERREUR", state_detail=str(exc)[:200])
        status.event(f"Erreur fatale : {exc}", "error")
        beep_alert(times=5)
        if not slot_found:
            input(">>> Appuie sur Entrée pour fermer l'assistant...")
    finally:
        # Une erreur fatale ou un créneau trouvé prime sur « Arrêté » :
        # sinon le tableau de bord perdrait l'information utile en fin de course.
        if fatal_error is not None:
            status.set(state="ERREUR", state_detail=str(fatal_error)[:200])
        elif not slot_found:
            status.set(state="ARRET", state_detail="")
        logger.info("Script terminé. Le navigateur reste ouvert.")
        status.flush()
        status.stop()


if __name__ == "__main__":
    main()
