#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tests hors ligne des garde-fous de bls_espagne_prefill.py.

Lancement :  python tests/test_bls.py      (aucun Chrome, aucun réseau)

Selenium, dotenv, undetected-chromedriver et webdriver_manager sont remplacés
par des stubs (tests/stubs), le pilote par un faux (tests/fake_driver.py) et le
temps est virtualisé en injectant dans le module testé un objet `time` et une
classe `datetime` factices : un compte à rebours de 31 min se déroule en
quelques millisecondes, de façon déterministe.

Les fichiers d'état (appointment_url.txt, bls_status.json, journaux) sont
détournés vers un dossier temporaire : le dépôt n'est jamais pollué.
"""

import builtins
import datetime as dt
import importlib
import logging
import os
import sys
import tempfile

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(TESTS_DIR)
WORK_DIR = tempfile.mkdtemp(prefix="bls-tests-")

sys.path.insert(0, os.path.join(TESTS_DIR, "stubs"))
sys.path.insert(0, TESTS_DIR)
sys.path.insert(0, REPO_DIR)

os.environ.update({
    "BLS_STATE_DIR": WORK_DIR,          # journal, URL mémorisée, état JSON
    "BLS_EMAIL": "testeur@example.com",
    "BLS_PASSWORD": "mot-de-passe-test",
    "APPOINTMENT_URL": "https://algeria.blsinternational.com/es/fr/appointment",
    "REFRESH_INTERVAL_SECONDS": "2",
    "VIEWER_ENABLED": "0",
    "TARGET_HOUR": "16",
    "TARGET_MINUTE": "55",
})

builtins.input = lambda prompt="": ""      # aucune invite bloquante en test

from fake_driver import FakeDriver, FakeWait, MONTHS, APPOINTMENT_URL  # noqa: E402

import bls_espagne_prefill as bls  # noqa: E402
from bls_viewer import StatusHub, mask_email  # noqa: E402

# BLS_STATE_DIR a été posé avant l'import : le journal, appointment_url.txt et
# bls_status.json sont déjà écrits dans WORK_DIR, y compris après un reload.
assert bls.LOG_FILE.startswith(WORK_DIR), bls.LOG_FILE
assert bls.APPOINTMENT_URL_FILE.startswith(WORK_DIR), bls.APPOINTMENT_URL_FILE
assert bls.STATUS_FILE.startswith(WORK_DIR), bls.STATUS_FILE

_REAL_DATETIME = dt.datetime


class VirtualClock:
    """Objet `time` factice : sleep() fait avancer l'horloge sans attendre."""

    def __init__(self, start=1_700_000_000.0):
        self.start = start
        self.t = start

    def sleep(self, seconds):
        try:
            self.t += max(0.0, min(float(seconds or 0.0), 600.0))
        except (TypeError, ValueError):
            pass

    def time(self):
        return self.t

    def monotonic(self):
        return self.t


class VirtualDateTime(_REAL_DATETIME):
    """datetime.now() synchronisé sur l'horloge virtuelle du module testé."""

    clock = None

    @classmethod
    def now(cls, tz=None):
        base = _REAL_DATETIME.now(tz)
        if cls.clock is None:
            return base
        return base + dt.timedelta(seconds=cls.clock.t - cls.clock.start)


CLOCK = VirtualClock()
VirtualDateTime.clock = CLOCK


def install_test_environment(module):
    """(Ré)installe stubs et horloge virtuelle après un import/reload."""
    module.time = CLOCK
    module.datetime = VirtualDateTime
    module.WebDriverWait = FakeWait
    module.beep_alert = lambda times=5: None


install_test_environment(bls)

PASSED, FAILED = [], []


def check(label, condition, detail=""):
    if condition:
        PASSED.append(label)
        print(f"  OK    {label}")
    else:
        FAILED.append(f"{label} -> {detail}")
        print(f"  ECHEC {label} -> {detail}")


def fresh_status():
    """Hub d'état réel (sans serveur HTTP) pour vérifier le tableau de bord."""
    hub = StatusHub(enabled=True, host="127.0.0.1", port=1,
                    status_file=os.path.join(WORK_DIR, "bls_status_test.json"),
                    account_email="testeur@example.com")
    bls.status = hub
    return hub


class LogCapture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record.getMessage())

    def text(self):
        return "\n".join(self.records)


def capture_logs():
    handler = LogCapture()
    logger = logging.getLogger("bls_espagne_prefill")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    return handler


print("\n=== 1. Utilitaires purs ===")
check("format_duration heures", bls.format_duration(3725) == "1h02", bls.format_duration(3725))
check("format_duration minutes", bls.format_duration(90) == "1 min 30 s", bls.format_duration(90))
check("format_duration secondes", bls.format_duration(45) == "45 s", bls.format_duration(45))
check("format_duration invalide", bls.format_duration(None) == "?")
check("jittered_delay plancher 1 s", bls.jittered_delay(0.1, 1.5) >= 1.0)
check("sleep_with_jitter retourne le délai", bls.sleep_with_jitter(2, 0.5) >= 1.0)
check("mask_email", mask_email("karim@example.com") == "k****m@example.com", mask_email("karim@example.com"))
check("mask_email court", mask_email("a@b.c") == "a***@b.c", mask_email("a@b.c"))
check("mask_email vide", mask_email("") == "")

print("\n=== 2. Garde-fou « session expirée » : détection ===")
check("page de connexion détectée (shallow)", bls.looks_like_login_page(FakeDriver(page="login")) is True)
check("page calendrier != login", bls.looks_like_login_page(FakeDriver(page="calendar")) is False)

weird = FakeDriver(page="calendar")
weird.body_text = "Votre session a expiré, veuillez vous reconnecter."
weird.current_url = "https://algeria.blsinternational.com/es/fr/appointment"
check("texte seul : shallow = False", bls.looks_like_login_page(weird, deep=False) is False)
check("texte seul : deep = True", bls.looks_like_login_page(weird, deep=True) is True)

redirected = FakeDriver(page="calendar")
redirected.current_url = "https://algeria.blsinternational.com/es/fr/login?next=x"
check("URL /login détectée", bls.looks_like_login_page(redirected) is True)
check("driver mort -> pas d'exception", bls.looks_like_login_page(FakeDriver(alive=False), deep=True) is False)
check("captcha non détecté sur page normale", bls.captcha_detected(FakeDriver(page="login")) is False)

print("\n=== 3. driver_is_alive / safe_get / timeouts ===")
check("driver vivant", bls.driver_is_alive(FakeDriver()) is True)
check("driver mort", bls.driver_is_alive(FakeDriver(alive=False)) is False)
check("driver None", bls.driver_is_alive(None) is False)

timeout_driver = FakeDriver(page="calendar")
timeout_driver.raise_timeout_on_get = 1
check("safe_get : timeout -> False", bls.safe_get(timeout_driver, APPOINTMENT_URL) is False)
check("safe_get : ensuite OK", bls.safe_get(timeout_driver, APPOINTMENT_URL) is True)
check("safe_get : driver mort -> False", bls.safe_get(FakeDriver(alive=False), APPOINTMENT_URL) is False)

timed = FakeDriver(page="calendar")
bls.configure_driver_timeouts(timed)
check("délai de chargement appliqué", getattr(timed, "page_load_timeout", None) == bls.PAGE_LOAD_TIMEOUT_SECONDS,
      getattr(timed, "page_load_timeout", None))
check("délai de script appliqué", getattr(timed, "script_timeout", None) == bls.SCRIPT_TIMEOUT_SECONDS,
      getattr(timed, "script_timeout", None))

print("\n=== 4. Scan du mois suivant ===")
d = FakeDriver(page="calendar", months_available={})
check("mois courant initial", bls.visible_month_label(d) == MONTHS[0], bls.visible_month_label(d))
days, label = bls.scan_next_month(d)
check("mois suivant vide -> aucun jour", days == [])
check("retour au mois courant vérifié", d.month_index == 0, d.month_index)

d2 = FakeDriver(page="calendar", months_available={"octobre 2026": ["2026-10-07", "2026-10-09"]})
days2, label2 = bls.scan_next_month(d2)
check("jours trouvés au mois suivant", len(days2) == 2, len(days2))
check("libellé du mois suivant renvoyé", label2 == "octobre 2026", label2)
check("reste positionné sur le mois suivant", d2.month_index == 1, d2.month_index)
check("day_label lit data-day", bls.day_label(days2[0]) == "2026-10-07", bls.day_label(days2[0]))

