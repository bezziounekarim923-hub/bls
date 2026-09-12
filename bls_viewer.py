#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BLS Viewer — tableau de bord local de l'assistant de surveillance
================================================================

Petit serveur web (100 % bibliothèque standard, aucune dépendance) qui
affiche en temps réel ce que fait `bls_espagne_prefill.py` :

- le compte à rebours avant l'ouverture des créneaux,
- l'état de la session BLS (active / expirée),
- le nombre de vérifications, d'échecs, de relances de Chrome,
  de reconnexions automatiques et de scans du mois suivant,
- le mois affiché / le mois scanné et les jours disponibles détectés,
- le résultat du self-check (T-30 min),
- le journal des derniers événements.

Trois modes d'utilisation
-------------------------
1. **Intégré** (cas normal) : `bls_espagne_prefill.py` importe `StatusHub`,
   le met à jour pendant son exécution et démarre le serveur dans un thread
   en arrière-plan. Ouvre alors http://127.0.0.1:8765 dans ton navigateur.

2. **Autonome** : si le script tourne déjà (ou sur une autre machine/dossier),
   le viewer peut lire le fichier d'état écrit par le script :

       python bls_viewer.py --status-file bls_status.json --port 8766

3. **Démo** : pour voir l'interface sans Chrome ni identifiants, avec des
   données simulées :

       python bls_viewer.py --demo

Sécurité
--------
Le serveur écoute sur `127.0.0.1` par défaut : le tableau de bord n'est
visible que depuis ton ordinateur. Aucune donnée sensible n'est publiée
(mot de passe jamais transmis, email masqué). Si tu l'exposes sur
`0.0.0.0`, n'importe qui sur ton réseau pourra lire l'état de la
surveillance — à ne faire que temporairement.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import threading
import time
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ============ CONFIGURATION ============

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
STATUS_FILENAME = "bls_status.json"

# Nombre d'événements conservés dans le journal affiché
MAX_EVENTS = 80

# Écriture du fichier d'état au plus une fois toutes les X secondes
# (le tableau de bord, lui, lit la mémoire : il n'est pas limité)
FLUSH_MIN_INTERVAL = 0.5

# États possibles du script — la clé sert de classe CSS côté navigateur
STATES = (
    "INACTIF",
    "DEMARRAGE",
    "COMPTE_A_REBOURS",
    "SELF_CHECK",
    "CONNEXION",
    "NAVIGATION",
    "SURVEILLANCE",
    "SESSION_EXPIREE",
    "RECONNEXION",
    "CHROME_RECOVERY",
    "CRENEAU_TROUVE",
    "ERREUR",
    "ARRET",
)

# Libellés français affichés dans le tableau de bord
STATE_LABELS = {
    "INACTIF": "Inactif",
    "DEMARRAGE": "Démarrage",
    "COMPTE_A_REBOURS": "Compte à rebours",
    "SELF_CHECK": "Self-check",
    "CONNEXION": "Connexion",
    "NAVIGATION": "Navigation",
    "SURVEILLANCE": "Surveillance",
    "SESSION_EXPIREE": "Session expirée",
    "RECONNEXION": "Reconnexion",
    "CHROME_RECOVERY": "Relance de Chrome",
    "CRENEAU_TROUVE": "Créneau trouvé",
    "ERREUR": "Erreur",
    "ARRET": "Arrêté",
}

# =====================================================