d3 = FakeDriver(page="calendar")
d3.month_index = len(MONTHS) - 1
days3, _ = bls.scan_next_month(d3)
check("bouton suivant désactivé -> aucun jour", days3 == [])
check("mois inchangé si bouton désactivé", d3.month_index == len(MONTHS) - 1, d3.month_index)

bls.SCAN_NEXT_MONTH = False
d4 = FakeDriver(page="calendar", months_available={"octobre 2026": ["2026-10-07"]})
days4, _ = bls.scan_next_month(d4)
check("SCAN_NEXT_MONTH=0 -> pas de scan", days4 == [] and d4.month_index == 0)
bls.SCAN_NEXT_MONTH = True

d5 = FakeDriver(page="calendar")
d5.month_index = 2
check("restore_month revient au mois de départ",
      bls.restore_month(d5, MONTHS[0]) is True and d5.month_index == 0, d5.month_index)

hub4 = fresh_status()
bls.scan_next_month(FakeDriver(page="calendar", months_available={"octobre 2026": ["2026-10-01"]}))
check("scan compté au tableau de bord", hub4.snapshot()["next_month_scans"] == 1,
      hub4.snapshot()["next_month_scans"])

print("\n=== 5. Boucle : session expirée -> reconnexion auto -> créneau mois suivant ===")
status_hub = fresh_status()
logs = capture_logs()

d = FakeDriver(page="calendar", logged_in=True,
               months_available={"octobre 2026": ["2026-10-07", "2026-10-14"]})
d.expiry_armed = False


def hook(url, driver):
    if not driver.expiry_armed and "appointment" in url and len(driver.gets) >= 2:
        driver.expiry_armed = True
        driver.logged_in = False
        driver.page = "login"
        driver.month_index = 0
        return
    if url.endswith("algeria.blsinternational.com/"):
        driver.page = "account" if driver.logged_in else "login"
        return
    driver.page = "calendar" if driver.logged_in else "login"


d.on_get = hook
result = bls.refresh_until_slot_appears(d, APPOINTMENT_URL, refresh_mode="reload")
snap = status_hub.snapshot()

check("boucle rend la main sur « slot »", result == "slot", result)
check("session expirée détectée", "SESSION EXPIRÉE" in logs.text())
check("reconnexion automatique tentée", "reconnexion automatique" in logs.text().lower())
check("reconnexion réussie", d.logged_in is True)
check("compteur relogins = 1", snap["relogins"] == 1, snap["relogins"])
check("état final = CRENEAU_TROUVE", snap["state"] == "CRENEAU_TROUVE", snap["state"])
check("jours publiés au tableau de bord", snap["slots_found"] == ["2026-10-07", "2026-10-14"], snap["slots_found"])
check("mois du créneau publié", snap["state_detail"] == "octobre 2026", snap["state_detail"])
check("scans mois suivant >= 1", snap["next_month_scans"] >= 1, snap["next_month_scans"])
check("vérifications comptées", snap["checks"] >= 3, snap["checks"])
check("session repassée à « active »", snap["session"] == "active", snap["session"])
check("présélection du jour effectuée", d._clicked_days == ["2026-10-07"], d._clicked_days)
check("événement « session expirée » au journal",
      any(e["level"] == "warn" and "Session expirée" in e["message"] for e in snap["events"]))
check("email masqué dans l'état", snap["account"] == "t******r@example.com", snap["account"])
check("mot de passe jamais publié", "mot-de-passe-test" not in str(snap))

print("\n=== 5b. AUTO_RELOGIN_ON_EXPIRY=0 -> alerte + intervention manuelle ===")
status_hub_b = fresh_status()
bls.AUTO_RELOGIN_ON_EXPIRY = False
prompts = []
builtins.input = lambda prompt="": (prompts.append(prompt), "")[1]

db = FakeDriver(page="login", logged_in=False, months_available={"septembre 2026": ["2026-09-20"]})
db.manual_done = False


def hook_b(url, driver):
    if url.endswith("algeria.blsinternational.com/"):
        driver.page = "account" if driver.logged_in else "login"
        return
    # La session ne revient qu'une fois l'intervention manuelle simulée
    driver.page = "calendar" if driver.manual_done else "login"


def manual_prompt(prompt=""):
    prompts.append(prompt)
    db.manual_done = True
    db.logged_in = True
    return ""


builtins.input = manual_prompt
db.on_get = hook_b
result_b = bls.refresh_until_slot_appears(db, APPOINTMENT_URL, refresh_mode="reload")
check("créneau trouvé après intervention", result_b == "slot", result_b)
check("invite manuelle affichée", any("reconnecté" in p for p in prompts), prompts)
check("aucune reconnexion auto comptée", status_hub_b.snapshot()["relogins"] == 0, status_hub_b.snapshot()["relogins"])
bls.AUTO_RELOGIN_ON_EXPIRY = True
builtins.input = lambda prompt="": ""

print("\n=== 6. Boucle : calendrier introuvable -> auto-relance + plafond ===")
status_hub2 = fresh_status()
logs2 = capture_logs()
bls.MAX_NO_CALENDAR_ATTEMPTS = 2
bls.MAX_DRIVER_RECOVERIES = 1
bls.MAX_CONSECUTIVE_FAILURES = 2
created = []


def fake_create_driver():
    index = len(created)
    if index < 2:
        drv = FakeDriver(page="blank", logged_in=True)
    else:
        drv = FakeDriver(page="calendar", logged_in=True,
                         months_available={"septembre 2026": ["2026-09-22"]})
    created.append(drv)
    return drv


bls.create_driver = fake_create_driver
bls.quit_driver = lambda driver: None
result2 = bls.refresh_until_slot_appears(FakeDriver(page="blank", logged_in=True), APPOINTMENT_URL, refresh_mode="reload")
snap2 = status_hub2.snapshot()
check("relance aboutit à un créneau", result2 == "slot", result2)
check("Chrome relancé plusieurs fois", len(created) >= 2, len(created))
check("plafond de relances signalé", "relances de Chrome successives" in logs2.text())
check("compteur recoveries >= 1", snap2["recoveries"] >= 1, snap2["recoveries"])
check("état final = CRENEAU_TROUVE", snap2["state"] == "CRENEAU_TROUVE", snap2["state"])
check("créneau du mois courant détecté", snap2["slots_found"] == ["2026-09-22"], snap2["slots_found"])

print("\n=== 7. Boucle : chargements en timeout -> relance ===")
status_hub3 = fresh_status()
bls.MAX_CONSECUTIVE_FAILURES = 2
bls.MAX_NO_CALENDAR_ATTEMPTS = 6
bls.MAX_DRIVER_RECOVERIES = 3
created2 = []


def fake_create_driver2():
    drv = FakeDriver(page="calendar", logged_in=True,
                     months_available={"septembre 2026": ["2026-09-25"]})
    created2.append(drv)
    return drv


bls.create_driver = fake_create_driver2
timeouty = FakeDriver(page="calendar", logged_in=True)
timeouty.raise_timeout_on_get = 99
result3 = bls.refresh_until_slot_appears(timeouty, APPOINTMENT_URL, refresh_mode="reload")
check("relance après timeouts puis créneau", result3 == "slot", result3)
check("nouveau Chrome créé après timeouts", len(created2) >= 1, len(created2))

print("\n=== 8. Boucle : fenêtre Chrome fermée pendant la surveillance ===")
status_hub8 = fresh_status()
logs8 = capture_logs()
created8 = []


def fake_create_driver8():
    drv = FakeDriver(page="calendar", logged_in=True,
                     months_available={"septembre 2026": ["2026-09-28"]})
    created8.append(drv)
    return drv


bls.create_driver = fake_create_driver8
closed = FakeDriver(page="calendar", logged_in=True)
cycle = {"n": 0}


def hook8(url, driver):
    cycle["n"] += 1
    if cycle["n"] == 2:
        driver.alive = False        # l'utilisateur ferme Chrome


closed.on_get = hook8
result8 = bls.refresh_until_slot_appears(closed, APPOINTMENT_URL, refresh_mode="reload")
check("relance après fermeture de Chrome", result8 == "slot", result8)
check("nouveau Chrome créé", len(created8) >= 1, len(created8))
check("motif « ne répond plus » journalisé", "ne répond plus" in logs8.text())

print("\n=== 9. Configuration : bornage des valeurs .env ===")
os.environ["REFRESH_INTERVAL_SECONDS"] = "0.2"
os.environ["MAX_CONSECUTIVE_FAILURES"] = "abc"
os.environ["TARGET_HOUR"] = "99"
os.environ["AUTO_CLICK_FIRST_DAY"] = "peut-etre"
importlib.reload(bls)
install_test_environment(bls)
builtins.input = lambda prompt="": ""
check("intervalle plancher à 2 s", bls.REFRESH_INTERVAL_SECONDS == 2.0, bls.REFRESH_INTERVAL_SECONDS)
check("entier invalide -> défaut 5", bls.MAX_CONSECUTIVE_FAILURES == 5, bls.MAX_CONSECUTIVE_FAILURES)
check("heure hors bornes -> 23", bls.TARGET_HOUR == 23, bls.TARGET_HOUR)
check("booléen invalide -> défaut True", bls.AUTO_CLICK_FIRST_DAY is True)
check("avertissements de configuration émis", len(bls._CONFIG_WARNINGS) >= 4, bls._CONFIG_WARNINGS)

os.environ["REFRESH_INTERVAL_SECONDS"] = "5"
os.environ.pop("MAX_CONSECUTIVE_FAILURES", None)
os.environ["TARGET_HOUR"] = "16"
os.environ.pop("AUTO_CLICK_FIRST_DAY", None)
importlib.reload(bls)
install_test_environment(bls)
builtins.input = lambda prompt="": ""
bls.create_driver = lambda: FakeDriver(page="calendar")
bls.quit_driver = lambda driver: None

print("\n=== 10. Compte à rebours : heure cible déjà passée ===")
status_hub9 = fresh_status()
logs9 = capture_logs()
past = bls.datetime.now() - dt.timedelta(minutes=1)
bls.TARGET_HOUR, bls.TARGET_MINUTE = past.hour, past.minute
sentinel = FakeDriver(page="calendar")
check("retour immédiat (pas de blocage)", bls.wait_until_target_time(sentinel) is sentinel)
check("message « heure cible déjà passée »", "Heure cible déjà passée" in logs9.text())

print("\n=== 10b. Compte à rebours : self-check T-30 min + contrôle final T-2 min ===")
status_hub10 = fresh_status()
logs10 = capture_logs()
bls.PREFLIGHT_MINUTES = 30
bls.FINAL_CHECK_MINUTES = 2
preflight_calls, final_calls = [], []


def fake_preflight(driver, minutes_before=30):
    preflight_calls.append((minutes_before, bls.status.snapshot()))
    return driver, True


def fake_final(driver):
    final_calls.append(bls.status.snapshot())
    return driver


real_preflight = bls.run_preflight_check
real_final = bls.run_final_check
bls.run_preflight_check = fake_preflight
bls.run_final_check = fake_final
target = bls.datetime.now().replace(second=0, microsecond=0) + dt.timedelta(minutes=31)
bls.TARGET_HOUR, bls.TARGET_MINUTE = target.hour, target.minute
same_driver = FakeDriver(page="calendar")
check("driver rendu intact (aucune relance inutile)", bls.wait_until_target_time(same_driver) is same_driver)
check("self-check déclenché une seule fois", len(preflight_calls) == 1, len(preflight_calls))
check("self-check déclenché à T-30 min",
      preflight_calls and abs(preflight_calls[0][1]["seconds_remaining"] - 30 * 60) <= 6,
      preflight_calls[0][1]["seconds_remaining"] if preflight_calls else None)
check("contrôle final déclenché une seule fois", len(final_calls) == 1, len(final_calls))
check("contrôle final déclenché à T-2 min",
      final_calls and abs(final_calls[0]["seconds_remaining"] - 2 * 60) <= 6,
      final_calls[0]["seconds_remaining"] if final_calls else None)
check("état pendant l'attente = COMPTE_A_REBOURS",
      preflight_calls and preflight_calls[0][1]["state"] == "COMPTE_A_REBOURS",
      preflight_calls[0][1]["state"] if preflight_calls else None)
check("cible horaire publiée",
      preflight_calls and preflight_calls[0][1]["target_time"] == f"{bls.TARGET_HOUR:02d}:{bls.TARGET_MINUTE:02d}",
      preflight_calls[0][1]["target_time"] if preflight_calls else None)
check("horodatage cible publié",
      preflight_calls and abs(preflight_calls[0][1]["target_ts"] - target.timestamp()) <= 60,
      preflight_calls[0][1]["target_ts"] if preflight_calls else None)
check("fenêtre de progression publiée (~31 min)",
      preflight_calls and 30 * 60 < preflight_calls[0][1]["target_window_seconds"] <= 32 * 60,
      preflight_calls[0][1]["target_window_seconds"] if preflight_calls else None)
snap10 = status_hub10.snapshot()
check("compte à rebours remis à 0 à l'ouverture", snap10["seconds_remaining"] == 0, snap10["seconds_remaining"])
check("heure cible atteinte journalisée", "Heure cible atteinte" in logs10.text())
check("compte à rebours journalisé plusieurs fois", logs10.text().count("Ouverture des créneaux dans") >= 5,
      logs10.text().count("Ouverture des créneaux dans"))

print("\n=== 10c. Seuils configurables : PREFLIGHT_MINUTES=1, FINAL_CHECK_MINUTES=0 ===")
status_hub11 = fresh_status()
preflight_calls.clear()
final_calls.clear()
bls.PREFLIGHT_MINUTES = 1
bls.FINAL_CHECK_MINUTES = 0
target2 = bls.datetime.now().replace(second=0, microsecond=0) + dt.timedelta(minutes=3)
bls.TARGET_HOUR, bls.TARGET_MINUTE = target2.hour, target2.minute
bls.wait_until_target_time(FakeDriver(page="calendar"))
check("self-check déclenché au seuil configuré (T-1 min)", len(preflight_calls) == 1, len(preflight_calls))
check("self-check NON déclenché à T-3 min",
      preflight_calls and preflight_calls[0][0] == 1
      and abs(preflight_calls[0][1]["seconds_remaining"] - 60) <= 6,
      preflight_calls[0] if preflight_calls else None)
check("aucun contrôle final si FINAL_CHECK_MINUTES=0", final_calls == [], final_calls)
bls.PREFLIGHT_MINUTES = 30
bls.FINAL_CHECK_MINUTES = 2
bls.run_preflight_check = real_preflight
bls.run_final_check = real_final

print("\n=== 11. Self-check réel : Chrome mort -> relance ; vivant -> rien ===")
status_hub6 = fresh_status()
logs6 = capture_logs()
created3 = []


def fake_create_driver3():
    drv = FakeDriver(page="calendar", logged_in=True)
    drv.on_get = lambda url, d: setattr(d, "page", "account" if url.endswith(".com/") else "calendar")
    created3.append(drv)
    return drv


bls.create_driver = fake_create_driver3
new_driver, ready = bls.run_preflight_check(FakeDriver(page="calendar", alive=False), minutes_before=30)
check("Chrome relancé pendant le self-check", len(created3) == 1, len(created3))
check("self-check retourne le nouveau driver", bool(created3) and new_driver is created3[0])
check("relance journalisée", "ne répond plus" in logs6.text())
snap6 = status_hub6.snapshot()
check("preflight publié au tableau de bord", snap6["preflight"]["done"] is True, snap6["preflight"])
check("preflight : session testée", snap6["preflight"]["session_ok"] is not None, snap6["preflight"])

logs7 = capture_logs()
alive = FakeDriver(page="calendar", logged_in=True)
alive.on_get = lambda url, drv: setattr(drv, "page", "account" if url.endswith(".com/") else "calendar")
driver_out, ready2 = bls.run_preflight_check(alive, minutes_before=30)
check("aucune relance quand Chrome répond", len(created3) == 1, len(created3))
check("self-check retourne le même driver", driver_out is alive)
check("self-check : session active détectée", "session toujours active" in logs7.text())
check("self-check : calendrier accessible", "calendrier accessible" in logs7.text())
snap7 = status_hub6.snapshot()
check("preflight session_ok=True", snap7["preflight"]["session_ok"] is True, snap7["preflight"])
check("preflight calendar_ok=True", snap7["preflight"]["calendar_ok"] is True, snap7["preflight"])
check("self-check conclut « prêt »", ready2 is True, ready2)