def mask_email(email: str) -> str:
    """Masque un email avant publication (k*****@gmail.com)."""
    if not email or "@" not in email:
        return ""
    local, _, domain = email.partition("@")
    if len(local) <= 1:
        masked = local + "***"
    else:
        masked = local[0] + "*" * min(6, len(local) - 1) + local[-1]
    return f"{masked}@{domain}"


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class StatusHub:
    """
    État partagé de la surveillance + serveur web du tableau de bord.

    Toutes les méthodes sont sûres depuis plusieurs threads (le script
    Selenium tourne dans le thread principal, le serveur HTTP dans le
    sien). Si `enabled=False`, les mises à jour sont ignorées et aucun
    serveur n'est démarré : le script continue de fonctionner normalement.
    """

    def __init__(
        self,
        enabled: bool = True,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        status_file: str | None = None,
        account_email: str = "",
    ) -> None:
        self.enabled = enabled
        self.host = host
        self.port = int(port)
        script_dir = os.path.dirname(os.path.abspath(__file__))
        self.status_file = status_file or os.path.join(script_dir, STATUS_FILENAME)

        self._lock = threading.RLock()
        self._events: deque = deque(maxlen=MAX_EVENTS)
        self._server = None
        self._thread = None
        self._last_flush = 0.0

        self._data = {
            "schema": 1,
            "started_at": _now_iso(),
            "updated_at": _now_iso(),
            "state": "INACTIF",
            "state_label": STATE_LABELS["INACTIF"],
            "state_detail": "",
            "target_time": "",
            "target_ts": None,
            "target_window_seconds": 0,
            "seconds_remaining": None,
            "session": "inconnue",
            "account": mask_email(account_email),
            "current_url": "",
            "month_displayed": "",
            "month_scanned": "",
            "checks": 0,
            "failures": 0,
            "recoveries": 0,
            "relogins": 0,
            "next_month_scans": 0,
            "slots_found": [],
            "last_check_at": None,
            "next_check_in": None,
            "preflight": {
                "done": False,
                "session_ok": None,
                "calendar_ok": None,
                "message": "",
            },
            "viewer": {"host": host, "port": self.port, "url": self.url},
        }

    # ---------- lecture ----------

    @property
    def url(self) -> str:
        host = self.host if self.host not in ("0.0.0.0", "::") else "127.0.0.1"
        return f"http://{host}:{self.port}"

    def snapshot(self) -> dict:
        """Copie de l'état courant, sérialisable en JSON."""
        with self._lock:
            data = dict(self._data)
            data["events"] = list(self._events)
            data["state_label"] = STATE_LABELS.get(data["state"], data["state"])
            data["updated_at"] = _now_iso()
            return data

    # ---------- écriture ----------

    def set(self, **fields) -> None:
        """Met à jour un ou plusieurs champs d'état."""
        if not self.enabled:
            return
        with self._lock:
            for key, value in fields.items():
                if key == "state":
                    value = str(value).upper()
                    if value not in STATES:
                        value = "INACTIF"
                self._data[key] = value
            self._flush()

    def incr(self, name: str, amount: int = 1) -> None:
        """Incrémente un compteur (checks, failures, recoveries...)."""
        if not self.enabled:
            return
        with self._lock:
            current = self._data.get(name, 0)
            try:
                self._data[name] = int(current) + int(amount)
            except (TypeError, ValueError):
                self._data[name] = int(amount)
            self._flush()

    def reset(self, *names: str) -> None:
        """Remet des compteurs à zéro (ex: failures après une relance)."""
        if not self.enabled:
            return
        with self._lock:
            for name in names:
                self._data[name] = 0
            self._flush()

    def event(self, message: str, level: str = "info") -> None:
        """Ajoute une ligne au journal du tableau de bord."""
        if not self.enabled:
            return
        level = level if level in ("info", "ok", "warn", "error", "slot") else "info"
        with self._lock:
            self._events.appendleft(
                {"ts": _now_iso(), "level": level, "message": str(message)}
            )
            self._flush()

    def slot_found(self, dates, month: str = "") -> None:
        """Signale la détection de créneaux (état prioritaire du dashboard)."""
        if not self.enabled:
            return
        dates = [str(d) for d in (dates or [])][:20]
        with self._lock:
            self._data["slots_found"] = dates
            self._data["state"] = "CRENEAU_TROUVE"
            self._data["state_detail"] = month or ""
            self._events.appendleft(
                {
                    "ts": _now_iso(),
                    "level": "slot",
                    "message": f"Créneau(x) détecté(s) : {', '.join(dates) or '?'}"
                    + (f" ({month})" if month else ""),
                }
            )
            self._flush(force=True)

    # ---------- persistance ----------

    def flush(self, force: bool = True) -> None:
        """Force l'écriture immédiate de l'état sur disque."""
        if not self.enabled:
            return
        with self._lock:
            self._flush(force=force)

    def _flush(self, force: bool = False) -> None:
        """
        Écrit l'état dans `bls_status.json` (écriture atomique), au plus
        une fois toutes les FLUSH_MIN_INTERVAL secondes. Utile pour un
        viewer lancé séparément et pour diagnostiquer après coup.
        """
        now = time.time()
        if not force and (now - self._last_flush) < FLUSH_MIN_INTERVAL:
            return
        self._last_flush = now
        try:
            payload = json.dumps(self.snapshot(), ensure_ascii=False, indent=1)
            directory = os.path.dirname(self.status_file) or "."
            fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".bls_status_", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(payload)
                os.replace(tmp_path, self.status_file)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except Exception:
            # Le tableau de bord est un confort : il ne doit jamais
            # interrompre la surveillance.
            pass

    # ---------- serveur ----------

    def start(self) -> str | None:
        """Démarre le serveur HTTP en arrière-plan. Retourne l'URL ou None."""
        if not self.enabled:
            return None
        with self._lock:
            if self._server is not None:
                return self.url
            hub = self

            class Handler(_DashboardHandler):
                provider = staticmethod(hub.snapshot)

            try:
                self._server = ThreadingHTTPServer((self.host, self.port), Handler)
            except OSError as exc:
                self.event(
                    f"Tableau de bord indisponible sur {self.host}:{self.port} ({exc})",
                    "warn",
                )
                self._server = None
                return None

            self._server.daemon_threads = True
            self._thread = threading.Thread(
                target=self._server.serve_forever,
                name="bls-viewer",
                daemon=True,
            )
            self._thread.start()
            self._data["viewer"] = {"host": self.host, "port": self.port, "url": self.url}
            self.event(f"Tableau de bord démarré : {self.url}", "ok")
            self._flush(force=True)
            return self.url

    def stop(self) -> None:
        """Arrête le serveur (appelé en fin de script, best effort)."""
        with self._lock:
            if self._server is None:
                return
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:
                pass
            self._server = None
            self._thread = None