logs8b = capture_logs()
out2 = bls.run_final_check(FakeDriver(page="calendar", alive=False))
check("contrôle final : Chrome relancé", len(created3) == 2, len(created3))
check("contrôle final : nouveau driver retourné", out2 is created3[-1])

bls.FINAL_CHECK_MINUTES = 0
same = FakeDriver(page="calendar")
check("contrôle final désactivé -> driver inchangé", bls.run_final_check(same) is same)
check("contrôle final désactivé -> aucune relance", len(created3) == 2, len(created3))
bls.FINAL_CHECK_MINUTES = 2

print("\n=== 11b. Contrôle final : session expirée à T-2 min -> reconnexion auto ===")
status_hub12 = fresh_status()
expired_final = FakeDriver(page="login", logged_in=False)
expired_final.on_get = lambda url, drv: setattr(drv, "page", "account" if drv.logged_in else "login")
out3 = bls.run_final_check(expired_final)
check("contrôle final : reconnexion automatique", expired_final.logged_in is True)
check("contrôle final : relogins compté", status_hub12.snapshot()["relogins"] == 1, status_hub12.snapshot()["relogins"])
check("contrôle final : driver conservé", out3 is expired_final)

print("\n=== 12. Connexion non interactive (reconnexion auto) ===")
status_hub13 = fresh_status()
interactive_prompts = []
builtins.input = lambda prompt="": (interactive_prompts.append(prompt), "")[1]

login_driver = FakeDriver(page="login", logged_in=False)
login_driver.on_get = lambda url, drv: setattr(drv, "page", "account" if drv.logged_in else "login")
check("connexion automatique réussie", bls.login(login_driver, interactive=False) is True)
check("connexion auto : aucune invite bloquante", interactive_prompts == [], interactive_prompts)
check("session publiée « active »", status_hub13.snapshot()["session"] == "active", status_hub13.snapshot()["session"])

check("champs introuvables -> False", bls.login(FakeDriver(page="blank", logged_in=False), interactive=False) is False)
check("champs introuvables -> aucune invite", interactive_prompts == [], interactive_prompts)

saved_email, saved_password = bls.BLS_EMAIL, bls.BLS_PASSWORD
bls.BLS_EMAIL, bls.BLS_PASSWORD = "", ""
check("sans identifiants -> False", bls.login(FakeDriver(page="login", logged_in=False), interactive=False) is False)
check("sans identifiants -> aucune invite", interactive_prompts == [], interactive_prompts)
bls.BLS_EMAIL, bls.BLS_PASSWORD = saved_email, saved_password
builtins.input = lambda prompt="": ""

check("session déjà active -> True", bls.login(FakeDriver(page="account", logged_in=True), interactive=False) is True)

print("\n=== 13. Rafraîchissement SPA : calibrage ===")
status_hub14 = fresh_status()
logs14 = capture_logs()

# 13.a — l'URL affiche directement le calendrier -> mode "reload"
reloadable = FakeDriver(page="calendar", logged_in=True)
mode_a = bls.calibrate_refresh_mode(reloadable, APPOINTMENT_URL)
check("URL avec calendrier -> mode reload", mode_a == "reload", mode_a)
check("mode publié au tableau de bord", status_hub14.snapshot()["refresh_mode"] == "reload",
      status_hub14.snapshot()["refresh_mode"])

# 13.b — le rechargement fait perdre le calendrier (SPA) -> mode "soft"
spa = FakeDriver(page="calendar", logged_in=True)
spa.gets_count = {"n": 0}


def spa_hook(url, driver):
    driver.gets_count["n"] += 1
    # 1er chargement : calendrier ; tout rechargement suivant : vue liste
    driver.page = "calendar" if driver.gets_count["n"] == 1 else "list"


spa.on_get = spa_hook
spa.get(APPOINTMENT_URL)          # le calendrier est affiché (navigation manuelle)
mode_b = bls.calibrate_refresh_mode(spa, APPOINTMENT_URL)
check("SPA (calendrier perdu au rechargement) -> mode soft", mode_b == "soft", mode_b)
check("calibrage journalisé", "Calibrage" in logs14.text())

# 13.c — REFRESH_MODE forcé
bls.REFRESH_MODE = "reload"
check("REFRESH_MODE=reload forcé", bls.calibrate_refresh_mode(spa, APPOINTMENT_URL) == "reload")
bls.REFRESH_MODE = "soft"
check("REFRESH_MODE=soft forcé", bls.calibrate_refresh_mode(reloadable, APPOINTMENT_URL) == "soft")
bls.REFRESH_MODE = "auto"

print("\n=== 14. Mode soft : AUCUN rechargement tant que le calendrier est affiché ===")
status_hub15 = fresh_status()
logs15 = capture_logs()
soft = FakeDriver(page="calendar", logged_in=True,
                  months_available={"septembre 2026": ["2026-09-30"]})
soft.on_get = lambda url, drv: setattr(drv, "page", "calendar")
gets_before = len(soft.gets)
result15 = bls.refresh_until_slot_appears(soft, APPOINTMENT_URL, refresh_mode="soft")
check("créneau trouvé en mode soft", result15 == "slot", result15)
check("page rechargée au plus une fois (calibrage absent)",
      len(soft.gets) - gets_before <= 1, len(soft.gets) - gets_before)
check("mois conservé après rafraîchissement SPA", soft.month_index == 0, soft.month_index)
check("mode soft publié", status_hub15.snapshot()["refresh_mode"] == "soft",
      status_hub15.snapshot()["refresh_mode"])

print("\n=== 15. Mode soft : calendrier perdu puis retrouvé par un bouton ===")
status_hub16 = fresh_status()
logs16 = capture_logs()
spa2 = FakeDriver(page="calendar", logged_in=True,
                  months_available={"septembre 2026": ["2026-09-29"]})
spa2.links = [{"text": "Book Appointment", "href": "/book", "page": "calendar"}]
state16 = {"n": 0}


def spa2_hook(url, driver):
    state16["n"] += 1
    driver.page = "calendar" if state16["n"] == 1 else "list"


spa2.on_get = spa2_hook
spa2.get(APPOINTMENT_URL)                     # calendrier affiché (1re fois)
spa2.page = "list"                            # ...puis perdu (retour vue liste)
result16 = bls.refresh_until_slot_appears(spa2, APPOINTMENT_URL, refresh_mode="soft")
check("calendrier retrouvé automatiquement", result16 == "slot", result16)
check("retrouvé via le bouton « Book Appointment »",
      "Calendrier retrouvé automatiquement" in logs16.text()
      or "Resynchronisation : calendrier réaffiché automatiquement" in logs16.text(),
      logs16.text()[-200:])
check("aucune invite manuelle nécessaire", "réaffiché dans Chrome" not in logs16.text())

print("\n=== 16. Liens dangereux ignorés (cancel / delete / pay) ===")
dangerous = FakeDriver(page="list", logged_in=True)
dangerous.links = [
    {"text": "Cancel Appointment", "href": "/cancel-appointment", "page": "blank"},
    {"text": "Delete booking", "href": "/delete", "page": "blank"},
    {"text": "Pay now", "href": "/payment", "page": "blank"},
]
check("lien « cancel » détecté comme dangereux",
      bls.is_dangerous_element(dangerous._link_element(dangerous.links[0])) is True)
check("lien « pay » détecté comme dangereux",
      bls.is_dangerous_element(dangerous._link_element(dangerous.links[2])) is True)
safe_link = FakeDriver(page="list")._link_element({"text": "Book Appointment", "href": "/book"})
check("lien « book » non dangereux", bls.is_dangerous_element(safe_link) is False)
clicked = bls.click_booking_link(dangerous)
check("aucun lien dangereux cliqué", clicked is False and dangerous.page == "list", dangerous.page)
check("tous les liens dangereux non cliqués",
      all(link.get("_clicked") is None for link in dangerous.links))

mixed = FakeDriver(page="list", logged_in=True)
mixed.links = [
    {"text": "Cancel Appointment", "href": "/cancel", "page": "blank"},
    {"text": "Book Appointment", "href": "/book", "page": "calendar"},
]
check("le bon lien est cliqué malgré un lien dangereux",
      bls.click_booking_link(mixed) is True and mixed.page == "calendar", mixed.page)

print("\n=== 17. Navigation manuelle : URL mémorisée seulement si calendrier vu ===")
status_hub17 = fresh_status()
logs17 = capture_logs()
manual = FakeDriver(page="list", logged_in=True)
manual.on_get = lambda url, drv: setattr(drv, "page", "list")
manual.current_url = "https://algeria.blsinternational.com/manage-appointments"
enter_count = {"n": 0}


def manual_input(prompt=""):
    enter_count["n"] += 1
    if enter_count["n"] >= 2:
        manual.page = "calendar"      # l'utilisateur clique enfin jusqu'au calendrier
    return ""


builtins.input = manual_input
url17 = bls.navigate_to_appointment_page(manual)
builtins.input = lambda prompt="": ""
check("deux invites nécessaires", enter_count["n"] == 2, enter_count["n"])
check("avertissement « aucun calendrier détecté »", "Aucun calendrier détecté" in logs17.text())
check("URL mémorisée une fois le calendrier vu", url17 == "https://algeria.blsinternational.com/manage-appointments", url17)
check("diagnostic riche journalisé", "Diagnostic calendrier" in logs17.text())

# Cas où le calendrier n'est jamais atteint : 3 essais puis mémorisation forcée
manual2 = FakeDriver(page="list", logged_in=True)
manual2.on_get = lambda url, drv: setattr(drv, "page", "list")
manual2.current_url = "https://algeria.blsinternational.com/manage-appointments"
builtins.input = lambda prompt="": ""
url17b = bls.navigate_to_appointment_page(manual2)
check("3 essais puis mémorisation forcée", url17b == "https://algeria.blsinternational.com/manage-appointments")
check("message « après 3 essais »", "après 3 essais" in logs17.text())

print("\n=== 18. Rafraîchissement SPA : aller-retour de mois ===")
nudge = FakeDriver(page="calendar", logged_in=True)
check("soft_refresh_calendar garde le calendrier", bls.soft_refresh_calendar(nudge) is True)
check("soft_refresh_calendar revient au mois de départ", nudge.month_index == 0, nudge.month_index)

nudge2 = FakeDriver(page="calendar", logged_in=True)
nudge2.month_index = len(MONTHS) - 1        # bouton « suivant » désactivé
check("soft_refresh_calendar avec suivant désactivé", bls.soft_refresh_calendar(nudge2) is True)
check("mois final inchangé (dernier mois)", nudge2.month_index == len(MONTHS) - 1, nudge2.month_index)

nudge3 = FakeDriver(page="list", logged_in=True)
check("soft_refresh_calendar sur page sans calendrier -> False", bls.soft_refresh_calendar(nudge3) is False)

print("\n=== 19. Parcours réel BLS : « More actions » -> « Continue to slot selection » ===")
status_hub19 = fresh_status()
logs19 = capture_logs()

# 19.a — le menu contient une action dangereuse ET l'entrée attendue
spa_menu = FakeDriver(page="list", logged_in=True)
spa_menu.dropdowns = [{
    "trigger": "More actions",
    "items": ["Cancel Appointment", "Continue to slot selection"],
    "page": "calendar",
}]
check("entrée de menu introuvable avant ouverture", bls.find_slot_menu_item(spa_menu) is None)
check("déclencheur « More actions » trouvé", len(bls.find_dropdown_triggers(spa_menu)) == 1,
      len(bls.find_dropdown_triggers(spa_menu)))
check("parcours en 2 clics réussi", bls.open_dropdown_and_click_slot_item(spa_menu) is True)
check("calendrier affiché après le parcours", spa_menu.page == "calendar", spa_menu.page)
check("« Cancel Appointment » NON cliqué", spa_menu.page != "blank" and "Cancel" not in logs19.text().split("ignorée")[-1][:60])

# 19.b — menu sans entrée de créneau : rien ne se passe, menu refermé
no_slot_menu = FakeDriver(page="list", logged_in=True)
no_slot_menu.dropdowns = [{"trigger": "More actions", "items": ["Cancel Appointment", "View details"], "page": "blank"}]
check("menu sans entrée de créneau -> False", bls.open_dropdown_and_click_slot_item(no_slot_menu) is False)
check("page inchangée", no_slot_menu.page == "list", no_slot_menu.page)
check("menu refermé après échec", no_slot_menu.dropdowns[0].get("open") is False)
check("entrée dangereuse ignorée", bls.find_slot_menu_item(no_slot_menu) is None)

# 19.c — plusieurs rendez-vous : le 1er menu n'a pas l'entrée, le 2e si
multi = FakeDriver(page="list", logged_in=True)
multi.dropdowns = [
    {"trigger": "More actions", "items": ["View details"], "page": "blank"},
    {"trigger": "More actions", "items": ["Continue to slot selection"], "page": "calendar"},
]
check("le 2e déclencheur est essayé", bls.open_dropdown_and_click_slot_item(multi) is True)
check("calendrier affiché via le 2e menu", multi.page == "calendar", multi.page)

# 19.d — click_booking_link intègre le parcours (étape 4)
via_booking = FakeDriver(page="list", logged_in=True)
via_booking.dropdowns = [{"trigger": "More actions", "items": ["Continue to slot selection"], "page": "calendar"}]
check("click_booking_link passe par le menu déroulant", bls.click_booking_link(via_booking) is True)
check("calendrier affiché via click_booking_link", via_booking.page == "calendar", via_booking.page)

# 19.e — boucle complète en mode soft : rechargement -> parcours rejoué seul
status_hub20 = fresh_status()
logs20 = capture_logs()
bls.MAX_NO_CALENDAR_ATTEMPTS = 6
spa_full = FakeDriver(page="calendar", logged_in=True,
                      months_available={"septembre 2026": ["2026-09-26"]})
spa_full.dropdowns = [{"trigger": "More actions", "items": ["Cancel Appointment", "Continue to slot selection"], "page": "calendar"}]
state20 = {"n": 0}


def spa_full_hook(url, driver):
    state20["n"] += 1
    # 1er chargement : calendrier déjà là ; tout rechargement : retour à la liste
    driver.page = "calendar" if state20["n"] == 1 else "list"


spa_full.on_get = spa_full_hook
spa_full.get(APPOINTMENT_URL)                    # l'utilisateur a atteint le calendrier
mode20 = bls.calibrate_refresh_mode(spa_full, APPOINTMENT_URL)
check("calibrage : SPA -> mode soft", mode20 == "soft", mode20)
result20 = bls.refresh_until_slot_appears(spa_full, APPOINTMENT_URL, refresh_mode=mode20)
check("créneau trouvé après resynchronisation", result20 == "slot", result20)
check("aucune invite manuelle dans le parcours SPA", "réaffiché dans Chrome" not in logs20.text())
check("aucune relance de Chrome inutile", status_hub20.snapshot()["recoveries"] == 0,
      status_hub20.snapshot()["recoveries"])
check("« Cancel Appointment » jamais cliqué pendant la boucle", spa_full.page == "calendar", spa_full.page)

print("\n=== 19f. Boutons aria-expanded parasites ignorés ===")
d19f = FakeDriver(page="list", logged_in=True)
d19f.dropdowns = [
    # Sélecteur de langue : aria-expanded + data-state, mais PAS un menu d'actions
    {"trigger": "English", "items": ["English", "Español"], "page": "blank",
     "radix": False},
    # Le vrai menu d'actions du rendez-vous
    {"trigger": "More actions",
     "items": ["Cancel Appointment", "Continue to slot selection"],
     "page": "calendar"},
]

triggers19f = [bls.element_label(t) for t in bls.find_dropdown_triggers(d19f)]
check("le bouton de langue est écarté", triggers19f == ["More actions"], triggers19f)
check("seul le vrai menu d'actions est retenu", "English" not in triggers19f)
check("parcours réussi malgré le bouton parasite", bls.click_booking_link(d19f) is True)
check("le menu de langue n'a jamais été ouvert", not d19f.dropdowns[0].get("open"),
      d19f.dropdowns[0].get("open"))