class _DashboardHandler(BaseHTTPRequestHandler):
    """
    Sert le tableau de bord (HTML) et l'état (JSON).

    `provider` est un callable sans argument retournant le dictionnaire
    d'état : en mode intégré c'est `StatusHub.snapshot`, en mode autonome
    c'est la lecture du fichier écrit par le script.
    """

    provider = staticmethod(lambda: {})
    server_version = "BLSViewer/1.0"
    protocol_version = "HTTP/1.1"

    # Le journal HTTP du serveur n'a rien à faire dans les logs du script
    def log_message(self, fmt, *args):  # noqa: A003 - signature imposée
        return

    def do_GET(self):  # noqa: N802 - signature imposée
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path in ("/status.json", "/status"):
            self._send_json(self._safe_snapshot())
        elif path in ("/", "/index.html"):
            self._send_bytes(DASHBOARD_HTML.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/health":
            self._send_json({"ok": True, "ts": _now_iso()})
        else:
            self._send_bytes(b"Not found", "text/plain; charset=utf-8", code=404)

    def _safe_snapshot(self) -> dict:
        try:
            data = self.provider() or {}
        except Exception as exc:  # pragma: no cover - filet de sécurité
            data = {"state": "ERREUR", "state_detail": f"état illisible ({exc})"}
        if isinstance(data, dict):
            data.setdefault("schema", 1)
        return data

    def _send_json(self, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self._send_bytes(body, "application/json; charset=utf-8", no_cache=True)

    def _send_bytes(self, body: bytes, content_type: str, code: int = 200, no_cache: bool = False) -> None:
        try:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            if no_cache:
                self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


# ============ Interface HTML/CSS/JS du tableau de bord ============

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BLS — tableau de bord de surveillance</title>
<style>
  :root{
    --bg:#0d1117; --panel:#161b22; --panel2:#1c2330; --border:#2b3444;
    --text:#e6edf3; --muted:#93a1b1; --accent:#4aa8ff; --ok:#3fb950;
    --warn:#d29922; --err:#f85149; --slot:#ff7b1c;
    --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;
  }
  *{box-sizing:border-box}
  body{
    margin:0;padding:20px;background:var(--bg);color:var(--text);
    font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
  }
  header{display:flex;flex-wrap:wrap;gap:12px;align-items:center;justify-content:space-between;margin-bottom:18px}
  h1{font-size:18px;margin:0;letter-spacing:.2px}
  h1 span{color:var(--muted);font-weight:400}
  .badge{
    padding:6px 12px;border-radius:999px;font-weight:600;font-size:13px;
    border:1px solid var(--border);background:var(--panel2);color:var(--muted);
    display:inline-flex;align-items:center;gap:8px;white-space:nowrap;
  }
  .badge::before{content:"";width:9px;height:9px;border-radius:50%;background:currentColor}
  .badge.SURVEILLANCE,.badge.ok{color:var(--ok)}
  .badge.COMPTE_A_REBOURS,.badge.SELF_CHECK,.badge.NAVIGATION,.badge.CONNEXION,.badge.DEMARRAGE{color:var(--accent)}
  .badge.SESSION_EXPIREE,.badge.ERREUR,.badge.CHROME_RECOVERY,.badge.RECONNEXION{color:var(--warn)}
  .badge.CRENEAU_TROUVE{color:#fff;background:var(--slot);border-color:var(--slot);animation:pulse 1s infinite}
  .badge.ARRET,.badge.INACTIF{color:var(--muted)}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.55}}

  .alert{
    display:none;margin-bottom:16px;padding:14px 16px;border-radius:10px;
    background:rgba(255,123,28,.14);border:1px solid var(--slot);color:#ffd9b3;
  }
  .alert.show{display:block}
  .alert.offline{background:rgba(248,81,73,.12);border-color:var(--err);color:#ffc9c5}
  .alert strong{color:#fff}

  .grid{display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(240px,1fr))}
  .card{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:16px}
  .card h2{margin:0 0 10px;font-size:12px;text-transform:uppercase;letter-spacing:.9px;color:var(--muted)}
  .big{font-family:var(--mono);font-size:44px;font-weight:700;line-height:1.05;letter-spacing:-1px}
  .big.small{font-size:30px}
  .sub{color:var(--muted);font-size:13px;margin-top:6px}
  .bar{height:6px;border-radius:4px;background:var(--panel2);overflow:hidden;margin-top:12px}
  .bar>i{display:block;height:100%;width:0;background:var(--accent);transition:width .9s linear}

  .stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(96px,1fr));gap:10px}
  .stat{background:var(--panel2);border-radius:9px;padding:10px 12px}
  .stat b{display:block;font-family:var(--mono);font-size:22px}
  .stat span{color:var(--muted);font-size:12px}
  .stat.warn b{color:var(--warn)} .stat.err b{color:var(--err)} .stat.ok b{color:var(--ok)}

  .kv{display:grid;grid-template-columns:auto 1fr;gap:6px 12px;font-size:13.5px}
  .kv dt{color:var(--muted)} .kv dd{margin:0;font-family:var(--mono);word-break:break-all}

  .chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:4px}
  .chip{background:rgba(63,185,80,.14);border:1px solid rgba(63,185,80,.5);color:#b7f0c1;
        padding:3px 9px;border-radius:999px;font-family:var(--mono);font-size:12.5px}
  .none{color:var(--muted);font-size:13px}

  #log{list-style:none;margin:0;padding:0;max-height:340px;overflow:auto;font-family:var(--mono);font-size:12.8px}
  #log li{padding:5px 8px;border-bottom:1px solid rgba(43,52,68,.6);display:flex;gap:9px}
  #log li time{color:var(--muted);flex:0 0 auto}
  #log li.info span{color:var(--text)}
  #log li.ok span{color:var(--ok)}
  #log li.warn span{color:var(--warn)}
  #log li.error span{color:var(--err)}
  #log li.slot span{color:var(--slot);font-weight:700}
  .wide{grid-column:1/-1}
  footer{margin-top:16px;color:var(--muted);font-size:12.5px}
  code{font-family:var(--mono);background:var(--panel2);padding:1px 5px;border-radius:4px}