check("le calendrier est affiché", d19f.page == "calendar", d19f.page)
check("la langue n'a pas été changée (page ≠ blank)", d19f.page != "blank")
check("« Cancel Appointment » toujours ignoré", bls.calendar_is_rendered(d19f))

print("\n=== 20. Clic robuste (élément masqué par un overlay) ===")


class StubbornElement:
    """Refuse le clic natif, accepte le clic après recentrage."""
    id = "stubborn-1"
    text = "Continue to slot selection"

    def __init__(self):
        self.attempts = []

    def get_attribute(self, name):
        return None

    def click(self):
        self.attempts.append("native")
        if len(self.attempts) == 1:
            raise Exception("element click intercepted")

    def is_enabled(self):
        return True

    def is_displayed(self):
        return True


stubborn = StubbornElement()
fake_ctx = FakeDriver(page="calendar")
check("clic robuste : retombe sur ses pieds", bls.click_element(fake_ctx, stubborn) is True)
check("clic natif retenté après recentrage", len(stubborn.attempts) == 2, stubborn.attempts)
check("element_label lit le texte", bls.element_label(stubborn) == "Continue to slot selection")

print("\n=== 20b. Éléments détachés (stale) après re-rendu du menu ===")
# En vrai, ouvrir puis refermer un menu Radix fait re-rendre la liste des
# rendez-vous : les références récoltées AVANT la boucle deviennent invalides.
stale_drv = FakeDriver(page="list", logged_in=True, stale_menus=True)
stale_drv.dropdowns = [
    # 1er rendez-vous : menu sans entrée de créneau -> on le referme (Échap)
    {"trigger": "More actions", "items": ["Cancel Appointment", "View details"],
     "page": "blank"},
    # 2e rendez-vous : le bon menu
    {"trigger": "More actions",
     "items": ["Cancel Appointment", "Continue to slot selection"],
     "page": "calendar"},
]
before20b = bls.find_dropdown_triggers(stale_drv)
check("deux déclencheurs repérés avant la boucle", len(before20b) == 2, len(before20b))
check("le parcours aboutit malgré les éléments détachés",
      bls.open_dropdown_and_click_slot_item(stale_drv) is True)
check("calendrier affiché via le 2e menu", stale_drv.page == "calendar", stale_drv.page)
check("l'entrée dangereuse n'a pas été cliquée", stale_drv.page != "blank")

# Sans re-interrogation du DOM, la référence périmée lève immédiatement
try:
    before20b[1].click()
    stale_raised = False
except Exception as exc:
    stale_raised = "StaleElementReference" in type(exc).__name__
check("la vieille référence est bien détachée (le test est réaliste)", stale_raised)

print("\n=== 20c. Clic JavaScript de secours (séquence pointer) ===")


class JsOnlyElement:
    """Refuse tout clic natif ; ne réagit qu'aux événements dispatchés en JS."""
    id = "js-only-1"
    text = "Continue to slot selection"

    def __init__(self):
        self.native_attempts = 0
        self.js_clicks = 0

    def get_attribute(self, name):
        return None

    def click(self):
        self.native_attempts += 1
        raise Exception("element not interactable")

    def js_click(self):
        self.js_clicks += 1

    def is_enabled(self):
        return True

    def is_displayed(self):
        return True


js_only = JsOnlyElement()
js_ctx = FakeDriver(page="list")
check("clic de secours : succès via JavaScript", bls.click_element(js_ctx, js_only) is True)
check("deux clics natifs tentés d'abord", js_only.native_attempts == 2, js_only.native_attempts)
check("clic JavaScript efectué une fois", js_only.js_clicks == 1, js_only.js_clicks)
check("la séquence inclut pointerup (exigé par les menus Radix)",
      "pointerup" in bls.JS_CLICK_SCRIPT and "dispatchEvent" in bls.JS_CLICK_SCRIPT)
check("la séquence inclut pointerdown et click",
      "pointerdown" in bls.JS_CLICK_SCRIPT and "'click'" in bls.JS_CLICK_SCRIPT)

print("\n=== 20d. Doublon masqué (site responsive) écarté ===")
# BLS peut dupliquer le même bouton « More actions » (version mobile masquée) :
# cliquer l'exemplaire invisible ferait échouer tout le parcours.
hidden_drv = FakeDriver(page="list", logged_in=True)
hidden_drv.dropdowns = [
    {"trigger": "More actions", "hidden": True,          # doublon invisible
     "items": ["Continue to slot selection"], "page": "blank"},
    {"trigger": "More actions",                          # le vrai, visible
     "items": ["Continue to slot selection"], "page": "calendar"},
]
visible20d = bls.find_dropdown_triggers(hidden_drv)
check("le déclencheur masqué est écarté", len(visible20d) == 1, len(visible20d))
check("parcours réussi via le bouton visible",
      bls.open_dropdown_and_click_slot_item(hidden_drv) is True)
check("calendrier affiché (et pas la page du doublon)",
      hidden_drv.page == "calendar", hidden_drv.page)

# Si TOUS les déclencheurs sont masqués, on ne les écarte pas : mieux vaut
# tenter le clic que de renoncer.
all_hidden = FakeDriver(page="list", logged_in=True)
all_hidden.dropdowns = [{"trigger": "More actions", "hidden": True,
                         "items": ["Continue to slot selection"], "page": "calendar"}]
check("tous masqués -> quand même retenus en dernier recours",
      len(bls.find_dropdown_triggers(all_hidden)) == 1)
check("element_is_visible : élément masqué", bls.element_is_visible(
    all_hidden._dropdown_trigger(all_hidden.dropdowns[0])) is False)

print("\n=== 20e. Calendrier ouvert dans un NOUVEL ONGLET ===")
tab_drv = FakeDriver(page="list", logged_in=True)
tab_drv.dropdowns = [{"trigger": "More actions", "new_tab": True,
                      "items": ["Cancel Appointment", "Continue to slot selection"],
                      "page": "calendar"}]
check("un seul onglet au départ", tab_drv.window_handles == ["w1"], tab_drv.window_handles)
check("parcours réussi malgré l'ouverture d'un onglet",
      bls.open_dropdown_and_click_slot_item(tab_drv) is True)
check("un second onglet a été ouvert", len(tab_drv.window_handles) == 2,
      tab_drv.window_handles)
check("le script a basculé sur le nouvel onglet",
      tab_drv.current_window_handle == "w2", tab_drv.current_window_handle)
check("calendrier lu dans le bon onglet", tab_drv.page == "calendar", tab_drv.page)
check("l'onglet d'origine est resté sur la liste",
      tab_drv._windows["w1"]["page"] == "list", tab_drv._windows["w1"]["page"])

print("\n=== 20f. Nouvel onglet SANS calendrier : retour en arrière ===")
junk_drv = FakeDriver(page="list", logged_in=True)
junk_drv.dropdowns = [{"trigger": "More actions", "new_tab": True,
                       "items": ["Continue to slot selection"], "page": "blank"}]
check("échec quand le nouvel onglet n'a pas de calendrier",
      bls.open_dropdown_and_click_slot_item(junk_drv) is False)
check("retour à l'onglet d'origine", junk_drv.current_window_handle == "w1",
      junk_drv.current_window_handle)
check("page d'origine inchangée", junk_drv.page == "list", junk_drv.page)
check("la fenêtre parasite n'est pas fermée de force",
      len(junk_drv.window_handles) == 2, junk_drv.window_handles)

print("\n=== 21. Intégration : main() de bout en bout ===")
import json as _json  # noqa: E402
import urllib.request  # noqa: E402

STATUS_PATH = os.path.join(WORK_DIR, "bls_status_main.json")
try:
    os.remove(STATUS_PATH)
except OSError:
    pass

hub21 = StatusHub(enabled=True, host="127.0.0.1", port=8799,
                  status_file=STATUS_PATH, account_email="testeur@example.com")
bls.status = hub21
logs21 = capture_logs()
prompts21 = []
builtins.input = lambda prompt="": (prompts21.append(prompt), "")[1]

e2e = FakeDriver(page="login", logged_in=False,
                 months_available={"septembre 2026": ["2026-09-24"]})


def e2e_hook(url, driver):
    if url.endswith("algeria.blsinternational.com/"):
        driver.page = "account" if driver.logged_in else "login"
        return
    driver.page = "calendar" if driver.logged_in else "login"


e2e.on_get = e2e_hook
bls.create_driver = lambda: e2e
bls.TARGET_HOUR, bls.TARGET_MINUTE = 16, 55      # déjà passée -> démarrage immédiat

bls.main()
builtins.input = lambda prompt="": ""
snap21 = hub21.snapshot()

check("main() se termine sans exception", True)
check("tableau de bord démarré par main()", hub21._server is None,
      "serveur encore actif après main()")
check("fichier d'état écrit sur disque", os.path.exists(STATUS_PATH), STATUS_PATH)
if os.path.exists(STATUS_PATH):
    with open(STATUS_PATH, encoding="utf-8") as handle:
        written = _json.load(handle)
    check("état du fichier JSON valide", written["state"] == "CRENEAU_TROUVE", written["state"])
    check("jours détectés dans le fichier", written["slots_found"] == ["2026-09-24"], written["slots_found"])
    check("aucun secret dans le fichier", "mot-de-passe-test" not in _json.dumps(written))
check("état final = CRENEAU_TROUVE", snap21["state"] == "CRENEAU_TROUVE", snap21["state"])
check("créneau publié", snap21["slots_found"] == ["2026-09-24"], snap21["slots_found"])
check("mode de rafraîchissement calibré", snap21["refresh_mode"] in ("reload", "soft"), snap21["refresh_mode"])
check("session active publiée", snap21["session"] == "active", snap21["session"])
check("URL du calendrier publiée", "appointment" in snap21["current_url"], snap21["current_url"])
check("mois affiché publié", snap21["month_displayed"] == "septembre 2026", snap21["month_displayed"])
check("vérifications comptées", snap21["checks"] >= 1, snap21["checks"])
check("invite finale affichée", any("terminé pour fermer l'assistant" in p for p in prompts21), prompts21)
check("aucune invite CAPTCHA inutile", len(prompts21) == 1, prompts21)
check("démarrage immédiat journalisé", "Heure cible déjà passée" in logs21.text())
check("connexion confirmée journalisée", "Connexion confirmée" in logs21.text())
check("calendrier atteint journalisé", "Calendrier des créneaux affiché" in logs21.text())
check("alerte créneau journalisée", "CRÉNEAU POTENTIELLEMENT DISPONIBLE" in logs21.text())
check("journal du viewer complet (>= 8 événements)", len(snap21["events"]) >= 8, len(snap21["events"]))
check("presélection effectuée", e2e._clicked_days == ["2026-09-24"], e2e._clicked_days)

print("\n=== 22. Intégration : main() survit à une erreur fatale de Chrome ===")
hub22 = fresh_status()
logs22 = capture_logs()
prompts22 = []
builtins.input = lambda prompt="": (prompts22.append(prompt), "")[1]


def broken_create_driver():
    raise RuntimeError("Échec du lancement de Chrome : ChromeDriver 153 vs Chrome 151")


bls.create_driver = broken_create_driver
bls.main()
builtins.input = lambda prompt="": ""
snap22 = hub22.snapshot()
check("main() ne propage pas l'exception", True)
check("état final = ERREUR", snap22["state"] == "ERREUR", snap22["state"])
check("détail de l'erreur publié", "ChromeDriver" in (snap22["state_detail"] or ""), snap22["state_detail"])
check("erreur fatale journalisée", "Erreur fatale" in logs22.text())
check("invite après erreur fatale", any("fermer l'assistant" in p for p in prompts22), prompts22)

print("\n=== 23. Scénario réel complet : connexion -> liste -> 2 menus -> calendrier -> créneau ===")
# Reproduit fidèlement la séance décrite par l'utilisateur :
#   connexion automatique, /manage-appointments (SPA qui retombe sur la liste
#   à chaque rechargement), DEUX boutons « More actions » (le 1er sans entrée
#   de créneau, le 2e avec « Continue to slot selection »), entrées dangereuses
#   dans les deux menus, éléments détachés après chaque ouverture de menu, et
#   créneau disponible uniquement au mois suivant.
hub23 = fresh_status()
logs23 = capture_logs()
prompts23 = []
builtins.input = lambda prompt="": (prompts23.append(prompt), "")[1]

real = FakeDriver(page="login", logged_in=False, stale_menus=True,
                  months_available={"octobre 2026": ["2026-10-07"]})
real.dropdowns = [
    {"trigger": "More actions",                      # 1er rendez-vous : pas de créneau
     "items": ["Cancel Appointment", "View details"], "page": "blank"},
    {"trigger": "More actions",                      # 2e rendez-vous : le bon
     "items": ["Cancel Appointment", "Continue to slot selection"],
     "page": "calendar"},
]


def real_hook(url, driver):
    if url.endswith("algeria.blsinternational.com/"):
        driver.page = "account" if driver.logged_in else "login"
        return
    # Toute (re)charge de l'URL des créneaux réinitialise la SPA sur la liste
    driver.page = "list" if driver.logged_in else "login"


real.on_get = real_hook

check("23.1 connexion automatique", bls.login(real, interactive=False) is True)
check("23.2 aucune invite bloquante pendant la connexion", prompts23 == [], prompts23)

url23 = bls.try_auto_navigate(real)
check("23.3 calendrier atteint automatiquement", bool(url23), url23)
check("23.4 passé par le 2e menu « More actions »", real.page == "calendar", real.page)
check("23.5 « Continue to slot selection » cliqué",
      "Continue to slot selection" in real._clicked_texts, real._clicked_texts)
check("23.6 « Cancel Appointment » JAMAIS cliqué",
      "Cancel Appointment" not in real._clicked_texts, real._clicked_texts)
check("23.7 « View details » non cliqué non plus",
      "View details" not in real._clicked_texts, real._clicked_texts)
check("23.8 le 1er menu a été ouvert puis refermé",
      real.dropdowns[0].get("open") is False, real.dropdowns[0].get("open"))
check("23.9 échec du 1er menu journalisé",
      "aucune entrée de sélection de créneau" in logs23.text())

mode23 = bls.calibrate_refresh_mode(real, url23)
check("23.10 calibrage -> mode soft (SPA)", mode23 == "soft", mode23)

result23 = bls.refresh_until_slot_appears(real, url23, refresh_mode=mode23)
snap23 = hub23.snapshot()
check("23.11 créneau trouvé", result23 == "slot", result23)
check("23.12 créneau du MOIS SUIVANT détecté", snap23["slots_found"] == ["2026-10-07"],
      snap23["slots_found"])
check("23.13 mois suivant publié", snap23["state_detail"] == "octobre 2026",
      snap23["state_detail"])
check("23.14 jour présélectionné", real._clicked_days == ["2026-10-07"], real._clicked_days)
check("23.15 état final = CRENEAU_TROUVE", snap23["state"] == "CRENEAU_TROUVE",
      snap23["state"])
check("23.16 aucune invite manuelle de toute la séance", prompts23 == [], prompts23)
check("23.17 aucune relance de Chrome", snap23["recoveries"] == 0, snap23["recoveries"])
check("23.18 session restée active", snap23["session"] == "active", snap23["session"])
check("23.19 mode soft publié au tableau de bord", snap23["refresh_mode"] == "soft",
      snap23["refresh_mode"])
check("23.20 toujours aucun clic dangereux en fin de séance",
      "Cancel Appointment" not in real._clicked_texts, real._clicked_texts)
check("23.21 scan du mois suivant compté", snap23["next_month_scans"] >= 1,
      snap23["next_month_scans"])
builtins.input = lambda prompt="": ""

print("\n=== 24. Chaîne complète : page compte -> liste -> menu -> calendrier ===")
# Après connexion, le script est sur la page COMPTE. Le calendrier est à trois
# sauts : lien « Manage Appointments » -> liste -> « More actions » ->
# « Continue to slot selection ». Chaque saut intermédiaire ressemble à un
# échec (aucun calendrier) : sans enchaînement, le script rendait la main.
hub24 = fresh_status()
logs24 = capture_logs()