</style>
</head>
<body>
<header>
  <h1>Assistant BLS Espagne <span>— tableau de bord de surveillance</span></h1>
  <div id="state" class="badge INACTIF">État inconnu</div>
</header>

<div id="alert" class="alert"></div>

<div class="grid">
  <section class="card">
    <h2>Ouverture des créneaux</h2>
    <div id="countdown" class="big">--:--:--</div>
    <div id="target" class="sub">Heure cible non définie</div>
    <div class="bar"><i id="progress"></i></div>
  </section>

  <section class="card">
    <h2>Session &amp; navigateur</h2>
    <dl class="kv">
      <dt>Session</dt><dd id="session">inconnue</dd>
      <dt>Compte</dt><dd id="account">—</dd>
      <dt>Self-check</dt><dd id="preflight">non effectué</dd>
      <dt>Page</dt><dd id="url">—</dd>
    </dl>
  </section>

  <section class="card wide">
    <h2>Activité</h2>
    <div class="stats">
      <div class="stat"><b id="checks">0</b><span>vérifications</span></div>
      <div class="stat"><b id="scans">0</b><span>scans mois suivant</span></div>
      <div class="stat warn"><b id="failures">0</b><span>échecs en cours</span></div>
      <div class="stat warn"><b id="recoveries">0</b><span>relances Chrome</span></div>
      <div class="stat warn"><b id="relogins">0</b><span>reconnexions</span></div>
      <div class="stat"><b id="next">—</b><span>prochain scan</span></div>
    </div>
  </section>

  <section class="card">
    <h2>Calendrier</h2>
    <dl class="kv">
      <dt>Mois affiché</dt><dd id="monthDisplayed">—</dd>
      <dt>Mois scanné</dt><dd id="monthScanned">—</dd>
      <dt>Dernier scan</dt><dd id="lastCheck">—</dd>
    </dl>
    <h2 style="margin-top:14px">Jours disponibles</h2>
    <div id="slots" class="chips"><span class="none">Aucun créneau détecté pour l'instant.</span></div>
  </section>

  <section class="card wide">
    <h2>Journal des événements</h2>
    <ul id="log"><li class="info"><time>--:--:--</time><span>En attente du script…</span></li></ul>
  </section>