hop = FakeDriver(page="account", logged_in=True,
                 months_available={"septembre 2026": ["2026-09-29"]})
hop.links = [{"text": "Manage Appointments", "href": "/es/fr/manage-appointments",
              "page": "list"}]
hop.dropdowns = [{"trigger": "More actions",
                  "items": ["Cancel Appointment", "Continue to slot selection"],
                  "page": "calendar"}]

check("24.1 aucun menu sur la page compte", bls.find_dropdown_triggers(hop) == [])
check("24.2 chaînage réussi jusqu'au calendrier", bls.click_booking_link(hop) is True)
check("24.3 calendrier affiché au bout de la chaîne", hop.page == "calendar", hop.page)
check("24.4 lien « Manage Appointments » cliqué",
      "Manage Appointments" in hop._clicked_texts, hop._clicked_texts)
check("24.5 entrée de menu cliquée ensuite",
      "Continue to slot selection" in hop._clicked_texts, hop._clicked_texts)
check("24.6 « Cancel Appointment » jamais cliqué",
      "Cancel Appointment" not in hop._clicked_texts, hop._clicked_texts)
check("24.7 enchaînement journalisé",
      "Liste des rendez-vous atteinte" in logs24.text())

# try_auto_navigate() doit aboutir seul, sans invite manuelle
hop2 = FakeDriver(page="account", logged_in=True)
hop2.links = [{"text": "Manage Appointments", "href": "/es/fr/manage-appointments",
               "page": "list"}]
hop2.dropdowns = [{"trigger": "More actions",
                   "items": ["Cancel Appointment", "Continue to slot selection"],
                   "page": "calendar"}]
url24 = bls.try_auto_navigate(hop2)
check("24.8 navigation automatique de bout en bout", bool(url24), url24)
check("24.9 URL du calendrier renvoyée", "appointment" in (url24 or ""), url24)
check("24.10 calendrier atteint sans intervention", hop2.page == "calendar", hop2.page)

# Contrôle négatif : sans l'enchaînement, la chaîne s'arrête à la liste
sans_chaine = bls.chain_to_calendar_via_menu
bls.chain_to_calendar_via_menu = lambda driver: False
hop3 = FakeDriver(page="account", logged_in=True)
hop3.links = [{"text": "Manage Appointments", "href": "/es/fr/manage-appointments",
               "page": "list"}]
hop3.dropdowns = [{"trigger": "More actions",
                   "items": ["Cancel Appointment", "Continue to slot selection"],
                   "page": "calendar"}]
old_chain = bls.click_booking_link(hop3)
bls.chain_to_calendar_via_menu = sans_chaine
check("24.11 sans enchaînement le parcours échoue (le test discrimine)",
      old_chain is False and hop3.page == "list", (old_chain, hop3.page))

print("\n=== 25. URL mémorisée menant à la liste : enchaînement sans fausse alerte ===")
# Cas nominal sur BLS : l'URL mémorisée est celle de /manage-appointments. Le
# calendrier ne s'y affiche pas directement -> ce n'est PAS une session
# expirée ni une page déplacée, c'est le parcours normal en plusieurs sauts.
hub25 = fresh_status()
logs25 = capture_logs()

saved_list = FakeDriver(page="list", logged_in=True,
                        months_available={"septembre 2026": ["2026-09-30"]})
saved_list.dropdowns = [{"trigger": "More actions",
                         "items": ["Cancel Appointment", "Continue to slot selection"],
                         "page": "calendar"}]
saved_list.on_get = lambda url, driver: setattr(driver, "page", "list")

url25 = bls.try_auto_navigate(saved_list)
snap25 = hub25.snapshot()
check("25.1 calendrier atteint depuis l'URL mémorisée", bool(url25), url25)
check("25.2 page finale = calendrier", saved_list.page == "calendar", saved_list.page)
check("25.3 enchaînement sur le menu journalisé",
      "liste des rendez-vous" in logs25.text().lower())
check("25.4 AUCUNE fausse alerte « session expirée ou page déplacée »",
      "session expirée ou page déplacée" not in logs25.text())
check("25.5 aucun événement d'alerte « calendrier non affiché »",
      not any(e["message"] == "URL mémorisée : calendrier non affiché"
              for e in snap25["events"]), [e["message"] for e in snap25["events"]])
check("25.6 événement d'enchaînement publié",
      any("liste des rendez-vous" in e["message"].lower() for e in snap25["events"]))
check("25.7 « Cancel Appointment » jamais cliqué",
      "Cancel Appointment" not in saved_list._clicked_texts, saved_list._clicked_texts)
check("25.8 URL du calendrier publiée", "appointment" in snap25["current_url"],
      snap25["current_url"])

# Cas réellement anormal : l'URL mémorisée mène ailleurs (page compte) -> alerte
logs25b = capture_logs()
hub25b = fresh_status()
odd = FakeDriver(page="account", logged_in=True)
odd.on_get = lambda url, driver: setattr(driver, "page", "account")
url25b = bls.try_auto_navigate(odd)
check("25.9 page sans liste ni calendrier -> échec", url25b is None, url25b)
check("25.10 alerte légitime cette fois",
      "session expirée ou page déplacée" in logs25b.text())
# Après le parcours réussi, saved_list est sur le calendrier : plus de liste
list_sans_menu = FakeDriver(page="list", logged_in=True)      # dropdowns = []
check("25.11 liste SANS menu d'actions -> False",
      bls.on_appointment_list(list_sans_menu) is False)
check("25.12 calendrier atteint -> plus considéré comme la liste",
      bls.on_appointment_list(saved_list) is False)

list_page = FakeDriver(page="list", logged_in=True)
list_page.dropdowns = [{"trigger": "More actions",
                        "items": ["Continue to slot selection"], "page": "calendar"}]
check("25.13 on_appointment_list vrai sur la liste", bls.on_appointment_list(list_page) is True)
check("25.14 on_appointment_list faux sur la page compte",
      bls.on_appointment_list(FakeDriver(page="account", logged_in=True)) is False)
check("25.15 faux sur une page d'erreur",
      bls.on_appointment_list(FakeDriver(page="blank", logged_in=True)) is False)

print("\n=== 26. Repli de recherche de menu : bornage du nombre de boutons ===")


class StubButton:
    """Bouton quelconque qui se signale dès qu'on l'examine."""

    def __init__(self, index, touched):
        self.id = f"btn-{index}"
        self._index = index
        self._touched = touched

    def _touch(self):
        self._touched.add(self._index)

    @property
    def text(self):
        self._touch()
        return f"Bouton {self._index}"

    def get_attribute(self, name):
        self._touch()
        return None

    def is_displayed(self):
        return True

    def is_enabled(self):
        return True


class ManyButtonsDriver:
    """Page de 300 boutons, aucun sélecteur de menu déroulant reconnu."""

    def __init__(self, count=300):
        self.touched = set()
        self.buttons = [StubButton(i, self.touched) for i in range(count)]

    def find_elements(self, by, selector):
        if "dropdown-menu-trigger" in selector or "aria-haspopup" in selector:
            return []                      # aucun menu Radix détectable
        if selector == "button":
            return self.buttons
        return []


many = ManyButtonsDriver(300)
found26 = bls.find_dropdown_triggers(many)
check("26.1 aucun menu retenu sur une page sans menu d'actions", found26 == [], found26)
check("26.2 repli borné à MAX_BUTTON_FALLBACK boutons",
      len(many.touched) <= bls.MAX_BUTTON_FALLBACK, len(many.touched))
check("26.3 bien moins que les 300 boutons de la page",
      len(many.touched) < 300, len(many.touched))
check("26.4 la borne est configurable et encadrée",
      5 <= bls.MAX_BUTTON_FALLBACK <= 400, bls.MAX_BUTTON_FALLBACK)
check("26.5 les 60 premiers boutons seulement examinés",
      max(many.touched) < bls.MAX_BUTTON_FALLBACK, max(many.touched))

print("\n" + "=" * 66)
print(f"RESULTAT : {len(PASSED)} verifications OK, {len(FAILED)} en echec")
for failure in FAILED:
    print("  ECHEC", failure)
print("=" * 66)
print(f"Dossier de travail des tests : {WORK_DIR}")
sys.exit(1 if FAILED else 0)