</div>

<footer>
  Lecture seule — ce tableau de bord n'agit jamais sur le site : le choix du créneau
  et la réservation restent manuels dans Chrome. Rafraîchissement automatique toutes
  les 1,5 s (<code>/status.json</code>).
</footer>

<script>
(function(){
  "use strict";
  var POLL_MS = 1500, last = null, fails = 0, lastPayload = null;
  var $ = function(id){ return document.getElementById(id); };

  function fmtDuration(seconds){
    if(seconds === null || seconds === undefined || isNaN(seconds)) return "--:--:--";
    seconds = Math.max(0, Math.floor(seconds));
    var h = Math.floor(seconds/3600), m = Math.floor((seconds%3600)/60), s = seconds%60;
    function pad(n){ return (n<10?"0":"")+n; }
    return (h>0 ? pad(h)+":" : "") + pad(m)+":"+pad(s);
  }

  function text(id, value, fallback){
    var el = $(id); if(!el) return;
    el.textContent = (value === null || value === undefined || value === "") ? (fallback || "—") : value;
  }

  function renderSlots(list){
    var box = $("slots"); if(!box) return;
    if(!list || !list.length){
      box.innerHTML = '<span class="none">Aucun créneau détecté pour l\'instant.</span>';
      return;
    }
    box.innerHTML = list.map(function(d){
      return '<span class="chip"></span>';
    }).join("");
    var chips = box.querySelectorAll(".chip");
    list.forEach(function(d, i){ if(chips[i]) chips[i].textContent = d; });
  }

  function renderLog(events){
    var ul = $("log"); if(!ul) return;
    if(!events || !events.length) return;
    ul.innerHTML = "";
    events.slice(0, 60).forEach(function(ev){
      var li = document.createElement("li");
      li.className = ev.level || "info";
      var t = document.createElement("time");
      t.textContent = (ev.ts || "").split(" ")[1] || ev.ts || "";
      var s = document.createElement("span");
      s.textContent = ev.message || "";
      li.appendChild(t); li.appendChild(s); ul.appendChild(li);
    });
  }

  function showAlert(kind, html){
    var box = $("alert"); if(!box) return;
    if(!kind){ box.className = "alert"; box.innerHTML = ""; return; }
    box.className = "alert show" + (kind === "offline" ? " offline" : "");
    box.innerHTML = html;
  }

  function render(d){
    lastPayload = d;
    var state = (d.state || "INACTIF").toUpperCase();
    var badge = $("state");
    badge.className = "badge " + state;
    badge.textContent = (d.state_label || state) + (d.state_detail ? " — " + d.state_detail : "");

    if(state === "CRENEAU_TROUVE"){
      showAlert("slot", "<strong>⚡ Créneau détecté !</strong> Regarde la fenêtre Chrome : "
        + "le jour est présélectionné. Choisis ton créneau horaire et clique « Réserver » — "
        + "cette dernière étape reste manuelle.");
    } else if(state === "SESSION_EXPIREE"){
      showAlert("warn", "<strong>Session expirée.</strong> Le script tente une reconnexion ; "
        + "si un CAPTCHA est demandé, résous-le dans la fenêtre Chrome.");
    } else if(state === "ERREUR" || state === "CHROME_RECOVERY"){
      showAlert("warn", "<strong>" + (d.state_label || state) + ".</strong> "
        + (d.state_detail ? d.state_detail : "Surveillance en cours de rétablissement."));
    } else {
      showAlert(null);
    }

    text("target", d.target_time ? "Cible : " + d.target_time : "Heure cible non définie");
    text("session", d.session || "inconnue");
    text("account", d.account || "—");
    text("url", d.current_url || "—");
    text("monthDisplayed", d.month_displayed || "—");
    text("monthScanned", d.month_scanned || "—");
    text("lastCheck", d.last_check_at || "—");
    text("checks", d.checks);
    text("scans", d.next_month_scans);
    text("failures", d.failures);
    text("recoveries", d.recoveries);
    text("relogins", d.relogins);

    var p = d.preflight || {};
    var bits = [];
    if(p.done){
      bits.push(p.session_ok === true ? "session ✔" : (p.session_ok === false ? "session ✘" : "session ?"));
      bits.push(p.calendar_ok === true ? "calendrier ✔" : (p.calendar_ok === false ? "calendrier ✘" : "calendrier ?"));
      if(p.message) bits.push(p.message);
      text("preflight", bits.join(" · "));
    } else {
      text("preflight", "non effectué");
    }

    renderSlots(d.slots_found);
    renderLog(d.events);
  }

  // Le compte à rebours est recalculé localement chaque seconde à partir
  // de l'horodatage cible : il reste fluide même entre deux rafraîchissements.
  function tick(){
    var d = lastPayload;
    if(!d){ $("countdown").textContent = "--:--:--"; return; }
    var remaining = null;
    if(typeof d.target_ts === "number" && d.target_ts > 0){
      remaining = (d.target_ts * 1000 - Date.now()) / 1000;
      if(remaining < 0) remaining = 0;
    } else if(typeof d.seconds_remaining === "number"){
      remaining = d.seconds_remaining;
    }
    $("countdown").textContent = (d.state === "SURVEILLANCE" || d.state === "CRENEAU_TROUVE")
      ? "OUVERT" : fmtDuration(remaining);
    var bar = $("progress");
    if(remaining !== null && d.target_window_seconds > 0){
      var done = 1 - Math.min(1, Math.max(0, remaining / d.target_window_seconds));
      bar.style.width = (done * 100).toFixed(1) + "%";
    } else if(remaining !== null){
      bar.style.width = "0%";
    }
    text("next", (typeof d.next_check_in === "number") ? ("~" + d.next_check_in.toFixed(0) + " s") : "—");
  }

  function poll(){
    fetch("status.json", {cache:"no-store"})
      .then(function(r){ if(!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
      .then(function(d){
        fails = 0;
        if(JSON.stringify(d) !== JSON.stringify(last)) { last = d; }
        render(d); tick();
        document.title = ((d.state === "CRENEAU_TROUVE") ? "⚡ " : "") + "BLS — " + (d.state_label || d.state);
      })
      .catch(function(){
        fails++;
        if(fails >= 2){
          showAlert("offline", "<strong>Tableau de bord déconnecté du script.</strong> "
            + "Le script s'est peut-être arrêté, ou il écoute sur un autre port. "
            + "Nouvelle tentative automatique…");
        }
      });
  }

  poll();
  setInterval(poll, POLL_MS);
  setInterval(tick, 1000);
})();
</script>
</body>
</html>
"""


# ============ Modes autonome et démo ============

class FileStatusProvider:
    """Lit l'état depuis le fichier JSON écrit par le script (mode autonome)."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._mtime = 0.0
        self._cache: dict = {}

    def __call__(self) -> dict:
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            return {
                "state": "ERREUR",
                "state_detail": f"fichier d'état introuvable : {self.path}",
                "events": [],
            }
        if mtime != self._mtime or not self._cache:
            self._mtime = mtime
            try:
                with open(self.path, encoding="utf-8") as handle:
                    self._cache = json.load(handle)
            except (OSError, ValueError) as exc:
                return {"state": "ERREUR", "state_detail": f"état illisible ({exc})", "events": []}
        data = dict(self._cache)
        data["state_label"] = STATE_LABELS.get(str(data.get("state", "")).upper(), data.get("state"))
        return data


def serve_file(path: str, host: str, port: int) -> None:
    """Démarre le tableau de bord en lecture d'un fichier d'état existant."""
    provider = FileStatusProvider(path)

    class Handler(_DashboardHandler):
        pass

    Handler.provider = staticmethod(provider)
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    display_host = host if host not in ("0.0.0.0", "::") else "127.0.0.1"
    print(f"Tableau de bord (fichier {path}) : http://{display_host}:{port}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nArrêt du tableau de bord.")
    finally:
        httpd.server_close()


DEMO_COUNTDOWN_SECONDS = 75


def _demo_cycle(hub: "StatusHub", stop: threading.Event) -> None:
    """Un cycle complet simulé : démarrage, compte à rebours, surveillance."""

    def wait(seconds: float) -> bool:
        """Attend en petits pas ; retourne False si l'arrêt est demandé."""
        deadline = time.time() + seconds
        while time.time() < deadline:
            if stop.is_set():
                return False
            time.sleep(min(0.2, max(0.0, deadline - time.time())))
        return not stop.is_set()

    # --- Démarrage ---
    hub.set(
        state="DEMARRAGE",
        session="inconnue",
        slots_found=[],
        checks=0,
        failures=0,
        recoveries=0,
        relogins=0,
        next_month_scans=0,
        month_displayed="",
        month_scanned="",
        last_check_at=None,
        preflight={"done": False, "session_ok": None, "calendar_ok": None, "message": ""},
        state_detail="",
    )
    hub.event("Préparation du navigateur Chrome…")
    if not wait(2):
        return
    hub.event("Démarrage via undetected-chromedriver (mode stealth actif)…", "ok")
    hub.event("Chrome démarré — session conservée par le profil Chrome", "ok")

    # --- Compte à rebours (raccourci pour la démo) ---
    target = time.time() + DEMO_COUNTDOWN_SECONDS
    hub.set(
        state="COMPTE_A_REBOURS",
        target_time="16:55",
        target_ts=target,
        target_window_seconds=DEMO_COUNTDOWN_SECONDS,
        seconds_remaining=DEMO_COUNTDOWN_SECONDS,
    )
    hub.event("En attente de 16:55…")
    if not wait(6):
        return

    # --- Self-check ---
    hub.set(state="SELF_CHECK", state_detail="T-30 min")
    hub.event("SELF-CHECK (T-30 min) — vérification avant l'ouverture des créneaux")
    if not wait(1.5):
        return
    hub.event("SELF-CHECK : session toujours active ✔", "ok")
    if not wait(1.5):
        return
    hub.event("SELF-CHECK : calendrier accessible ✔", "ok")
    hub.set(
        session="active",
        preflight={"done": True, "session_ok": True, "calendar_ok": True, "message": "T-30 min"},
        state="COMPTE_A_REBOURS",
        state_detail="",
    )

    # --- Contrôle final, puis attente de l'ouverture ---
    while not stop.is_set():
        remaining = max(0.0, target - time.time())
        hub.set(seconds_remaining=remaining)
        if remaining <= 20 and not hub.snapshot()["state_detail"]:
            hub.set(state_detail="contrôle final T-2 min")
            hub.event("CONTRÔLE FINAL : navigateur vivant, session en place ✔", "ok")
        if remaining <= 0:
            break
        time.sleep(0.5)
    if stop.is_set():
        return

    # --- Surveillance ---
    hub.set(
        state="SURVEILLANCE",
        state_detail="intervalle ~5 s",
        seconds_remaining=0,
        month_displayed="septembre 2026",
        current_url="https://algeria.blsinternational.com/es/fr/appointment",
    )
    hub.event("Heure cible atteinte (16:55), on continue.", "ok")
    hub.event("Surveillance active (intervalle moyen ~5,0 s avec jitter).")

    while not stop.is_set():
        hub.incr("checks")
        hub.set(last_check_at=_now_iso(), next_check_in=5.0, failures=0)
        checks = hub.snapshot()["checks"]

        if checks % 2 == 0:
            hub.set(month_scanned="octobre 2026")
            hub.incr("next_month_scans")
            if not wait(0.8):
                return
            hub.set(month_scanned="")

        if checks == 4:
            hub.set(state="SESSION_EXPIREE", session="expirée", state_detail="")
            hub.event("SESSION EXPIRÉE — le site t'a déconnecté pendant la surveillance !", "warn")
            if not wait(2):
                return
            hub.set(state="RECONNEXION", state_detail="automatique")
            hub.event("Reconnexion automatique avec les identifiants du .env…")
            if not wait(2):
                return
            hub.incr("relogins")
            hub.set(session="active", state="SURVEILLANCE", state_detail="intervalle ~5 s")
            hub.event("Reconnexion automatique réussie — surveillance reprise.", "ok")

        if checks == 7:
            hub.set(failures=3)
            hub.event("Chargement trop lent (> 45 s) — échec consécutif 3/5", "warn")
            if not wait(1.2):
                return

        if checks >= 10:
            hub.set(month_displayed="octobre 2026", failures=0, month_scanned="octobre 2026")
            hub.slot_found(["2026-10-07", "2026-10-09", "2026-10-14"], "octobre 2026")
            hub.event("Jour(s) disponible(s) détecté(s) sur le MOIS SUIVANT", "slot")
            hub.event("Premier jour disponible présélectionné — fenêtre au premier plan.", "ok")
            hub.event("CRÉNEAU DÉTECTÉ — à toi de jouer dans Chrome", "slot")
            if not wait(12):
                return
            break

        if not wait(1.6):
            return


def run_demo(host: str, port: int) -> None:
    """Simule une séance de surveillance pour prévisualiser l'interface."""
    hub = StatusHub(enabled=True, host=host, port=port, account_email="demo@example.com")
    hub.set(current_url="https://algeria.blsinternational.com/es/fr/appointment")
    url = hub.start()
    if url is None:
        raise SystemExit(f"Port {port} déjà utilisé — essaie --port {port + 1}.")
    print(f"Démo du tableau de bord : {url}  (Ctrl+C pour arrêter)", flush=True)
    print("Données simulées : aucun site n'est contacté, aucun Chrome n'est lancé.", flush=True)

    stop = threading.Event()

    def scenario() -> None:
        while not stop.is_set():
            _demo_cycle(hub, stop)
            if stop.is_set():
                return
            hub.event("Nouveau cycle de démonstration…")
            for _ in range(10):
                if stop.is_set():
                    return
                time.sleep(0.3)

    worker = threading.Thread(target=scenario, name="bls-viewer-demo", daemon=True)
    worker.start()
    try:
        while not stop.is_set():
            worker.join(timeout=0.5)
    except KeyboardInterrupt:
        stop.set()
        print("\nArrêt de la démo.")
    finally:
        hub.stop()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Tableau de bord local de l'assistant BLS (lecture seule)."
    )
    parser.add_argument("--demo", action="store_true",
                        help="affiche l'interface avec des données simulées")
    parser.add_argument("--status-file", default=None,
                        help=f"lit l'état depuis ce fichier JSON (défaut : {STATUS_FILENAME})")
    parser.add_argument("--host", default=DEFAULT_HOST,
                        help=f"interface d'écoute (défaut : {DEFAULT_HOST})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"port d'écoute (défaut : {DEFAULT_PORT})")
    args = parser.parse_args(argv)

    if args.demo:
        run_demo(args.host, args.port)
        return 0

    script_dir = os.path.dirname(os.path.abspath(__file__))
    path = args.status_file or os.path.join(script_dir, STATUS_FILENAME)
    if not os.path.exists(path):
        print(
            f"Avertissement : {path} n'existe pas encore.\n"
            "Lance d'abord bls_espagne_prefill.py (il écrit ce fichier), ou utilise --demo."
        )
    serve_file(path, args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
