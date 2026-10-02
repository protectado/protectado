# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Arnaud Ortais
# Dual-licensed: AGPL-3.0 (open source) or Commercial License — see LICENSE and LICENSE-COMMERCIAL.
import io
import mimetypes
import ipaddress
import json
import os
import re
import subprocess
import unicodedata
import zipfile
import asyncio
import secrets
import socket
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, date as _date
from fastapi import FastAPI, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

import database as db
import access_grid
import modes
import privacy
import passphrase
import secours
import uplink_watch
import wifi_keys
from monitor import ProtectadoMonitor, VPN_ALERTS_ALERT, VPN_ALERTS_VALUES, vpn_alerts
from paths import CONFIG_PATH, DATA_DIR, DB_PATH as _DB_PATH, ACTION_QUEUE_DIR
import scheduler as _scheduler
from scheduler import get_slot_at
from access_control import apply_device_access, enforcement_mode
import station_identity
import action_queue
from station_identity import KIDS_NET
from domain_names import root_domain
from pihole_api import is_real_device_ip
import claude_agent
import threading as _threading



@asynccontextmanager
async def _lifespan(_app):
    """Démarrage et arrêt du moniteur avec l'application, et nulle part ailleurs.

    Un seul moniteur par processus : un second doublerait compteurs, événements et
    escalades IA, et ne recevrait pas reload_config(). Pendant l'assistant (boîtier non
    configuré), il n'y a pas de moniteur du tout.
    """
    global monitor
    if _is_configured():
        _startup()
    else:
        print("[Démarrage] boîtier NON configuré — assistant d'onboarding (pas de monitor)")
    yield
    if monitor:
        monitor.stop()
        monitor = None


def _init_mimetypes():
    """Table des types MIME intégrée à Python, sans lire /etc/mime.types.

    Python lit les fichiers de types du système au premier fichier statique servi. La
    sandbox de l'agent n'autorise pas /etc/mime.types : la lecture levait une
    PermissionError et /static répondait 500 (indicateur de solidité de clé cassé). La
    table intégrée couvre tout ce que /static sert.

    Vider knownfiles AVANT init : init(files=[]) relirait quand même ces fichiers lors de
    la première initialisation.
    """
    mimetypes.knownfiles = []
    mimetypes.init()


_init_mimetypes()
app = FastAPI(title="Protectado", lifespan=_lifespan)
templates = Jinja2Templates(directory="templates")
# /static : le middleware l'autorisait déjà depuis toutes les postures (assistant, réseau
# enfants) mais rien ne le servait. Sert le code partagé entre l'assistant et le tableau
# de bord — aujourd'hui l'estimation de solidité d'une clé Wi-Fi, qui doit dire la même
# chose aux deux endroits.
app.mount("/static", StaticFiles(directory="static"), name="static")

monitor: ProtectadoMonitor | None = None

_status_cache: dict = {}          # {"data": ..., "ts": datetime}
_STATUS_CACHE_TTL = 8             # secondes — légèrement sous le refresh SSE (10s)
_ai_key_invalid: bool = False     # True si une 401 OpenRouter a été reçue depuis le dernier redémarrage

# Sessions : token → expiry datetime. La session se ferme SESSION_TTL après la dernière
# action de l'utilisateur. Les requêtes que la page fait seule (flux de mise à jour,
# suivi d'une mise à jour du boîtier) ne comptent pas : constaté, un onglet laissé
# ouvert restait connecté indéfiniment, et un enfant s'en est servi.
_sessions: dict[str, datetime] = {}
SESSION_TTL = timedelta(minutes=10)
AUTO_HEADER = "X-Protectado-Auto"    # posé par la page sur ses requêtes automatiques

# Rate-limiting login : ip → liste de timestamps de tentatives
_login_attempts: dict[str, list[float]] = {}
LOGIN_WINDOW_SEC  = 300   # fenêtre 5 minutes
LOGIN_MAX_ATTEMPTS = 10   # max 10 tentatives par fenêtre

SUPPORTED_LANGS = ["fr", "en", "es", "pt"]
_PROFILE_KEY_RE = re.compile(r'^[a-z0-9_]{1,64}$')
_DOMAIN_RE      = re.compile(r'^(?:[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$')
# Les valeurs acceptées viennent de modes.py. Elles étaient écrites ici en double, et
# l'une des deux listes contenait « free » ET « permissive », c'est-à-dire deux noms pour
# le même mode : c'est ce doublon que le renommage a supprimé.
_VALID_OVERRIDE_MODES = set(modes.OVERRIDE_MODES)
_VALID_MODES = set(modes.SLOT_MODES)
_translations_cache: dict[str, dict] = {}


def _load_translations(lang: str) -> dict:
    if lang not in SUPPORTED_LANGS:
        lang = "fr"
    if lang not in _translations_cache:
        path = os.path.join(os.path.dirname(__file__), "i18n", f"{lang}.json")
        try:
            with open(path, encoding="utf-8") as f:
                _translations_cache[lang] = json.load(f)
        except FileNotFoundError:
            _translations_cache[lang] = _load_translations("fr")
    return _translations_cache[lang]


def _lang() -> str:
    try:
        lang = _load_config().get("language", "fr")
        return lang if lang in SUPPORTED_LANGS else "fr"
    except Exception:
        return "fr"

def _t(lang: str | None = None) -> dict:
    return _load_translations(lang if lang is not None else _lang())


def _load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return json.load(f)


def _save_config(config: dict):
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    os.replace(tmp, CONFIG_PATH)
    if monitor:
        monitor.reload_config()


def _is_auto_request(request: Request) -> bool:
    """Requête faite par la page seule, sans action de l'utilisateur. EventSource ne
    sait pas poser d'en-tête : le flux est reconnu à son chemin."""
    return request.url.path == "/api/stream" or AUTO_HEADER in request.headers


def _check_session(request: Request) -> bool:
    """Session valide ? Ne la prolonge que pour une requête de l'utilisateur."""
    token = request.cookies.get("fw_session")
    if not token or token not in _sessions:
        return False
    if datetime.now() > _sessions[token]:
        del _sessions[token]
        return False
    if not _is_auto_request(request):
        _sessions[token] = datetime.now() + SESSION_TTL  # fenêtre glissante
    return True


def _session_alive(token: str | None) -> bool:
    """Lecture seule, pour le flux : il vérifie à chaque envoi sans jamais prolonger."""
    return bool(token) and token in _sessions and datetime.now() <= _sessions[token]


def _record_login_attempt(ip: str) -> bool:
    """
    Enregistre une tentative de login depuis ip.
    Retourne False si le rate-limit est atteint.
    """
    now = datetime.now().timestamp()
    attempts = [t for t in _login_attempts.get(ip, []) if now - t < LOGIN_WINDOW_SEC]
    if len(attempts) >= LOGIN_MAX_ATTEMPTS:
        _login_attempts[ip] = attempts
        return False
    attempts.append(now)
    _login_attempts[ip] = attempts
    return True


def _slugify(name: str) -> str:
    name = unicodedata.normalize("NFKD", name)
    name = "".join(c for c in name if not unicodedata.combining(c))
    name = re.sub(r"[^a-z0-9]+", "_", name.lower())
    return name.strip("_")

def _is_kids_client(request: Request) -> bool:
    """Le client est-il sur le réseau enfants ? Si oui, le dashboard de configuration
    est INTERDIT — on ne lui sert que la page info-IP (protectado.admin)."""
    host = request.client.host if request.client else ""
    try:
        return ipaddress.ip_address(host) in KIDS_NET
    except ValueError:
        return False


def _uplink_ip() -> str:
    """IP côté box (wlan_up) = adresse du dashboard à donner au parent. Lue en direct,
    donc suit un changement de bail DHCP. Vide si indisponible."""
    try:
        out = subprocess.run(["ip", "-4", "addr", "show", "wlan_up"],
                             capture_output=True, text=True, timeout=5).stdout
        m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", out)
        return m.group(1) if m else ""
    except Exception:
        return ""


_VERSION_FILE = os.path.join(DATA_DIR, "version.json")


def _version_info() -> dict:
    """Version déployée : commit court, branche suivie, date du dernier alignement.

    Lue dans data/version.json, écrit par bootstrap.sh et protectado-update.sh à chaque
    « git reset » réussi. On n'interroge PAS git ici : l'agent tourne sous nono, qui ne
    lui donne ni le binaire git ni le dépôt en écriture — un appel échouerait, et le
    faire échouer silencieusement à chaque chargement de page serait pire que rien.

    Renvoie des chaînes vides quand le fichier manque (installation antérieure à son
    introduction) : l'interface affiche alors un tiret, jamais une erreur.
    """
    data = _read_json(_VERSION_FILE)
    return {
        "commit":     str(data.get("commit") or ""),
        "branch":     str(data.get("branch") or ""),
        "updated_at": str(data.get("updated_at") or ""),
    }


def _local_ip_facing(peer: str = "") -> str:
    """IPv4 locale par laquelle ce boîtier est joignable DEPUIS `peer`.

    En mode DNS, l'adresse à saisir dans la box ne peut pas venir de _uplink_ip() : cette
    fonction lit wlan_up, qui n'existe qu'en posture passerelle. Elle ne peut pas non plus
    venir de location.hostname côté client : depuis l'ajout du mDNS, un parent qui suit la
    documentation ouvre http://protectado.local et se verrait proposer « protectado.local »
    comme serveur DNS — qu'aucune box n'accepte à cet endroit. Le parent le plus discipliné
    serait le seul à échouer.

    On demande donc la réponse au noyau : un socket UDP « connecté » ne transmet aucun
    paquet, il se contente de résoudre la route et de fixer l'adresse source. C'est
    exactement l'interface par laquelle le client nous parle, y compris quand la machine
    en a plusieurs.
    """
    targets = []
    try:
        if peer and ipaddress.ip_address(peer).version == 4 and not ipaddress.ip_address(peer).is_loopback:
            targets.append(peer)
    except ValueError:
        pass
    # Repli : l'adresse source de la route par défaut. Aucun paquet n'est émis, donc
    # aucune connectivité Internet n'est requise pour que ça fonctionne.
    targets.append("1.1.1.1")
    for target in targets:
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(1)
            sock.connect((target, 9))          # port discard — rien n'est envoyé en UDP
            ip = sock.getsockname()[0]
            if ip and not ip.startswith("0."):
                return ip
        except OSError:
            continue
        finally:
            if sock is not None:
                sock.close()
    return ""


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path

    # Réseau enfants (mode gateway) : le tableau de bord de configuration N'EST PAS
    # accessible. On ne sert QUE la page qui rappelle l'adresse (protectado.admin → info-IP).
    host = (request.headers.get("host") or "").split(":")[0]
    # Mode secours avec réseau enfants : la SEULE exception à cette interdiction. Toute
    # URL mène à la page de secours, ce qui fait aussi le portail captif (les tests de
    # connectivité des téléphones reçoivent une redirection et ouvrent la page).
    if _is_kids_client(request) and _secours_kids():
        if (path == "/secours" or path.startswith("/api/secours")
                or path.startswith("/static") or path.startswith("/api/i18n")):
            return await call_next(request)
        return RedirectResponse(url="/secours", status_code=302)
    if _is_kids_client(request) or host == "protectado.admin":
        # /secours passe aussi, pour répondre 404 hors mode secours (cf. secours_page).
        if (path == "/admin-info" or path == "/api/child/status"
                or path == "/secours" or path.startswith("/api/secours")
                or path.startswith("/static") or path.startswith("/api/i18n")):
            return await call_next(request)
        return RedirectResponse(url="/admin-info", status_code=302)

    # Contrôle de santé des scripts de mise à jour : sans session, quelle que soit la
    # posture (il sait répondre pendant l'assistant), et il ne révèle rien.
    if path == "/api/health":
        return await call_next(request)

    # Posture CONFIG d'un boîtier DÉJÀ configuré, en mode secours (cas B) : le réseau
    # Protectado-Setup est ouvert à tous, donc l'assistant normal n'est PAS servi. Il
    # réécrirait le mot de passe parent en gardant les profils (apply_configuration) :
    # n'importe qui à portée prendrait la main sur les règles sans que rien n'y paraisse.
    # La page de secours le remplace : reconnecter (mot de passe parent) ou tout effacer.
    if not _is_configured() and _secours_active():
        if (path == "/secours" or path.startswith("/api/secours")
                or path.startswith("/static") or path.startswith("/api/i18n")):
            return await call_next(request)
        return RedirectResponse(url="/secours", status_code=302)

    # Posture CONFIG (boîtier NON configuré) : captive portal — toute URL mène à l'assistant.
    if not _is_configured():
        if (path == "/onboarding" or path.startswith("/api/onboarding")
                or path.startswith("/api/i18n") or path.startswith("/static")):
            return await call_next(request)
        return RedirectResponse(url="/onboarding", status_code=302)

    # Boîtier configuré : plus d'onboarding, flux normal authentifié.
    if path == "/onboarding" or path == "/setup" or path.startswith("/api/onboarding"):
        return RedirectResponse(url="/", status_code=302)
    if path.startswith("/api/i18n"):
        return await call_next(request)
    # Feuille de style et scripts de thème : servis AVANT le contrôle de session, sinon
    # la page de connexion — qui est justement servie sans session — se retrouvait sans
    # ses couleurs, chaque jeton étant introuvable. Ces fichiers ne portent aucune donnée.
    if path.startswith("/static"):
        return await call_next(request)
    if path == "/login":
        return await call_next(request)
    if not _check_session(request):
        if path.startswith("/api/"):
            return JSONResponse({"ok": False, "error_code": "session_expired",
                                 "error": "session_expired"}, status_code=401)
        return RedirectResponse(url="/login", status_code=302)
    return await call_next(request)


class MonitorUnavailable(RuntimeError):
    """Le moniteur n'a pas démarré : dashboard en mode dégradé."""


@app.exception_handler(MonitorUnavailable)
async def _monitor_unavailable(request: Request, exc: MonitorUnavailable):
    return JSONResponse({"ok": False, "error_code": "monitor_unavailable",
                         "error": "Surveillance non démarrée"}, status_code=503)


def _start_monitor() -> ProtectadoMonitor:
    """Construit et lance LE moniteur du processus. Appelé au démarrage seulement."""
    global monitor
    if monitor is None:
        m = ProtectadoMonitor()
        try:
            m.pihole.setup_profiles(m.config["profiles"])
        except Exception as e:
            print(f"[Setup] Avertissement Pi-hole setup : {e}")
        m.start(interval=60)
        monitor = m
    return monitor


def get_monitor() -> ProtectadoMonitor:
    """Le moniteur démarré avec l'application. Une requête n'en crée jamais : sans lui,
    l'appelant reçoit MonitorUnavailable (réponse 503)."""
    if monitor is None:
        raise MonitorUnavailable("monitor non démarré")
    return monitor


# ------------------------------------------------------------------ #
#  Auth                                                               #
# ------------------------------------------------------------------ #

@app.get("/api/i18n/{lang}")
async def get_translations(lang: str):
    if lang not in SUPPORTED_LANGS:
        lang = "fr"
    return JSONResponse(_load_translations(lang))


# ------------------------------------------------------------------ #
#  Onboarding (posture CONFIG) — assistant premier démarrage.         #
#  L'assistant (sandboxé) met des actions en file (scan/validate/     #
#  finish) ; action_runner (root) les exécute. Résultats dans         #
#  data/*.json. AUCUNE op réseau ici (séparation de privilèges).      #
# ------------------------------------------------------------------ #

_PENDING = os.path.join(DATA_DIR, "pending_config.json")
_POSTURE = os.path.join(DATA_DIR, "posture.json")   # capacité détectée par l'orchestrateur (root)


def _detected_mode() -> str:
    """Mode que le matériel permet ('gateway'|'dns_only'), écrit au boot par
    protectado-boot.sh. Défaut prudent 'dns_only' si le fichier manque."""
    m = _read_json(_POSTURE).get("mode")
    return m if m in ("gateway", "dns_only") else "dns_only"

_PAIRING_FILE = os.path.join(DATA_DIR, "pairing_code")


def _pairing_code() -> str:
    """Code d'appairage écrit par bootstrap.sh et affiché à l'installateur.

    Il ferme la fenêtre pendant laquelle l'assistant est joignable sans authentification :
    tant que configured != true, il n'y a pas encore de mot de passe parent à vérifier.
    En posture gateway l'assistant n'est atteignable que depuis l'AP isolé
    Protectado-Setup, donc rien à protéger ; en dns_only le boîtier répond à TOUT le
    réseau de la maison, et le premier arrivé — y compris l'appareil d'un enfant —
    pouvait définir le mot de passe parent avant le parent lui-même.
    """
    try:
        with open(_PAIRING_FILE) as f:
            return f.read().strip()
    except OSError:
        return ""


def _pairing_required() -> bool:
    # Uniquement en dns_only, et uniquement si bootstrap a bien posé un code : une
    # installation antérieure à cette version n'en a pas, et doit rester configurable.
    return _detected_mode() == "dns_only" and bool(_pairing_code())


def _is_configured() -> bool:
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f).get("configured") is True
    except Exception:
        return False

def _queue(action: str, args: dict):
    """Dépose une action pour le runner root. Lève OSError si la file est inaccessible.

    La file est créée par le runner en root:protectado-queue 2770 ; ce processus doit
    appartenir au groupe pour y écrire (cf. SupplementaryGroups dans l'unité systemd).
    Un échec silencieux ici laissait l'assistant tourner sans fin : il est journalisé
    et relevé (cf. action_queue).
    """
    try:
        action_queue.enqueue(action, args)
    except OSError as e:
        print(f"[Dashboard] File d'actions inaccessible ({ACTION_QUEUE_DIR}) — "
              f"action '{action}' NON transmise au runner : {e}")
        raise


def _queue_error(action: str, e: Exception) -> JSONResponse:
    """Réponse d'erreur explicite quand le runner n'a pas pu être sollicité."""
    return JSONResponse(
        {"ok": False,
         "error_code": "queue_unavailable",
         # Le texte reste en français : il sert au diagnostic et aux logs. C'est
         # error_code que le client traduit, avec {action} en paramètre.
         "error_params": {"action": action},
         "error": f"file d'actions inaccessible — le service privilégié n'a pas reçu "
                  f"« {action} » ({e.__class__.__name__}). Vérifier l'appartenance au "
                  f"groupe protectado-queue et relancer bootstrap.sh."},
        status_code=500)

def _read_json(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}

KIDS_GATEWAY_IP = "192.168.50.1"      # le boîtier lui-même sur le réseau enfants


def _device_label(ip: str, custom_names: dict, hostnames: dict) -> str:
    """Comment DÉSIGNER un appareil au parent, dans cet ordre de préférence.

    1. le nom que le parent lui a donné, qui fait toujours autorité ;
    2. le nom que l'appareil annonce au DHCP, remonté avec l'identité de la station ;
    3. son adresse, en dernier recours.

    Une adresse ne dit rien à personne, et elle change à chaque rotation de MAC : la
    désigner ainsi à un endroit et par son nom à un autre force le parent à faire le
    rapprochement lui-même.
    """
    return (custom_names or {}).get(ip) or (hostnames or {}).get(ip) or ip


def _config_with_identity() -> dict:
    """config.json, enrichie des appareils reconnus par leur clé Wi-Fi.

    En passerelle, la liste d'appareils d'un profil n'est plus saisie par le parent : elle
    est LUE sur le réseau (cf. station_identity.py), donc elle ne vit pas dans le fichier.
    Les endpoints qui lisent la configuration sur disque doivent l'enrichir, sinon
    l'interface annonce « 0 appareil » pour un enfant connecté, et le mode adulte ne
    retrouve plus le profil d'une adresse.

    Les endpoints qui passent par le monitor (`m.config`) n'en ont pas besoin : le cycle
    y a déjà appliqué l'identité.
    """
    config = _load_config()
    if (config.get("network") or {}).get("enforcement") == "gateway":
        station_identity.apply_to_config(
            config,
            station_identity.load(os.path.join(DATA_DIR, station_identity.IDENTITY_NAME)),
        )
    return config


def _unassigned_devices(devices: list, config: dict) -> list:
    """Appareils ni rattachés à un profil, ni écartés par le parent, hors boîtier.

    Ne concerne plus que la posture DNS seule. En passerelle, un appareil est reconnu par
    la clé Wi-Fi qu'il a utilisée (cf. station_identity.py) : il n'y a plus d'appareil à
    rattacher à la main, et un appareil sans clé valide ne rejoint pas le réseau.
    """
    assigned = {d.get("ip") for p in (config.get("profiles") or {}).values()
                for d in ((p or {}).get("devices") or [])}
    ignored = set(config.get("ignored_devices") or [])
    return [d for d in devices
            if d.get("ip") and d["ip"] not in assigned
            and d["ip"] not in ignored and d["ip"] != KIDS_GATEWAY_IP]


def _gen_passphrase() -> str:
    """Clé Wi-Fi proposée au parent — cf. passphrase.py pour la forme et la force."""
    return passphrase.generate()

@app.get("/onboarding", response_class=HTMLResponse)
async def onboarding_page(request: Request):
    return templates.TemplateResponse(request, "onboarding.html", {})

@app.get("/admin-info", response_class=HTMLResponse)
async def admin_info(request: Request):
    # Page servie sur le réseau enfants (protectado.admin) : rappelle SEULEMENT l'adresse
    # du tableau de bord, jamais le tableau de bord lui-même. Sans authentification.
    lang = _lang()
    return templates.TemplateResponse(request, "admin_info.html",
        {"ip": _uplink_ip(), "t": _load_translations(lang), "lang": lang})

# ------------------------------------------------------------------ #
#  Page de secours (cf. secours.py) : réseau enfants, mode secours    #
# ------------------------------------------------------------------ #

def _secours_kids() -> bool:
    etat = secours.load(os.path.join(DATA_DIR, secours.STATE_NAME))
    return etat["active"] and etat["case"] == secours.KIDS


def _secours_active() -> bool:
    """Mode secours, dans un cas ou dans l'autre. Sans réseau enfants (cas B), la page
    de secours est servie à la place de l'assistant, sur Protectado-Setup."""
    return secours.load(os.path.join(DATA_DIR, secours.STATE_NAME))["active"]


def _secours_absent():
    return JSONResponse({"ok": False, "error_code": "not_found"}, status_code=404)


def _secours_attempts_path() -> str:
    return os.path.join(DATA_DIR, secours.ATTEMPTS_NAME)


def _secours_lock() -> int:
    """Secondes de blocage restantes des essais du mot de passe parent."""
    reste, etat = secours.lock_remaining(secours.load_attempts(_secours_attempts_path()),
                                         time.monotonic(), secours.boot_id())
    if etat != secours.load_attempts(_secours_attempts_path()):
        secours.save_attempts(_secours_attempts_path(), etat)
    return reste


@app.get("/secours", response_class=HTMLResponse)
async def secours_page(request: Request):
    etat = secours.load(os.path.join(DATA_DIR, secours.STATE_NAME))
    if not etat["active"]:
        return HTMLResponse("", status_code=404)
    lang = _lang()
    return templates.TemplateResponse(request, "secours.html",
                                      {"t": _load_translations(lang), "lang": lang,
                                       "cas": etat["case"]})


@app.get("/api/secours/state")
async def secours_state():
    if not _secours_active():
        return _secours_absent()
    etat = secours.load(os.path.join(DATA_DIR, secours.STATE_NAME))
    config = _load_config()
    enfants = ((config.get("network") or {}).get("kids") or {}).get("ssid", "")
    scan = _read_json(os.path.join(DATA_DIR, "wifi_scan.json"))
    reset_at = etat.get("reset_at")
    return JSONResponse({
        # Le réseau enfants du boîtier lui-même n'est pas une box à laquelle se relier.
        "networks": [n for n in scan.get("networks") or [] if n.get("ssid") != enfants],
        "scan_done": bool(scan),
        "lock_sec": _secours_lock(),
        "reset_in": (max(0, int(reset_at - time.monotonic())) if reset_at else None),
        "validation": _read_json(os.path.join(DATA_DIR, "box_validation.json")),
    })


@app.post("/api/secours/scan")
async def secours_scan():
    if not _secours_active():
        return _secours_absent()
    try:
        os.remove(os.path.join(DATA_DIR, "wifi_scan.json"))
    except OSError:
        pass
    try:
        _queue("scan_wifi", {})
    except OSError as e:
        return _queue_error("scan_wifi", e)
    return JSONResponse({"ok": True})


class SecoursReconnect(BaseModel):
    ssid: str
    key: str = ""
    password: str = ""


@app.post("/api/secours/reconnect")
async def secours_reconnect(body: SecoursReconnect):
    """Le mot de passe parent est vérifié ICI, avant tout test de connexion, avec la
    limitation des essais ; le runner le revérifie avant d'agir."""
    if not _secours_active():
        return _secours_absent()
    reste = _secours_lock()
    if reste:
        return JSONResponse({"ok": False, "error_code": "too_many_attempts",
                             "error_params": {"sec": reste}}, status_code=429)
    chemin = _secours_attempts_path()
    attendu = str(_load_config().get("dashboard_password") or "")
    if not attendu or not secrets.compare_digest(body.password, attendu):
        secours.save_attempts(chemin, secours.failed(secours.load_attempts(chemin),
                                                     time.monotonic(), secours.boot_id()))
        return JSONResponse({"ok": False, "error_code": "bad_password"}, status_code=403)
    secours.save_attempts(chemin, secours.succeeded())
    try:
        os.remove(os.path.join(DATA_DIR, "box_validation.json"))     # « en cours »
    except OSError:
        pass
    try:
        _queue("secours_reconnect", {"ssid": body.ssid, "key": body.key,
                                     "password": body.password})
    except OSError as e:
        return _queue_error("secours_reconnect", e)
    return JSONResponse({"ok": True})


class SecoursReset(BaseModel):
    confirm: bool = False


@app.post("/api/secours/reset")
async def secours_reset(body: SecoursReset):
    """Sans mot de passe (cas « mot de passe oublié »), mais avec une confirmation
    explicite, et le runner attend son délai d'annulation avant d'effacer."""
    if not _secours_active():
        return _secours_absent()
    if not body.confirm:
        return JSONResponse({"ok": False, "error_code": "confirmation_required"},
                            status_code=400)
    try:
        _queue("secours_reset", {})
    except OSError as e:
        return _queue_error("secours_reset", e)
    return JSONResponse({"ok": True})


@app.post("/api/secours/reset/cancel")
async def secours_reset_cancel():
    if not _secours_active():
        return _secours_absent()
    try:
        _queue("secours_reset_cancel", {})
    except OSError as e:
        return _queue_error("secours_reset_cancel", e)
    return JSONResponse({"ok": True})


def _uplink_lost() -> dict | None:
    """Depuis combien de temps le boîtier ne joint plus la box, avant le mode secours :
    l'enfant (et le parent qui passe) l'apprennent ici. Le délai du basculement n'est
    pas donné : clé refusée, il est court, et l'annoncer n'apprend rien d'utile."""
    etat = uplink_watch.load(os.path.join(DATA_DIR, uplink_watch.STATE_NAME))
    if etat["connected"] or _secours_active():
        return None
    return {"h": etat["offline_sec"] // 3600, "min": etat["offline_sec"] % 3600 // 60}


@app.get("/api/child/status")
async def child_status(request: Request):
    """État affiché à l'ENFANT sur sa propre page (protectado.admin).

    Servie sans authentification : être sur le réseau enfants suffit. Le produit se
    réclame de la confiance et du dialogue ; un enfant doit pouvoir savoir seul pourquoi
    un site ne répond pas, quelles sont les règles, et ce qui est enregistré.

    NE CONTIENT JAMAIS : l'historique de navigation (un frère ou une sœur a accès à
    cette page), la liste des appareils, une clé Wi-Fi, un identifiant, ni quoi que ce
    soit du profil d'un AUTRE enfant. Mode en cours, règles, politique — pas de données.

    NE SIGNALE PAS NON PLUS qu'un parent a consulté le détail de l'historique. Prévenir
    l'enfant lui apprendrait surtout QUAND ses parents regardent, donc quand ils ne
    regardent pas : un adolescent qui veut se cacher n'a plus qu'à lire cette page pour
    savoir à quel moment il est observé. La consultation reste tracée, mais dans le
    journal d'événements du parent, pas ici.
    """
    ip = request.client.host if request.client else ""
    config = _config_with_identity()
    key = _find_profile_for_ip(config, ip)
    out = {
        "recognized": bool(key),
        "retention_days": privacy.retention_days(config),
        "share_with_ai": privacy.share_with_ai(config),
        # Ce que le boîtier enregistre, en une phrase — rendu côté client via i18n.
        "records": "domains_only",
        "uplink_lost": _uplink_lost(),
    }
    if not key:
        return JSONResponse(out)

    profile = config["profiles"][key]
    now = datetime.now()
    slot = get_slot_at(key, now)
    today = _DAY_KEYS[now.weekday()]
    out.update({
        "name":  profile.get("name", ""),      # son PROPRE prénom, sur son propre appareil
        "mode":  slot.get("mode"),
        "slot_start": slot.get("slot_start", ""),
        "slot_end":   slot.get("slot_end", ""),
        "until_midnight": bool(slot.get("until_midnight")),
        # L'enfant a le même droit que le parent à une heure qui veut dire quelque chose :
        # « ça rouvre demain à 7h » plutôt que « coupé jusqu'à 23h59 ».
        "reopens_at": _reopening(key, now),
        # Dit par le serveur, et non redéduit du nom du mode dans la page : quels modes
        # ferment est une propriété du vocabulaire (cf. modes.py), et la recopier dans un
        # gabarit recréerait une des trois définitions concurrentes déjà supprimées.
        "is_closed": not modes.is_open(slot.get("mode")),
        "privacy_level": privacy.level_of(profile),
        # Planning de la journée — les règles, pas les données.
        "today": [
            {"start": sl.get("start"), "end": sl.get("end"), "mode": sl.get("mode")}
            for sl in (profile.get("schedule", {}).get(today) or [])
        ],
        # Semaine complète, en lecture seule. Uniquement CE profil : la page est servie
        # sans authentification à tout le réseau enfants, un frère ou une sœur l'ouvre
        # depuis son propre appareil. Un planning reste une règle, pas une donnée, mais
        # les règles d'un autre enfant ne regardent pas celui qui consulte.
        "today_key": today,
        "week": {
            day: [
                {"start": sl.get("start"), "end": sl.get("end"), "mode": sl.get("mode")}
                for sl in (profile.get("schedule", {}).get(day) or [])
            ]
            for day in _DAY_KEYS
        },
    })
    return JSONResponse(out)


@app.get("/api/onboarding/state")
async def onboarding_state(request: Request):
    return JSONResponse({
        "configured": _is_configured(),
        # Adresse à saisir dans la box en mode DNS. Calculée côté SERVEUR : le client ne
        # peut pas la déduire de location.hostname, qui vaut « protectado.local » dès
        # que le parent suit la documentation.
        "local_ip":   _local_ip_facing(request.client.host if request.client else ""),
        "mode":       _detected_mode(),   # 'gateway' (portail captif) | 'dns_only' (LAN)
        # Booléen seulement : le code lui-même ne sort JAMAIS par l'API — il n'est
        # connu que de la personne qui a vu la fin de l'installation.
        "pairing_required": _pairing_required(),
        "validation": _read_json(os.path.join(DATA_DIR, "box_validation.json")),
        "pending":    os.path.exists(_PENDING),
    })

@app.post("/api/onboarding/scan")
async def onboarding_scan():
    # Effacer le résultat précédent : sinon l'assistant ne peut pas distinguer
    # « scan en cours » d'un ancien résultat (ou d'un ancien échec) resté sur disque.
    try:
        os.remove(os.path.join(DATA_DIR, "wifi_scan.json"))
    except OSError:
        pass
    try:
        _queue("scan_wifi", {})
    except OSError as e:
        return _queue_error("scan_wifi", e)
    return JSONResponse({"ok": True})

@app.get("/api/onboarding/scan")
async def onboarding_scan_result():
    return JSONResponse(_read_json(os.path.join(DATA_DIR, "wifi_scan.json")))

class BoxValidate(BaseModel):
    ssid: str
    key: str = ""

@app.post("/api/onboarding/validate")
async def onboarding_validate(body: BoxValidate):
    try:
        os.remove(os.path.join(DATA_DIR, "box_validation.json"))   # "en cours"
    except OSError:
        pass
    try:
        _queue("validate_box_wifi", {"ssid": body.ssid, "key": body.key})
    except OSError as e:
        return _queue_error("validate_box_wifi", e)
    return JSONResponse({"ok": True})

@app.get("/api/onboarding/validate")
async def onboarding_validate_result():
    return JSONResponse(_read_json(os.path.join(DATA_DIR, "box_validation.json")))

class PrepareBody(BaseModel):
    box_ssid: str = ""          # gateway uniquement
    box_key: str = ""
    kids_ssid: str = ""
    # Plus de clé unique : chaque profil enfant porte la sienne, créée avec le profil
    # (cf. wifi_keys.py). L'assistant ne demande donc plus de clé, et le Wi-Fi des
    # enfants n'est diffusé qu'à partir du premier profil.
    admin_password: str
    language: str = ""          # langue choisie dans l'assistant → config.language
    pairing_code: str = ""      # code d'appairage (dns_only) — cf. _pairing_code()
    timezone: str = ""          # fuseau détecté par le NAVIGATEUR du parent → config.timezone
                                # Tout le produit raisonne en heure locale (créneaux,
                                # coucher, crons) : un fuseau faux décale toutes les règles.
                                # Validé côté runner privilégié, pas ici.
    country: str = ""           # pays choisi dans l'assistant → config.network.country
                                # (domaine réglementaire Wi-Fi : plan de fréquences et
                                #  puissances autorisés — sans lui on émettait en "FR"
                                #  quel que soit le pays d'installation)

@app.post("/api/onboarding/prepare")
async def onboarding_prepare(body: PrepareBody):
    # Enregistre l'intention côté serveur pour que la page "finir" (rechargée via la box
    # en gateway, donc sans état JS) puisse finaliser sans re-saisie.
    # Le MODE est imposé par le matériel détecté (jamais choisi côté client).
    mode = _detected_mode()
    if _pairing_required() and not secrets.compare_digest(
            body.pairing_code.strip().upper(), _pairing_code().strip().upper()):
        return JSONResponse({"ok": False, "error_code": "bad_pairing_code",
                             "error": "Code d'appairage incorrect"}, status_code=403)
    if len(body.admin_password) < 6:
        return JSONResponse({"ok": False, "error_code": "admin_password_too_short", "error": "mot de passe admin trop court"}, status_code=400)
    lang = body.language if body.language in ("fr", "en", "es", "pt") else "fr"
    country = body.country.strip().upper()
    if not (len(country) == 2 and country.isalpha()):
        country = ""            # vide ⇒ pt_country() retombe sur le domaine mondial "00"
    pending = {"mode": mode, "admin_password": body.admin_password,
               "language": lang, "country": country,
               # Transmis tel quel : c'est le runner, qui tourne en root, qui valide.
               "timezone": body.timezone.strip()}
    if mode == "gateway":
        if not body.box_ssid:
            return JSONResponse({"ok": False, "error_code": "box_network_required", "error": "réseau box requis"}, status_code=400)
        kids_ssid = body.kids_ssid or f"{body.box_ssid}-Protectado"
        pending.update({"box_ssid": body.box_ssid, "box_key": body.box_key,
                        "kids_ssid": kids_ssid})
    with open(_PENDING, "w") as f:
        json.dump(pending, f)
    return JSONResponse({"ok": True, "mode": mode, "kids_ssid": pending.get("kids_ssid", "")})

@app.post("/api/onboarding/finish")
async def onboarding_finish():
    pending = _read_json(_PENDING)
    if not pending.get("admin_password"):
        return JSONResponse({"ok": False, "error_code": "no_pending_config", "error": "aucune config en attente"}, status_code=400)
    try:
        _queue("apply_configuration", pending)
    except OSError as e:
        # Surtout NE PAS effacer la config en attente : elle serait perdue alors que
        # rien n'a été appliqué, et le parent devrait tout resaisir.
        return _queue_error("apply_configuration", e)
    try:
        os.remove(_PENDING)
    except OSError:
        pass
    return JSONResponse({"ok": True})


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: int = 0):
    lang = _lang()
    # La version est affichée ici aussi : un parent qui n'arrive plus à se connecter doit
    # pouvoir la lire pour la communiquer au support.
    return templates.TemplateResponse(request, "login.html",
        {"error": error, "t": _load_translations(lang), "lang": lang,
         "version": _version_info()})


@app.post("/login")
async def login(request: Request, password: str = Form(...)):
    client_ip = request.client.host if request.client else "unknown"
    if not _record_login_attempt(client_ip):
        return RedirectResponse(url="/login?error=2", status_code=302)
    config = _load_config()
    if secrets.compare_digest(password, config.get("dashboard_password", "")):
        token = secrets.token_urlsafe(32)
        _sessions[token] = datetime.now() + SESSION_TTL
        response = RedirectResponse(url="/", status_code=302)
        response.set_cookie("fw_session", token, httponly=True, samesite="strict")
        return response
    return RedirectResponse(url="/login?error=1", status_code=302)


@app.post("/logout")
async def logout(request: Request):
    token = request.cookies.get("fw_session")
    if token:
        _sessions.pop(token, None)
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie("fw_session")
    return response


# ------------------------------------------------------------------ #
#  Routes                                                             #
# ------------------------------------------------------------------ #

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    lang = _lang()
    return templates.TemplateResponse(request, "index.html", {"t": _load_translations(lang), "lang": lang})


_DAY_KEYS = ["monday", "tuesday", "wednesday", "thursday", "friday",
             "saturday", "sunday"]


def _reopening(profile_key: str, now: datetime = None) -> dict | None:
    """Quand l'accès rouvre pour ce profil, sous une forme prête à afficher. None si
    l'accès est ouvert, ou si rien n'ouvre dans la semaine qui vient.

    CE QUE ÇA CORRIGE. Quand aucune plage ne couvre l'instant présent, le planificateur
    ferme et rapporte 00:00–23:59 : des bornes qui veulent dire « fermé toute la
    journée ». L'interface les prenait pour une heure de fin et annonçait « coupé jusqu'à
    23h59 » à 21 h, alors que l'accès rouvrait le lendemain à 7 h.

    `day_offset` est rendu plutôt qu'une phrase : le choix entre « à 7h00 », « demain à
    7h00 » et « mercredi à 7h00 » appartient à l'interface, qui connaît la langue du
    parent. Le serveur ne fabrique pas de texte.
    """
    if now is None:
        now = datetime.now()
    quand = _scheduler.reopening_at(profile_key, now)
    if quand is None:
        return None
    return {
        "at":         quand.isoformat(timespec="minutes"),
        "time":       quand.strftime("%H:%M"),
        "day_offset": (quand.date() - now.date()).days,
        "weekday":    _DAY_KEYS[quand.weekday()],
    }


async def _build_status() -> dict:
    """Construit les données de status en appelant Pi-hole."""
    import domain_classifier as _classifier

    loop = asyncio.get_event_loop()
    m = get_monitor()
    config = m.config

    active_ips, queries = await asyncio.gather(
        loop.run_in_executor(None, m.scanner.get_active_ips),
        loop.run_in_executor(None, lambda: m.pihole.get_recent_queries(minutes=5)),
    )
    # Ces requêtes servaient à afficher « requêtes DNS (5 min) », un nombre qui ne disait
    # rien de l'usage réel : un cache DNS rend zéro, une page riche en ressources rend
    # deux cents. Elles servent maintenant à nommer ce que l'enfant fait à l'instant, ce
    # qui est la question que le parent se pose vraiment.
    by_ip = m.pihole.queries_by_client(queries)

    device_names = config.get("device_names") or {}
    profiles_data = {}
    for pname, profile in config["profiles"].items():
        device_ips = [d["ip"] for d in profile.get("devices", [])]
        # Nom annoncé par l'appareil au DHCP, remonté avec l'identité (cf.
        # station_identity). C'est le repère que le parent lit sur la carte du profil
        # (« iPad », « A16-de-celine ») : il doit servir partout, sinon le tableau de bord
        # désigne le même appareil par son nom à un endroit et par son adresse à l'autre.
        hostnames = {d.get("ip"): d.get("hostname")
                     for d in profile.get("devices", []) if d.get("hostname")}
        last_dns = db.get_last_dns(pname)
        last_seen_hours = (
            round((datetime.now() - last_dns).total_seconds() / 3600, 1)
            if last_dns else None
        )

        takeover = None
        for ip in device_ips:
            ov = db.get_device_override(ip)
            if ov:
                expires = datetime.fromisoformat(ov["expires_at"])
                mins_left = max(0, int((expires - datetime.now()).total_seconds() / 60))
                takeover = {"active": True, "ip": ip,
                            "expires_at": ov["expires_at"],
                            "minutes_remaining": mins_left}
                break

        # État d'accès en cours : le mode, et jusqu'à quand. C'est la première question
        # d'un parent qui ouvre le tableau de bord, et elle n'y était nulle part : il
        # fallait aller dans un autre onglet lire une carte « Mode actuel par profil ».
        slot = get_slot_at(pname, datetime.now())
        # Activité RÉELLE, mesurée par le silence radio le plus court parmi ses appareils.
        # Remplace « requêtes DNS (5 min) », qui comptait la bavardise du résolveur et non
        # l'usage : zéro ne voulait pas dire inactif, et deux cents ne voulait pas dire
        # usage intensif. None quand aucun appareil ne la remonte (posture DNS seule, ou
        # pilote qui ne l'expose pas) : inconnu se dit, il ne s'invente pas.
        silences = [d.get("inactive_msec") for d in profile.get("devices", [])
                    if isinstance(d.get("inactive_msec"), int)]
        # Ce qu'il fait EN CE MOMENT : les domaines les plus demandés de la fenêtre de
        # cinq minutes, hors infrastructure partagée dont le nom n'apprend rien au parent.
        # Deux au plus : la carte doit rester lisible d'un coup d'œil.
        courants: dict = {}
        for ip in device_ips:
            for domaine in by_ip.get(ip, []):
                racine = m._root_domain(domaine)
                if racine and not _classifier.is_cdn(racine):
                    courants[racine] = courants.get(racine, 0) + 1
        profiles_data[pname] = {
            "name": profile["name"],
            "mode": slot["mode"],
            "slot_end": slot.get("slot_end", ""),
            "until_midnight": bool(slot.get("until_midnight")),
            "override": bool(slot.get("override")),
            "minutes_to_change": slot.get("next_change_minutes"),
            # Quand l'accès rouvre, plutôt que les bornes de journée d'un créneau fermé.
            "reopens_at": _reopening(pname),
            "idle_seconds": (min(silences) / 1000) if silences else None,
            # Débit en cours, sommé sur ses appareils : moyenne de la dernière minute,
            # calculée par le runner depuis les compteurs de hostapd (posture passerelle).
            # None si aucun appareil n'en remonte : inconnu, pas zéro.
            "rate_down": _somme_debits(profile, "down_bps"),
            "rate_up": _somme_debits(profile, "up_bps"),
            "current_domains": [d for d, _ in sorted(courants.items(),
                                                     key=lambda kv: -kv[1])[:2]],
            # Calculé depuis l'année de naissance : l'interface affiche un âge, la
            # configuration n'en stocke pas.
            "age": privacy.age_of(profile),
            "active_devices": [ip for ip in device_ips if ip in active_ips],
            # Étiquettes affichées au parent, par ordre de préférence : le nom qu'il a
            # donné à l'appareil, sinon celui que l'appareil annonce, sinon l'adresse.
            # Une IP ne dit rien à personne, et le parent a déjà sous les yeux les noms
            # annoncés sur la carte du profil.
            "active_device_labels": [_device_label(ip, device_names, hostnames)
                                     for ip in device_ips if ip in active_ips],
            # Même correspondance pour les écrans qui partent d'une adresse : mode adulte,
            # journal, prise de contrôle.
            "device_labels": {ip: _device_label(ip, device_names, hostnames)
                              for ip in device_ips},
            "device_ips": device_ips,
            "last_seen_hours": last_seen_hours,
            # « Couvre-feu » = l'accès est coupé par le planning, quelle qu'en soit l'heure.
            "is_bedtime": not modes.is_open(slot["mode"]),
            "takeover": takeover,
        }

    # Santé de l'effecteur d'accès. Le runner écrivait déjà data/gateway_status.json…
    # que PERSONNE ne lisait : un boîtier en passerelle dont la couche paquet n'est pas
    # active avait l'air parfaitement sain, Pi-hole classait bien les appareils dans le
    # groupe « bloqué », et aucun paquet n'était coupé. Le défaut ne pouvait se voir
    # qu'en lisant les journaux du service privilégié.
    gw = _read_json(os.path.join(DATA_DIR, "gateway_status.json"))
    maint = _read_json(os.path.join(DATA_DIR, "maintenance.json"))
    enforcement = (config.get("network") or {}).get("enforcement", "dns_only")
    return {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "profiles": profiles_data,
        # Ce qui demande un regard du parent, par opposition au journal, qui dit ce que
        # le boîtier a fait. Servi AVEC le statut et non par un endpoint à part : l'écran
        # doit faire apparaître et disparaître cette carte au même rythme que les cartes
        # des enfants, et un second appel introduirait un décalage entre les deux.
        "alerts": db.get_alerts(),
        # Tunnels probables (VPN) : en cours, ou terminés depuis moins de 48 h. Montrés
        # seulement si le parent l'a choisi (network.vpn_alerts) : par défaut, la
        # détection, pas encore calibrée, n'écrit qu'au journal complet.
        "vpn_sessions": (db.get_vpn_sessions()
                         if vpn_alerts(config) == VPN_ALERTS_ALERT else []),
        "enforcement": {
            "mode":       enforcement,
            # 'ok' | 'error' | 'dns_only' | '' (fichier absent : runner d'une version
            # antérieure, ou jamais démarré depuis le passage en passerelle).
            "state":      gw.get("state", ""),
            "detail":     gw.get("detail", ""),
            "updated_at": gw.get("updated_at", ""),
        },
        # Migrations système appliquées par le runner (cf. system_maintenance) :
        # 'ok' | 'error' | '' (aucune encore jouée). Une migration en échec, c'est un
        # boîtier livré qui n'a pas reçu ce qu'une mise à jour lui demandait.
        "maintenance": {k: maint.get(k, d) for k, d in
                        (("state", ""), ("detail", ""), ("pending", []))},
    }


def _somme_debits(profile: dict, champ: str):
    valeurs = [d.get(champ) for d in (profile.get("devices") or [])
               if isinstance(d.get(champ), (int, float))]
    return sum(valeurs) if valeurs else None


@app.get("/api/status")
async def status():
    global _status_cache
    now = datetime.now()
    cached = _status_cache.get("data")
    if cached and (now - _status_cache["ts"]).total_seconds() < _STATUS_CACHE_TTL:
        return JSONResponse(cached)
    data = await _build_status()
    _status_cache = {"data": data, "ts": now}
    return JSONResponse(data)


@app.get("/api/ai/status")
async def ai_status():
    config = _load_config()
    key = config.get("openrouter", {}).get("api_key", "")
    if not key:
        return JSONResponse({"available": False, "reason": "not_configured"})
    if not privacy.share_with_ai(config):
        # Coupure volontaire du parent : ce n'est ni une panne ni une clé invalide.
        return JSONResponse({"available": False, "reason": "sharing_disabled"})
    if _ai_key_invalid:
        return JSONResponse({"available": False, "reason": "invalid_key"})
    return JSONResponse({"available": True, "reason": "ok"})


@app.get("/api/reports/pending")
async def pending_reports():
    return JSONResponse(db.get_pending_reports())


@app.post("/api/reports/{report_id}/acknowledge")
async def acknowledge_report(report_id: int, request: Request):
    if not _check_session(request):
        return JSONResponse({"ok": False}, status_code=401)
    db.acknowledge_report(report_id)
    return JSONResponse({"ok": True})


@app.get("/api/report")
async def last_report():
    with db.get_db() as conn:
        # Filtrage sur le TYPE, plus sur le texte : « message LIKE 'Rapport quotidien%' »
        # cassait dès que le message cessait d'être écrit en français. La clause LIKE est
        # conservée en second terme uniquement pour les événements ANTÉRIEURS à
        # l'introduction du type daily_summary, qui portent encore type='info'.
        row = conn.execute("""
            SELECT * FROM events
            WHERE profile = 'global'
              AND (type = 'daily_summary'
                   OR (type = 'info' AND message LIKE 'Rapport quotidien%'))
            ORDER BY timestamp DESC LIMIT 1
        """).fetchone()
    return JSONResponse(dict(row) if row else {})


def _resync_pihole_blacklists():
    """Resync les blacklists Pi-hole des deux modes ouverts, pour tous les profils.

    Appelé après génération du rapport. Synchronise TOUS les groupes et pas seulement le
    mode en cours, pour que les listes soient à jour même pour un enfant dont l'accès est
    coupé à cet instant : il les retrouvera à la plage suivante.
    """
    import domain_classifier as classifier
    m = get_monitor()
    api = m.pihole
    for pname, profile in m.config["profiles"].items():
        if profile.get("mode") == "monitoring" or not profile.get("devices"):
            continue
        for mode in (modes.HOMEWORK, modes.FREE):
            group_name = f"{pname}-{mode}"
            group_id = api.get_group_id(group_name)
            if group_id is None:
                continue
            blacklist = classifier.get_active_blacklist(mode, profile, pname)
            api._sync_blacklist(group_id, mode, blacklist)


@app.post("/api/report/generate")
async def generate_report(request: Request):
    if not _check_session(request):
        return JSONResponse({"ok": False, "error_code": "unauthenticated", "error": "Non authentifié"}, status_code=401)
    import subprocess, sys
    base = os.path.dirname(DATA_DIR)
    venv_python = os.path.join(base, ".venv", "bin", "python3")
    script = os.path.join(base, "daily_report.py")
    python = venv_python if os.path.exists(venv_python) else sys.executable
    loop = asyncio.get_event_loop()
    def _run():
        return subprocess.run(
            [python, script],
            capture_output=True, text=True, timeout=300, cwd=base
        )
    try:
        result = await loop.run_in_executor(None, _run)
        if result.returncode == 0:
            await loop.run_in_executor(None, _resync_pihole_blacklists)
            return JSONResponse({"ok": True})
        output = (result.stdout[-300:] + "\n" + result.stderr[-300:]).strip()
        ai_error = "401" in output or "User not found" in output or "invalid_api_key" in output
        if ai_error:
            global _ai_key_invalid
            _ai_key_invalid = True
        return JSONResponse({"ok": False, "error_code": "server_error",
                             "error": output, "ai_key_invalid": ai_error}, status_code=500)
    except subprocess.TimeoutExpired:
        return JSONResponse({"ok": False, "error_code": "timeout", "error": "Timeout (300s)"}, status_code=500)
    except Exception as e:
        return JSONResponse({"ok": False, "error_code": "server_error",
                             "error": str(e)}, status_code=500)


@app.get("/api/domains")
async def domains():
    from domain_classifier import get_all_domains
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, get_all_domains)
    return JSONResponse(data)


# Âge maximal du dernier cycle réussi, en intervalles du moniteur.
HEALTH_MAX_CYCLES = 3


@app.get("/api/health")
async def health():
    """200 si le boîtier fonctionne vraiment : moniteur démarré ET dernier cycle réussi
    il y a moins de trois intervalles. Lu par les scripts de mise à jour, qui restaurent
    la version précédente sinon. Sans authentification, et sans rien révéler.

    Pendant l'assistant (boîtier non configuré), il n'y a pas de moniteur : 200, le
    tableau de bord qui répond est tout ce qu'on peut attendre.
    """
    if not _is_configured():
        return JSONResponse({"ok": True, "state": "not_configured"})
    if monitor is None:
        return JSONResponse({"ok": False, "state": "no_monitor"}, status_code=503)
    dernier = getattr(monitor, "last_cycle_ok", None)
    intervalle = getattr(monitor, "interval", 60) or 60
    if dernier is None:
        return JSONResponse({"ok": False, "state": "starting"}, status_code=503)
    if time.time() - dernier > HEALTH_MAX_CYCLES * intervalle:
        return JSONResponse({"ok": False, "state": "stale"}, status_code=503)
    return JSONResponse({"ok": True, "state": "ok"})


@app.get("/api/events")
async def events(limit: int = 50):
    return JSONResponse(db.get_recent_events(limit))


@app.get("/api/decisions")
async def decisions(limit: int = 30, scope: str = "decisions"):
    """Le journal des réglages : les décisions humaines (cf. DECISION_KEYS), ou, avec
    scope=all, tout le journal hors navigation (cf. database.JOURNAL_PRIVATE_KEYS)."""
    limit = min(max(limit, 1), 200)
    if scope == "all":
        return JSONResponse(db.get_journal(limit))
    return JSONResponse(db.get_decisions(limit))


@app.post("/api/vpn-sessions/{session_id}/ack")
async def acknowledge_vpn(session_id: int):
    """Le parent a vu un tunnel terminé et va s'en occuper : il quitte la carte des
    alertes. Refusé sur un tunnel en cours, dont la cause est toujours là."""
    session = db.get_vpn_session(session_id)
    if not session:
        return JSONResponse({"ok": False, "error_code": "not_found",
                             "error": "Session inconnue"}, status_code=404)
    if not session.get("ended_at"):
        return JSONResponse({"ok": False, "error_code": "vpn_still_active",
                             "error": "Tunnel toujours en cours"}, status_code=409)
    db.acknowledge_vpn_session(session_id)
    db.log_event(session["profile"], "info", session["ip"], "Tunnel marqué comme vu",
                 message_key="event.vpn_acknowledged")
    return JSONResponse({"ok": True})


@app.get("/api/usage/{profile}")
async def usage(profile: str):
    return JSONResponse(db.get_time_spent_today(profile))


@app.get("/api/schedule")
async def schedule():
    config = _load_config()
    result = {}
    for pname, profile in config["profiles"].items():
        if profile.get("mode") == "monitoring":
            continue
        result[pname] = {
            "name": profile["name"],
            "current_slot": get_slot_at(pname, datetime.now()),
            "rules": profile.get("schedule", {})
        }
    return JSONResponse(result)


@app.get("/api/overrides")
async def overrides():
    return JSONResponse(db.get_schedule_overrides())


class OverrideCreate(BaseModel):
    profile: str
    date: str
    mode: str
    reason: str = ""


@app.post("/api/overrides")
async def create_override(body: OverrideCreate):
    if (not _PROFILE_KEY_RE.match(body.profile)
            or body.profile not in (_load_config().get("profiles") or {})):
        return JSONResponse({"ok": False, "error_code": "invalid_profile",
                             "error": "Profil inconnu"}, status_code=400)
    try:
        _date.fromisoformat(body.date)
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error_code": "invalid_date",
                             "error": "Date invalide"}, status_code=400)
    mode = modes.normalize(body.mode)
    if mode not in _VALID_OVERRIDE_MODES:
        return JSONResponse({"ok": False, "error_code": "invalid_mode", "error": "Mode invalide"}, status_code=400)
    db.set_schedule_override(body.profile, body.date, mode, body.reason)
    db.log_event(body.profile, "info", "",
                 f"Override planning {body.date} : mode {mode}"
                 + (f" — {body.reason}" if body.reason else ""),
                 message_key=("event.schedule_override_reason" if body.reason
                              else "event.schedule_override"),
                 params={"date": body.date, "mode": mode, "reason": body.reason})
    get_monitor().notify()
    return JSONResponse({"ok": True})


@app.delete("/api/overrides/{profile}/{date}")
async def delete_override(profile: str, date: str):
    if db.clear_schedule_override(profile, date):
        db.log_event(profile, "info", "", f"Dérogation du {date} retirée",
                     message_key="event.schedule_override_removed", params={"date": date})
    get_monitor().notify()
    return JSONResponse({"ok": True})


# ---- Overrides temporaires de mode ----

@app.get("/api/temp-overrides")
async def list_temp_overrides():
    return JSONResponse(_scheduler.get_all_temp_overrides())


class TempOverrideCreate(BaseModel):
    profile: str
    mode: str
    minutes: int


@app.post("/api/temp-overrides")
async def create_temp_override(body: TempOverrideCreate, request: Request):
    if not _check_session(request):
        return JSONResponse({"ok": False}, status_code=401)
    if not _PROFILE_KEY_RE.match(body.profile):
        return JSONResponse({"ok": False, "error_code": "invalid_profile", "error": "profil invalide"}, status_code=400)
    # Normalisé une fois, puis utilisé partout : valider l'ancien vocabulaire tout en
    # écrivant la valeur brute en base ferait cohabiter deux noms du même mode.
    mode = modes.normalize(body.mode)
    if mode not in _VALID_MODES:
        return JSONResponse({"ok": False, "error_code": "invalid_mode", "error": "mode invalide"}, status_code=400)
    if not (5 <= body.minutes <= 240):
        return JSONResponse({"ok": False, "error_code": "invalid_duration", "error": "durée invalide (5–240 min)"}, status_code=400)

    m = get_monitor()
    profile_cfg = m.config["profiles"].get(body.profile)
    if not profile_cfg:
        return JSONResponse({"ok": False, "error_code": "unknown_profile", "error": "profil inconnu"}, status_code=400)

    _scheduler.set_temp_override(body.profile, mode, body.minutes)
    m._apply_pihole_mode(body.profile, mode)
    db.log_event(body.profile, "info", "",
                 f"Override temporaire (GUI) : mode {mode} pendant {body.minutes} min",
                 message_key="event.temp_override_started",
                 params={"mode": mode, "min": body.minutes})
    m.notify()

    def _restore():
        try:
            # Le monitor rattrape aussi les échéances : celui qui efface annonce, l'autre
            # se tait. Sans ça, l'événement de fin apparaissait deux fois.
            if not _scheduler.clear_temp_override(body.profile):
                return
            slot = _scheduler.get_slot_at(body.profile, datetime.now())
            m._apply_pihole_mode(body.profile, slot["mode"])
            db.log_event(body.profile, "info", "",
                         f"Override temporaire terminé — retour en mode {slot['mode']}",
                         message_key="event.temp_override_ended",
                         params={"mode": slot["mode"]})
            m.notify()
        except Exception as e:
            print(f"[Dashboard] Erreur restauration override {body.profile} : {e}")

    t = _threading.Timer(body.minutes * 60, _restore)
    t.daemon = True
    t.start()
    _scheduler.register_temp_timer(body.profile, t)
    return JSONResponse({"ok": True})


@app.delete("/api/temp-overrides/{profile}")
async def cancel_temp_override(profile: str, request: Request):
    if not _check_session(request):
        return JSONResponse({"ok": False}, status_code=401)
    if not _scheduler.clear_temp_override(profile):
        return JSONResponse({"ok": True})      # déjà expirée : rien à annoncer
    m = get_monitor()
    slot = _scheduler.get_slot_at(profile, datetime.now())
    m._apply_pihole_mode(profile, slot["mode"])
    db.log_event(profile, "info", "",
                 f"Override temporaire annulé — retour en mode {slot['mode']}",
                 message_key="event.temp_override_cancelled",
                 params={"mode": slot["mode"]})
    m.notify()
    return JSONResponse({"ok": True})


@app.get("/api/exceptions")
async def list_exceptions(request: Request):
    """TOUT ce qui est temporairement en vigueur, et de quoi l'annuler.

    POURQUOI CET ÉCRAN EXISTE. Le boîtier savait accorder cinq sortes d'exceptions, et
    n'en montrait que deux. Une rallonge de vingt minutes, un domaine ouvert deux heures
    par l'assistant, un appareil passé en mode adulte : accordés, appliqués, expirés, et
    invisibles entre-temps. Le parent ne pouvait donc ni savoir ce qui était en cours, ni
    refermer avant l'heure. Un écart qu'on ne voit pas est un écart qu'on ne peut pas
    reprendre.

    `kind` dit de quelle sorte il s'agit, et c'est cette clé que l'interface emploie pour
    savoir quoi appeler à l'annulation. Les libellés sont fabriqués côté client : le
    serveur rend des faits, pas des phrases.
    """
    if not _check_session(request):
        return JSONResponse({"ok": False}, status_code=401)

    config = _load_config()
    profiles = config.get("profiles") or {}
    aujourdhui = datetime.now().date().isoformat()
    items: list[dict] = []

    def _minutes(iso: str) -> int | None:
        try:
            return max(0, int((datetime.fromisoformat(iso) - datetime.now()).total_seconds() / 60))
        except (TypeError, ValueError):
            return None

    # 1. Mode temporaire accordé à un enfant (« 30 min de temps libre »).
    for row in _scheduler.get_all_temp_overrides():
        items.append({
            "kind": "temp_mode", "profile": row["profile"],
            "name": (profiles.get(row["profile"]) or {}).get("name", row["profile"]),
            "mode": row["mode"], "until": row["expires_at"],
            "minutes_left": row["minutes_left"],
        })

    # 2. Dérogation de journée, aujourd'hui ou à venir. Les jours passés sont de
    #    l'historique, pas des exceptions en cours.
    for row in db.get_schedule_overrides(since=aujourdhui):
        items.append({
            "kind": "day_mode", "profile": row["profile"],
            "name": (profiles.get(row["profile"]) or {}).get("name", row["profile"]),
            "mode": row["mode"], "date": row["date"], "reason": row.get("reason", ""),
        })

    # 3. Rallonge de plage pas encore échue, y compris celle qui déborde après minuit.
    for row in _scheduler.active_extensions():
        items.append({
            "kind": "slot_extension", "profile": row["profile"],
            "name": (profiles.get(row["profile"]) or {}).get("name", row["profile"]),
            "minutes": row["minutes"], "date": row["day"], "until": row["ends_at"],
        })

    # 4. Domaine ouvert temporairement, en général par l'assistant.
    for row in db.get_temp_domain_unblocks():
        items.append({
            "kind": "domain", "domain": row["domain"], "profile": row.get("profile", ""),
            "name": (profiles.get(row.get("profile") or "") or {}).get("name", ""),
            "until": row["expires_at"], "minutes_left": _minutes(row["expires_at"]),
        })

    # 5. Appareil passé en mode adulte : le filtrage ne s'y applique plus du tout. C'est
    #    l'exception la plus large du produit, et elle n'apparaissait que sur la carte du
    #    profil concerné.
    for pname, profile in profiles.items():
        for device in profile.get("devices") or []:
            ip = device.get("ip", "")
            ov = db.get_device_override(ip) if ip else None
            if not ov:
                continue
            items.append({
                "kind": "device", "profile": pname,
                "name": profile.get("name", pname), "ip": ip,
                "until": ov.get("expires_at", ""),
                "minutes_left": _minutes(ov.get("expires_at", "")),
                "taken_by": ov.get("taken_by", ""),
            })

    return JSONResponse({"ok": True, "items": items})


@app.delete("/api/slot-extension/{profile}")
async def cancel_slot_extension(profile: str, request: Request):
    """Annule la rallonge du jour. Il n'existait aucun chemin pour la reprendre."""
    if not _check_session(request):
        return JSONResponse({"ok": False}, status_code=401)
    if not _PROFILE_KEY_RE.match(profile):
        return JSONResponse({"ok": False, "error_code": "invalid_profile"}, status_code=400)
    if not db.clear_slot_extension(profile):
        return JSONResponse({"ok": True})      # déjà remise à zéro : rien à annoncer
    db.log_event(profile, "info", "", "Rallonge de plage annulée",
                 message_key="event.extension_cancelled", params={})
    get_monitor().notify()
    return JSONResponse({"ok": True})


@app.delete("/api/temp-domain/{domain:path}")
async def cancel_temp_domain(domain: str, request: Request, profile: str | None = None):
    """Referme un domaine ouvert temporairement, avant son échéance.

    `profile` désigne l'enfant ; absent, le domaine est refermé pour tous les enfants.
    La resynchronisation des blacklists est ce qui rend l'annulation effective : retirer
    la ligne sans elle laisserait le domaine autorisé jusqu'au prochain cycle.
    """
    if not _check_session(request):
        return JSONResponse({"ok": False}, status_code=401)
    domain = (domain or "").strip().lower()
    if not _DOMAIN_RE.match(domain):
        return JSONResponse({"ok": False, "error_code": "invalid_domain"}, status_code=400)
    if profile and not _PROFILE_KEY_RE.match(profile):
        return JSONResponse({"ok": False, "error_code": "invalid_profile"}, status_code=400)
    if not db.clear_temp_domain_unblock(domain, profile):
        return JSONResponse({"ok": True})
    db.log_event(profile or "", "info", domain, f"Autorisation temporaire retirée — {domain}",
                 message_key="event.domain_unblock_cancelled", params={"domain": domain})
    try:
        from claude_agent import _sync_pihole_blacklists
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _sync_pihole_blacklists, _load_config())
    except Exception as e:
        print(f"[Dashboard] Erreur sync Pi-hole après retrait de {domain} : {e}")
    get_monitor().notify()
    return JSONResponse({"ok": True})


class DomainUpdate(BaseModel):
    category: str | None = None
    # Blocage par domaine, pour toute la maison. None = inchangé. True = bloqué même si sa
    # catégorie est autorisée. False = RETIRE l'exception : la grille décide de nouveau.
    # Une AUTORISATION se pose par enfant (cf. /api/profiles/{key}/domain-rules).
    blocked_homework: bool | None = None
    blocked_free: bool | None = None
    # Enfant visé. Vide = tous.
    profile: str = ""


class DomainRuleBody(BaseModel):
    domain: str = Field(..., max_length=300)
    modes: list[str] = Field(default_factory=list)
    allow: bool


def _normaliser_domaine(saisie: str) -> str:
    """Ce que le parent colle (« https://www.poki.com/fr/jeux ») vers le domaine racine
    que le catalogue et les listes de blocage utilisent (« poki.com »)."""
    d = (saisie or "").strip().lower()
    d = re.sub(r"^[a-z]+://", "", d).split("/")[0].split("?")[0].split(":")[0].strip(".")
    return root_domain(d) if _DOMAIN_RE.match(d) else d


async def _appliquer_regles_domaines():
    """Les listes de blocage suivent une exception tout de suite, pas au prochain cycle."""
    from claude_agent import _sync_pihole_blacklists
    try:
        await asyncio.get_running_loop().run_in_executor(
            None, _sync_pihole_blacklists, _load_config())
    except Exception as e:
        print(f"[Dashboard] Erreur sync Pi-hole : {e}")
    try:
        get_monitor().notify()
    except Exception:
        pass


@app.get("/api/profiles/{key}/domain-rules")
async def list_profile_domain_rules(key: str):
    if not _PROFILE_KEY_RE.match(key) or key not in _load_config().get("profiles", {}):
        return JSONResponse({"ok": False, "error_code": "unknown_profile",
                             "error": "Profil inconnu"}, status_code=404)
    return JSONResponse(db.list_domain_rules(key))


@app.post("/api/profiles/{key}/domain-rules")
async def set_profile_domain_rule(key: str, body: DomainRuleBody):
    """Exception de domaine pour UN enfant, dans les modes choisis : autorisé même si sa
    catégorie est bloquée par sa grille (sauf catégories bloquées à tout âge), ou bloqué
    même si elle est autorisée."""
    config = _load_config()
    if not _PROFILE_KEY_RE.match(key) or key not in config.get("profiles", {}):
        return JSONResponse({"ok": False, "error_code": "unknown_profile",
                             "error": "Profil inconnu"}, status_code=404)
    domaine = _normaliser_domaine(body.domain)
    if not _DOMAIN_RE.match(domaine):
        return JSONResponse({"ok": False, "error_code": "invalid_domain",
                             "error": "Domaine invalide"}, status_code=400)
    choisis = set(body.modes)
    if not choisis or not choisis <= {modes.HOMEWORK, modes.FREE}:
        return JSONResponse({"ok": False, "error_code": "invalid_mode",
                             "error": "Mode invalide"}, status_code=400)
    # Une autorisation sur une catégorie bloquée à tout âge serait enregistrée sans effet
    # (cf. get_active_blacklist) : on le dit plutôt que de laisser croire qu'elle s'applique.
    if body.allow:
        with db.get_db() as conn:
            ligne = conn.execute("SELECT category FROM domains WHERE domain=?",
                                 (domaine,)).fetchone()
        if ligne and ligne["category"] in access_grid.ALWAYS_BLOCKED:
            return JSONResponse({"ok": False, "error_code": "domain_always_blocked",
                                 "error": "Catégorie bloquée à tout âge"}, status_code=400)
    from domain_classifier import update_domain as _update
    _update(domaine, by="parent")             # le domaine entre au catalogue s'il n'y est pas
    for mode in sorted(choisis):
        db.set_domain_rule(key, domaine, mode, allow=body.allow)
    db.log_event(key, "info", domaine, f"Règle du domaine {domaine} modifiée",
                 message_key="event.domain_rule_changed", params={"domain": domaine})
    await _appliquer_regles_domaines()
    return JSONResponse({"ok": True, "domain": domaine})


@app.delete("/api/profiles/{key}/domain-rules")
async def clear_profile_domain_rule(key: str, domain: str, mode: str = ""):
    if not _PROFILE_KEY_RE.match(key) or key not in _load_config().get("profiles", {}):
        return JSONResponse({"ok": False, "error_code": "unknown_profile",
                             "error": "Profil inconnu"}, status_code=404)
    if mode and mode not in (modes.HOMEWORK, modes.FREE):
        return JSONResponse({"ok": False, "error_code": "invalid_mode",
                             "error": "Mode invalide"}, status_code=400)
    db.clear_domain_rule(key, domain, mode)
    db.log_event(key, "info", domain, f"Règle du domaine {domain} retirée",
                 message_key="event.domain_rule_changed", params={"domain": domain})
    await _appliquer_regles_domaines()
    return JSONResponse({"ok": True})


@app.patch("/api/domains/{domain:path}")
async def update_domain_route(domain: str, body: DomainUpdate):
    if not _DOMAIN_RE.match(domain.lower()):
        return JSONResponse({"ok": False, "error_code": "invalid_domain", "error": "Domaine invalide"}, status_code=400)
    if body.profile and not _PROFILE_KEY_RE.match(body.profile):
        return JSONResponse({"ok": False, "error_code": "invalid_profile_key",
                             "error": "Clé de profil invalide"}, status_code=400)
    from domain_classifier import update_domain as _update
    from claude_agent import _sync_pihole_blacklists
    _update(domain, category=body.category, by="parent")
    # Une exception se pose, se retourne ou se retire. La retirer rend le domaine à la
    # décision de la grille, ce qui est l'état par défaut et doit rester atteignable.
    for champ, mode in ((body.blocked_homework, modes.HOMEWORK),
                        (body.blocked_free, modes.FREE)):
        if champ is None:
            continue
        if champ:
            db.set_domain_rule(body.profile, domain, mode, allow=False)
        else:
            db.clear_domain_rule(body.profile, domain, mode)
    db.log_event(body.profile, "info", domain, f"Règle du domaine {domain} modifiée",
                 message_key="event.domain_rule_changed", params={"domain": domain})
    try:
        # Synchronisation Pi-hole : plusieurs appels HTTP synchrones → hors boucle.
        config = _load_config()
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _sync_pihole_blacklists, config)
    except Exception as e:
        print(f"[Dashboard] Erreur sync Pi-hole : {e}")
    get_monitor().notify()
    return JSONResponse({"ok": True})


class ChatMessage(BaseModel):
    message: str = Field(..., min_length=1, max_length=1000)


class AiKeyUpdate(BaseModel):
    key: str = ""


@app.post("/api/ai/key")
async def update_ai_key(body: AiKeyUpdate):
    global _ai_key_invalid
    key = body.key.strip()
    config = _load_config()
    openrouter = config.setdefault("openrouter", {})
    openrouter["api_key"] = key
    # Inscrire le modèle par défaut s'il manque : la configuration reste ainsi explicite
    # et modifiable à la main. setdefault — on n'écrase jamais un choix du parent.
    from claude_agent import DEFAULT_MODEL
    openrouter.setdefault("model", DEFAULT_MODEL)
    _save_config(config)
    _ai_key_invalid = False
    return JSONResponse({"ok": True})


@app.post("/api/chat")
async def chat(body: ChatMessage):
    global _ai_key_invalid
    try:
        from openai import AuthenticationError as _OAIAuth
        # claude_agent.chat() est SYNCHRONE et peut durer 10 à 30 s : l'exécuter dans la
        # boucle d'événements gelait tout le dashboard — flux SSE compris — pour TOUS les
        # clients pendant ce temps. Même motif que /api/status et /api/domains.
        loop = asyncio.get_event_loop()
        reply = await loop.run_in_executor(None, claude_agent.chat, body.message)
        return JSONResponse({"reply": reply})
    except _OAIAuth:
        _ai_key_invalid = True
        return JSONResponse({"reply": "Clé API invalide ou révoquée.", "ai_available": False})


@app.post("/api/chat/reset")
async def chat_reset(request: Request):
    if not _check_session(request):
        return JSONResponse({"ok": False}, status_code=401)
    claude_agent.reset_chat_history()
    return JSONResponse({"ok": True})


# ------------------------------------------------------------------ #
#  SSE                                                                #
# ------------------------------------------------------------------ #

@app.get("/api/stream")
async def stream(request: Request):
    jeton = request.cookies.get("fw_session")

    async def event_generator():
        # Baseline = ID le plus récent au moment de la connexion.
        # Seuls les événements postérieurs à la connexion seront poussés.
        # Si la DB était vide, baseline = 0 → tout nouvel événement sera poussé.
        init = db.get_recent_events(limit=1)
        last_event_id = init[0]["id"] if init else 0

        while True:
            if await request.is_disconnected():
                break
            # Le flux reste ouvert des heures : la session est revérifiée à chaque envoi.
            # Expirée, la page est renvoyée vers la connexion au lieu de continuer à
            # afficher l'état des enfants.
            if not _session_alive(jeton):
                yield "event: session_expired\ndata: {}\n\n"
                break
            try:
                status_data = (await status()).body
                yield f"event: status\ndata: {status_data.decode()}\n\n"

                events_list = db.get_recent_events(limit=50)
                if events_list:
                    newest_id = events_list[0]["id"]
                    if newest_id > last_event_id:
                        # Le journal à l'écran montre les décisions, ou tout le journal
                        # hors navigation : la page filtre selon son réglage, grâce à
                        # `decision`. La navigation ne part jamais dans le flux.
                        new = [{**e, "decision": e.get("message_key") in db.DECISION_KEYS}
                               for e in events_list if e["id"] > last_event_id
                               and db.is_journal_event(e)]
                        if new:
                            yield f"event: new_events\ndata: {json.dumps(new)}\n\n"
                        last_event_id = newest_id

            except Exception as e:
                yield f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n"

            await asyncio.sleep(10)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


# ------------------------------------------------------------------ #
#  Devices                                                            #
# ------------------------------------------------------------------ #

@app.get("/devices")
async def devices_page():
    """Ancienne page Réseau, devenue un onglet : un favori ou un lien noté ailleurs mène
    à l'onglet au lieu de tomber dans le vide."""
    return RedirectResponse(url="/?tab=reseau", status_code=307)


def _format_last_seen(raw) -> str | None:
    """Normalise un timestamp Pi-hole (epoch int ou ISO string) en ISO string."""
    if not raw:
        return None
    try:
        if isinstance(raw, (int, float)):
            return datetime.fromtimestamp(raw).isoformat()
        return str(raw)
    except Exception:
        return None


# Synchrone : FastAPI l'exécute dans son pool de threads. En « async def », ses appels
# Pi-hole bloquants (jusqu'à 10 s chacun) figeaient la boucle, flux SSE compris.
@app.get("/api/devices")
def list_devices():
    m = get_monitor()
    config = m.config

    # Pi-hole clients : appareils qui font des requêtes DNS via Pi-hole
    pihole_clients: dict[str, dict] = {}
    for c in m.pihole.get_clients():
        ip = c.get("client") or c.get("ip", "")
        if ip:
            pihole_clients[ip] = c

    # Table réseau Pi-hole FTL — c'est l'onglet « Network » (appareils vus par ARP/DHCP).
    # Source PRINCIPALE : c'est là que FTL liste les appareils, y compris le réseau enfants.
    network_devices: dict[str, dict] = {}
    try:
        for d in m.pihole.get_network_devices():
            if d.get("ip"):
                network_devices[d["ip"]] = d
    except Exception as e:
        print(f"[devices] get_network_devices a échoué : {e}")

    # Inventaire ARP RÉEL, produit par le runner root (handler scan_arp, mode dns_only).
    # C'est une source INDÉPENDANTE de Pi-hole : sans elle, la section « bypass » —
    # définie comme « vu sur le réseau mais inconnu de FTL » — était vide par
    # construction, puisque ARPScanner.scan() interroge… FTL lui-même.
    arp_devices: dict[str, dict] = {}
    try:
        with open(os.path.join(DATA_DIR, "arp_scan.json")) as f:
            for d in (json.load(f).get("devices") or []):
                # Même filtre que sur les sources Pi-hole : le parent ne doit jamais
                # voir une adresse qui n'est l'appareil de personne.
                if d.get("ip") and is_real_device_ip(d["ip"]):
                    arp_devices[d["ip"]] = d
    except (OSError, ValueError):
        pass   # pas encore de scan, ou mode gateway : la section bypass reste vide

    # Typage (téléphone / console / imprimante…) déduit du fabricant de la MAC : les
    # appareils vus SEULEMENT en ARP n'ont aucune donnée FTL, c'est la seule information
    # exploitable pour les présenter au parent.
    from arp_scanner import _guess_device_type
    for ip, d in arp_devices.items():
        d.setdefault("device_type", _guess_device_type(d.get("vendor", ""), d.get("mac", "")))

    # Aucune écriture dans Pi-hole ici : la page inscrivait chaque appareil inconnu comme
    # client à chaque chargement. L'inscription se fait à l'assignation à un profil
    # (assign_client_to_group crée le client s'il manque), seul moment où elle sert.

    # Union des trois sources
    all_ips = set(pihole_clients) | set(network_devices) | set(arp_devices)

    # Carte inverse ip → profile_key depuis config
    assigned: dict[str, str] = {}
    assigned_mac: dict[str, str] = {}
    # Appareils rattachés à un profil D'ENFANT. Seuls ceux-là peuvent contourner quelque
    # chose : une imprimante, une TV ou l'appareil d'un invité qui n'utilisent pas le
    # résolveur du boîtier ne trichent avec aucune règle, puisqu'aucune ne les vise.
    # Le monitor applique déjà cette distinction avant d'alerter (monitor._check_dns_bypass) ;
    # la page l'ignorait et étiquetait « suspect » tout ce qu'elle ne reconnaissait pas.
    watched_ips: set[str] = set()
    # Noms annoncés au DHCP, remontés avec l'identité des stations. En posture passerelle
    # c'est la SEULE source pour les appareils des enfants : dnsmasq sert le DHCP du réseau
    # enfants avec port=0, donc Pi-hole FTL ne voit passer aucun de ces noms. Sans cette
    # correspondance, la page Appareils retombait sur le fabricant, ou sur rien.
    identity_hostnames: dict[str, str] = {}
    for key, profile in config.get("profiles", {}).items():
        for device in profile.get("devices", []):
            assigned[device["ip"]] = key
            assigned_mac[device["ip"]] = device.get("mac", "")
            if device.get("hostname"):
                identity_hostnames[device["ip"]] = device["hostname"]
            if profile.get("mode") != "monitoring":
                watched_ips.add(device["ip"])
    device_names: dict[str, str] = config.get("device_names", {})
    # Appareils déjà vus, par adresse matérielle : un appareil d'enfant déconnecté n'est
    # plus dans la liste (instantanée) de son profil, mais il reste le sien.
    vus_par_mac = db.seen_profiles_by_mac()

    pihole_list = []
    bypass_list = []

    for ip in sorted(all_ips):
        ph  = pihole_clients.get(ip, {})
        net = network_devices.get(ip, {})
        arp = arp_devices.get(ip, {})
        # « Sous contrôle » = connu de Pi-hole (client OU table réseau FTL). En mode
        # gateway, tout le DNS enfants est forcé vers Pi-hole. « Bypass » = vu seulement
        # en ARP local, inconnu de FTL.
        known_ftl = (ip in pihole_clients) or (ip in network_devices)

        # Le nom annoncé au DHCP passe AVANT le repli sur le fabricant : « iPad » est un
        # repère, « Apple, Inc. » n'en est pas un quand trois appareils de la maison
        # portent la même puce.
        hostname = (ph.get("name") or ph.get("hostname") or
                    net.get("hostname") or arp.get("hostname") or
                    identity_hostnames.get(ip) or
                    net.get("vendor") or arp.get("vendor") or "")
        last_seen = _format_last_seen(
            ph.get("last_query") or ph.get("last_seen") or ph.get("lastQuery")
            or net.get("last_seen")
        )
        mac = arp.get("mac") or net.get("mac") or assigned_mac.get(ip, "")

        entry = {
            "ip":              ip,
            "mac":             mac,
            "hostname":        hostname,
            "custom_name":     device_names.get(ip, ""),
            "via_pihole":      known_ftl,
            "last_seen":       last_seen,
            "assigned_profile": assigned.get(ip),
            # Réseau enfants : rattaché par sa clé, pas par cette page. `connected` dit
            # s'il est là maintenant, `known_profile` à quel enfant il appartient, même
            # déconnecté (sinon il s'affichait comme l'appareil du parent).
            "kids_network":    station_identity.is_kids_ip(ip),
            "connected":       ip in assigned,
            "known_profile":   assigned.get(ip) or (vus_par_mac.get(mac)
                                                    if station_identity.is_kids_ip(ip) else None),
            "device_type":     arp.get("device_type", "unknown"),
            # « Suspect » = appareil d'enfant hors filtrage. Les autres sont simplement
            # constatés, sans accusation.
            "suspect":         ip in watched_ips,
        }

        if known_ftl:
            pihole_list.append(entry)
        else:
            bypass_list.append(entry)

    # Les appareils d'enfants d'abord : c'est la seule ligne qui demande une action.
    bypass_list.sort(key=lambda d: (not d["suspect"], d["ip"]))

    profiles_list = [
        {"key": k, "name": v["name"]}
        for k, v in config.get("profiles", {}).items()
    ]
    # Une liste vide peut vouloir dire « aucun appareil » OU « Pi-hole ne répond pas »
    # (mot de passe d'API désynchronisé, FTL arrêté). Ne jamais confondre les deux :
    # on remonte l'erreur pour que la page le dise au lieu de rester blanche.
    pihole_error = ""
    if not pihole_clients and not network_devices:
        pihole_error = getattr(m.pihole, "last_error", "")
    return JSONResponse({
        "pihole":   pihole_list,
        "bypass":   bypass_list,
        "profiles": profiles_list,
        "pihole_error": pihole_error,
        # Le bouton de recherche active n'a de sens qu'en dns_only (cf. /api/devices/scan).
        "enforcement": (config.get("network") or {}).get("enforcement", "dns_only"),
    })


_ARP_SCAN_PATH = os.path.join(DATA_DIR, "arp_scan.json")


@app.post("/api/devices/scan")
async def devices_scan():
    """Recherche ACTIVE d'appareils sur le réseau (arp-scan), à la demande du parent.

    Le bouton « Actualiser » relit seulement ce que Pi-hole sait déjà : un appareil qui
    vient d'être branché et n'a encore résolu aucun nom n'y figure pas, et le parent n'a
    aucun moyen de le faire apparaître — le monitor ne relance un inventaire que toutes
    les dix minutes environ. C'est ce que cette route donne : provoquer l'inventaire au
    lieu de l'attendre.

    Sans objet en passerelle : le boîtier route tout le trafic des enfants et voit leurs
    appareils par son propre DHCP. Le scan n'y apprendrait rien, la route le dit plutôt
    que de lancer une opération privilégiée pour rien.
    """
    if (_load_config().get("network") or {}).get("enforcement") == "gateway":
        return JSONResponse({"ok": False, "error_code": "scan_not_applicable",
                             "error": "mode passerelle — scan sans objet"}, status_code=400)
    # L'horodatage du résultat PRÉCÉDENT, renvoyé au client : c'est ce qui lui permet de
    # distinguer « scan terminé » d'un ancien fichier resté sur disque. On ne supprime
    # pas le fichier, contrairement à l'assistant : la page Appareils s'en sert pour
    # afficher la section « hors Pi-hole » pendant toute la recherche.
    since = (_read_json(_ARP_SCAN_PATH).get("updated_at") or "")
    try:
        _queue("scan_arp", {})
    except OSError as e:
        return _queue_error("scan_arp", e)
    return JSONResponse({"ok": True, "since": since})


@app.get("/api/devices/scan")
async def devices_scan_result():
    """État du dernier inventaire ARP : le client compare `updated_at` à son `since`."""
    scan = _read_json(_ARP_SCAN_PATH)
    return JSONResponse({
        "updated_at": scan.get("updated_at", ""),
        "error":      scan.get("error", ""),
        "skipped":    scan.get("skipped", ""),
        "count":      len(scan.get("devices") or []),
    })


class DeviceAssign(BaseModel):
    ip: str
    mac: str = ""
    profile_key: str | None = None  # None = désassigner


@app.post("/api/devices/assign")
async def assign_device(body: DeviceAssign):
    try:
        ipaddress.ip_address(body.ip)
    except ValueError:
        return JSONResponse({"ok": False, "error_code": "invalid_ip", "error": "Adresse IP invalide"}, status_code=400)
    config = _load_config()

    # Retirer l'IP de tous les profils existants
    for profile in config.get("profiles", {}).values():
        profile["devices"] = [
            d for d in profile.get("devices", []) if d["ip"] != body.ip
        ]

    # Assigner au nouveau profil si fourni
    if body.profile_key and body.profile_key in config.get("profiles", {}):
        config["profiles"][body.profile_key].setdefault("devices", []).append(
            {"ip": body.ip, "mac": body.mac}
        )

    _save_config(config)
    m = get_monitor()

    # Rattaché à un profil : l'appareil sort du suivi « nouvel appareil » (son accès est
    # désormais régi par le planning du profil, plus par un délai de grâce).

    # Appliquer immédiatement dans Pi-hole, hors de la boucle (appels bloquants). La
    # configuration est déjà enregistrée : rien ne s'intercale entre lecture et écriture.
    def _appliquer():
        if body.profile_key and body.profile_key in config.get("profiles", {}):
            try:
                from scheduler import get_slot_at
                slot = get_slot_at(body.profile_key, datetime.now())
                apply_device_access(
                    m.pihole, [body.ip],
                    mode=slot["mode"], group=f"{body.profile_key}-{slot['mode']}",
                    profile=body.profile_key, reason="assignation_profil",
                )
            except Exception as e:
                print(f"[Dashboard] Avertissement assign Pi-hole : {e}")
        elif not body.profile_key:
            # Désassignation : basculer vers le groupe par défaut Pi-hole (groupe 0).
            # Appareil non géré → accès autorisé (un mode ouvert donne ACCEPT en gateway).
            try:
                apply_device_access(
                    m.pihole, [body.ip],
                    mode=modes.FREE, group="Default", reason="désassignation",
                )
            except Exception:
                pass
    await asyncio.get_running_loop().run_in_executor(None, _appliquer)

    m.notify()
    return JSONResponse({"ok": True})


class DeviceRename(BaseModel):
    ip: str
    name: str = ""


@app.get("/api/devices/pending")
def pending_devices():
    """Appareils connus de FTL, NI assignés à un profil NI ignorés → à traiter.
    Alimente la notification « nouveaux appareils » du dashboard."""
    m = get_monitor()
    config = m.config
    # Adresses du Pi lui-même à ne jamais proposer.
    skip = {"127.0.0.1", "192.168.50.1", _uplink_ip()}
    known: dict[str, dict] = {}
    try:
        for d in m.pihole.get_network_devices():
            ip = d.get("ip")
            if ip and ip not in skip:
                known[ip] = {"mac": d.get("mac", ""), "hostname": d.get("hostname") or d.get("vendor", "")}
    except Exception as e:
        print(f"[devices] pending/get_network_devices : {e}")
    try:
        for c in m.pihole.get_clients():
            ip = c.get("client") or c.get("ip", "")
            if ip and ip not in skip and ip not in known:
                known[ip] = {"mac": "", "hostname": c.get("name", "")}
    except Exception:
        pass
    names = config.get("device_names", {})
    # Filtre « ni rattaché ni ignoré » : règle unique, partagée avec le monitor.
    profiles = [{"key": k, "name": v["name"]}
                for k, v in config.get("profiles", {}).items() if k != "monitoring"]
    # EN PASSERELLE, IL N'Y A PLUS D'APPAREIL À TRAITER. L'identité vient de la clé Wi-Fi
    # utilisée à l'association : un appareil qui en connaît une est déjà rattaché à son
    # profil, un appareil qui n'en connaît aucune ne rejoint pas le réseau. Le
    # rattachement manuel par adresse, et le délai de grâce qui l'accompagnait, n'ont
    # plus d'objet ici. En DNS seul, rien ne change : le boîtier ne maîtrise pas
    # l'association, donc le parent désigne encore les appareils lui-même.
    if (config.get("network") or {}).get("enforcement") == "gateway":
        return JSONResponse({"devices": [], "profiles": profiles})
    pending = _unassigned_devices(
        [{"ip": ip, "mac": info["mac"], "hostname": names.get(ip) or info["hostname"] or ""}
         for ip, info in sorted(known.items())],
        config,
    )
    return JSONResponse({"devices": pending, "profiles": profiles})


class DeviceIgnore(BaseModel):
    ip: str

@app.post("/api/devices/ignore")
async def ignore_device(body: DeviceIgnore):
    """« Aucun » : l'appareil est écarté de la notification (sans l'assigner)."""
    try:
        ipaddress.ip_address(body.ip)
    except ValueError:
        return JSONResponse({"ok": False, "error_code": "invalid_ip", "error": "Adresse IP invalide"}, status_code=400)
    config = _load_config()
    ign = config.setdefault("ignored_devices", [])
    if body.ip not in ign:
        ign.append(body.ip)
    _save_config(config)
    return JSONResponse({"ok": True})


@app.post("/api/devices/name")
async def rename_device(body: DeviceRename):
    try:
        ipaddress.ip_address(body.ip)
    except ValueError:
        return JSONResponse({"ok": False, "error_code": "invalid_ip", "error": "Adresse IP invalide"}, status_code=400)

    name = body.name.strip()
    config = _load_config()

    if name:
        config.setdefault("device_names", {})[body.ip] = name
    else:
        config.get("device_names", {}).pop(body.ip, None)

    _save_config(config)

    m = get_monitor()
    try:
        await asyncio.get_running_loop().run_in_executor(
            None, m.pihole.set_client_comment, body.ip, name or f"Protectado — {body.ip}")
    except Exception as e:
        print(f"[Dashboard] Avertissement rename Pi-hole : {e}")

    return JSONResponse({"ok": True})


# ------------------------------------------------------------------ #
#  Profiles CRUD                                                      #
# ------------------------------------------------------------------ #

@app.get("/api/profiles")
async def list_profiles():
    config = _config_with_identity()
    profiles = config.get("profiles", {})
    maintenant = datetime.now()
    # Exposer le niveau EFFECTIF : un profil créé avant cette version n'a pas de champ
    # `privacy_level`, et l'interface afficherait « détaillé » pour un adolescent de 17
    # ans alors que le moteur applique déjà le niveau déduit de son âge.
    out = {}
    for key, p in profiles.items():
        p = dict(p or {})
        p["privacy_level"] = privacy.level_of(p)
        # L'année est la donnée stockée, l'âge n'est qu'une lecture : on l'ajoute pour que
        # l'interface n'ait pas à refaire le calcul, ni à se tromper de convention.
        p["age"] = privacy.age_of(p)
        # Grille d'accès : ce que chaque mode autorise POUR CET ENFANT, avec l'origine de
        # chaque ligne (défaut de sa tranche, ou correction du parent). Le profil de
        # supervision n'en a pas : il ne filtre rien.
        if p.get("mode") != "monitoring":
            p["access_grid"] = access_grid.describe(p)
            # Appareils déjà vus : distinguent « jamais connecté » de « pas connecté en ce
            # moment » quand le profil n'a aucun appareil présent.
            p["seen_devices"] = [dict(v, seen=_seen_marker(v["last_seen"], maintenant))
                                 for v in db.get_seen_devices(key)]
        out[key] = p
    return JSONResponse(out)


def _seen_marker(iso: str, now: datetime) -> dict:
    """Dernière visite d'un appareil, sous forme de repères (comme _reopening, mais
    vers le passé) : `day_offset` vaut 0 aujourd'hui, 1 hier. La phrase appartient à
    l'interface, qui connaît la langue du parent."""
    try:
        quand = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return {}
    return {"time": quand.strftime("%H:%M"), "date": quand.date().isoformat(),
            "day_offset": (now.date() - quand.date()).days,
            "weekday": _DAY_KEYS[quand.weekday()]}


@app.get("/api/network/info")
async def network_info():
    """Infos réseau non sensibles, pour adapter les explications affichées au parent :
    en gateway l'enfant doit rejoindre le Wi-Fi du boîtier (dont on donne le nom), en
    dns_only il reste sur le Wi-Fi de la box. La clé Wi-Fi n'est JAMAIS exposée ici."""
    config = _load_config()
    net = config.get("network") or {}
    enforcement = net.get("enforcement", "dns_only")
    return JSONResponse({
        "enforcement": enforcement if enforcement in ("dns_only", "gateway") else "dns_only",
        "kids_ssid": (net.get("kids") or {}).get("ssid", ""),
        # Sans profil enfant, il n'existe aucune clé, donc hostapd n'est pas démarré et
        # le réseau n'est pas diffusé du tout. L'interface doit pouvoir le dire au lieu
        # de laisser le parent chercher un réseau qui n'est pas là.
        "kids_wifi_ready": wifi_keys.count(config) > 0,
        "kids_keys": wifi_keys.count(config),
        # Adresse de l'interface Pi-hole. Pas un secret, contrairement à son mot de passe
        # (cf. /api/pihole/credentials) : elle sert à afficher le lien sans rien révéler.
        "pihole_host": ((config.get("pihole") or {}).get("host") or "").rstrip("/"),
        "vpn_alerts": vpn_alerts(config),
    })


class VpnAlertsUpdate(BaseModel):
    vpn_alerts: str


@app.post("/api/network/vpn-alerts")
async def set_vpn_alerts(body: VpnAlertsUpdate):
    """Où le parent voit un tunnel probable : au journal seulement, ou aussi dans
    « À regarder » (cf. monitor.VPN_ALERTS_*)."""
    if body.vpn_alerts not in VPN_ALERTS_VALUES:
        return JSONResponse({"ok": False, "error_code": "invalid_setting",
                             "error": "valeur invalide"}, status_code=400)
    config = _load_config()
    config.setdefault("network", {})["vpn_alerts"] = body.vpn_alerts
    _save_config(config)
    return JSONResponse({"ok": True, "vpn_alerts": body.vpn_alerts})


@app.get("/api/pihole/credentials")
async def pihole_credentials(request: Request):
    """Adresse et mot de passe de l'interface Pi-hole, pour le parent qui veut l'ouvrir.

    POURQUOI CET ÉCRAN. Ce mot de passe est généré à l'installation et écrit dans
    config.json ; personne ne le choisit et personne ne le retient. Le parent qui veut
    voir les journaux de Pi-hole lui-même n'avait donc qu'une sortie : une session SSH
    pour lire un fichier de configuration.

    POURQUOI UN ENDPOINT À PART, et non un champ de /api/network/info : celui-là est
    interrogé à chaque affichage et se dit explicitement non sensible. Un secret n'a pas
    à traverser le réseau à chaque rafraîchissement d'écran pour rester caché derrière un
    bouton. Ici il ne part que sur un clic. L'adresse, elle, n'est pas un secret et vit
    dans /api/network/info, ce qui permet d'afficher le lien sans rien révéler.

    Le secret reste EN CLAIR dans config.json, ce qui est un risque connu et accepté du
    produit (le scénario qui compte est la carte SD sortie du boîtier). Cet écran ne
    l'aggrave pas : il est derrière la session parent, comme la sauvegarde qui contient
    déjà les mêmes secrets.
    """
    if not _check_session(request):
        return JSONResponse({"ok": False}, status_code=401)
    return JSONResponse({
        "ok": True,
        "password": (_load_config().get("pihole") or {}).get("password") or "",
    })


@app.get("/api/network/genkey")
async def network_genkey():
    """Propose une clé lisible, pour la clé Wi-Fi d'un profil enfant.

    L'endpoint ne fait que PROPOSER : la clé n'est enregistrée que si le parent
    enregistre le profil avec.
    """
    return JSONResponse({"key": _gen_passphrase()})


class ProfileUpdate(BaseModel):
    name: str
    # Année de naissance, pas âge : un âge saisi une fois ne vieillit pas et emporte avec
    # lui le niveau de vie privée par défaut (cf. privacy.age_of).
    birth_year: int | None = None
    schedule: dict = {}
    # Niveau de vie privée — vide = déduit de l'âge (cf. privacy.default_level_for_age).
    # L'âge n'est qu'un DÉFAUT : le parent peut relever ou abaisser le niveau ensuite.
    privacy_level: str = ""
    # Clé Wi-Fi du profil. Vide = on garde celle du profil, ou on en génère une à la
    # création. C'est elle qui porte l'identité de l'enfant sur le réseau (cf. wifi_keys).
    wifi_key: str = ""
    # Grille d'accès : {mode: [catégories autorisées]}. None = ne pas y toucher, ce qui
    # permet d'enregistrer un profil depuis un écran qui n'affiche pas la grille.
    # Un mode ABSENT de la grille reçue signifie « défaut de la tranche d'âge », alors
    # qu'une liste vide signifie « rien d'autorisé » : deux choses différentes.
    access: dict | None = None


@app.post("/api/profiles/pihole-setup")
async def pihole_setup():
    config = _load_config()
    m = get_monitor()
    try:
        await asyncio.get_running_loop().run_in_executor(
            None, m.pihole.setup_profiles, config.get("profiles", {}))
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"ok": False, "error_code": "server_error",
                             "error": str(e)}, status_code=500)


@app.post("/api/profiles/{key}")
async def create_or_update_profile(key: str, body: ProfileUpdate):
    if not _PROFILE_KEY_RE.match(key) or key == "monitoring":
        return JSONResponse({"ok": False, "error_code": "invalid_profile_key", "error": "Clé de profil invalide"}, status_code=400)
    # Planning validé ici : une plage illisible écrite dans config.json serait ignorée
    # par le planificateur, donc un créneau que le parent croit posé ne s'appliquerait pas.
    defaut = _scheduler.validate_schedule(body.schedule)
    if defaut:
        code, params = defaut
        return JSONResponse({"ok": False, "error_code": code, "error_params": params,
                             "error": f"planning invalide ({code})"}, status_code=400)
    config = _load_config()
    existing = config.setdefault("profiles", {}).get(key, {})
    # Un profil doit AU MINIMUM porter un planning horaire — sinon il n'applique aucune
    # règle et ne sert à rien. Exigé à la création (au moins une plage dans un jour).
    has_slot = any(body.schedule.get(d) for d in (body.schedule or {}))
    if not existing and not has_slot:
        return JSONResponse({"ok": False, "error_code": "planning_required",
                             "error": "planning_required"}, status_code=400)
    year = body.birth_year
    if year is not None:
        # Bornes larges, seulement pour écarter une faute de frappe (« 20 » au lieu de
        # « 2020 ») : le produit s'adresse à des enfants, pas à des centenaires.
        now_year = datetime.now().year
        if not (now_year - 30 <= int(year) <= now_year):
            return JSONResponse({"ok": False, "error_code": "invalid_birth_year",
                                 "error": "année de naissance invalide"}, status_code=400)
        year = int(year)
    computed_age = privacy.age_of({"birth_year": year}) if year else None
    level = body.privacy_level if body.privacy_level in privacy.LEVELS else (
        existing.get("privacy_level") or privacy.default_level_for_age(computed_age))
    # CLÉ WI-FI DU PROFIL. Chaque enfant a la sienne, et c'est elle qui l'identifie sur
    # le réseau : hostapd annonce à l'association quelle clé a servi, donc l'appareil
    # est rattaché à son profil quelle que soit son adresse MAC du moment. Une clé
    # saisie par le parent est acceptée telle quelle, sinon on garde l'existante, sinon
    # on en génère une. Un profil sans clé serait un enfant qui ne peut pas se connecter.
    submitted = (body.wifi_key or "").strip()
    if submitted and (refus := _wifi_key_refusal(config, key, submitted)):
        return refus
    wifi_key = submitted or existing.get("wifi_key") or wifi_keys.generate()
    # La grille est nettoyée avant d'être écrite : un formulaire bricolé ne doit pas
    # pouvoir autoriser une catégorie que le produit bloque à tout âge.
    if body.access is None:
        access = existing.get("access")
    else:
        access = access_grid.sanitize(body.access)
    config["profiles"][key] = {
        "name":       body.name,
        "birth_year": year,
        "devices":  existing.get("devices", []),
        "schedule": body.schedule,
        "privacy_level": level,
        "wifi_key": wifi_key,
        **({"access": access} if access else {}),
        **({"alias": existing["alias"]} if existing.get("alias") else {}),
    }
    # Figer l'étiquette envoyée aux services tiers, DÉFINITIVEMENT. Sans cela elle était
    # déduite du rang alphabétique de la clé : créer un enfant dont la clé passe avant
    # les autres faisait changer « Enfant 1 » de personne. Le compteur ne redescend
    # jamais, donc supprimer un profil ne recycle pas son numéro.
    privacy.assign_alias(config, key)
    _save_config(config)
    # Journal des décisions : une création, une modification réelle (un « Enregistrer »
    # sans changement n'écrit rien), et le changement de clé, qui déconnecte l'enfant.
    nouveau = config["profiles"][key]
    if not existing:
        db.log_event(key, "info", "", f"Profil créé : {body.name}",
                     message_key="event.profile_created", params={"name": body.name})
    else:
        if any(existing.get(c) != nouveau.get(c) for c in
               ("name", "birth_year", "schedule", "privacy_level", "access")):
            db.log_event(key, "info", "", f"Profil modifié : {body.name}",
                         message_key="event.profile_updated", params={"name": body.name})
        if wifi_key != existing.get("wifi_key"):
            db.log_event(key, "info", "", "Clé Wi-Fi changée",
                         message_key="event.wifi_key_changed", params={"name": body.name})
    # La grille décide de ce qui est bloqué : la changer sans resynchroniser Pi-hole
    # laisserait le parent devant un écran qui ne correspond à rien jusqu'à la prochaine
    # plage horaire, parfois des heures.
    if access != existing.get("access"):
        try:
            m = get_monitor()
            slot = get_slot_at(key, datetime.now())
            m._apply_pihole_mode(key, slot["mode"])
            m.notify()
        except Exception as e:
            print(f"[Dashboard] grille enregistrée, application différée ({e})")

    # Servir la clé à hostapd. Le fichier de clés est réécrit de toute façon au prochain
    # boot depuis config.json, donc une file indisponible ne perd pas la clé : elle
    # retarde seulement sa prise d'effet, ce que le parent doit savoir tout de suite.
    changed_key = wifi_key != existing.get("wifi_key")
    if changed_key or not existing:
        try:
            _queue("apply_wifi_keys", {})
        except OSError as e:
            return _queue_error("apply_wifi_keys", e)
    return JSONResponse({"ok": True, "wifi_key": wifi_key})


class ProfileDelete(BaseModel):
    # Exigé seulement pour purger l'historique : l'effacement est irréversible, comme
    # pour POST /api/profiles/{key}/purge-history.
    password: str = ""


def _wifi_key_refusal(config: dict, key: str, cle: str) -> JSONResponse | None:
    """Réponse 400 si la clé ne peut pas être celle de ce profil, sinon None.

    Une clé déjà portée par un AUTRE enfant est refusée : c'est elle qui identifie
    l'enfant à la connexion, donc partagée, l'appareil du second serait rattaché au
    premier profil, avec ses horaires et sa grille.
    """
    if not wifi_keys.valid(cle):
        return JSONResponse({"ok": False, "error_code": "wifi_key_invalid",
                             "error": "clé WPA2 invalide (8 à 63 caractères simples)"},
                            status_code=400)
    for autre, profil in (config.get("profiles") or {}).items():
        if autre != key and (profil or {}).get("wifi_key") == cle:
            return JSONResponse({"ok": False, "error_code": "wifi_key_duplicate",
                                 "error": "clé déjà utilisée par un autre enfant"},
                                status_code=400)
    return None


class WifiKeyUpdate(BaseModel):
    wifi_key: str


@app.post("/api/profiles/{key}/wifi-key")
async def update_profile_wifi_key(key: str, body: WifiKeyUpdate):
    """Enregistre la clé Wi-Fi d'un enfant, et elle seule.

    Le formulaire de profil l'enregistrait avec tout le reste (planning, grille), par un
    bouton placé bien plus bas : le bloc de la clé n'avait aucun bouton à lui.
    """
    config = _load_config()
    profils = config.get("profiles") or {}
    if (not _PROFILE_KEY_RE.match(key) or key not in profils
            or (profils[key] or {}).get("mode") == "monitoring"):
        return JSONResponse({"ok": False, "error_code": "invalid_profile_key",
                             "error": "Clé de profil invalide"}, status_code=400)
    cle = (body.wifi_key or "").strip()
    if (refus := _wifi_key_refusal(config, key, cle)):
        return refus
    if cle == profils[key].get("wifi_key"):
        return JSONResponse({"ok": True, "wifi_key": cle})
    profils[key]["wifi_key"] = cle
    _save_config(config)
    db.log_event(key, "info", "", "Clé Wi-Fi changée", message_key="event.wifi_key_changed",
                 params={"name": profils[key].get("name", key)})
    # Rechargement à chaud : seuls les appareils de CET enfant sont déconnectés, et devront
    # se reconnecter avec la nouvelle clé.
    try:
        _queue("apply_wifi_keys", {})
    except OSError as e:
        return _queue_error("apply_wifi_keys", e)
    return JSONResponse({"ok": True, "wifi_key": cle})


@app.delete("/api/profiles/{key}")
async def delete_profile(key: str, request: Request, purge_history: bool = False,
                         body: ProfileDelete | None = None):
    if not _PROFILE_KEY_RE.match(key):
        return JSONResponse({"ok": False, "error_code": "invalid_profile_key", "error": "Clé de profil invalide"}, status_code=400)
    config = _load_config()
    if key == "monitoring":
        return JSONResponse({"ok": False, "error_code": "monitoring_undeletable", "error": "Le profil monitoring ne peut pas être supprimé"}, status_code=400)
    if purge_history and not secrets.compare_digest(
            (body.password if body else ""), config.get("dashboard_password", "")):
        return JSONResponse({"ok": False, "error_code": "bad_password",
                             "error": "Mot de passe incorrect"}, status_code=403)
    # Appareils lus AVANT la suppression, sur la configuration qui s'applique : en
    # passerelle, ils ne sont pas dans le fichier mais reconnus par leur clé Wi-Fi.
    profil = (_config_with_identity().get("profiles") or {}).get(key) or {}
    ips = [d.get("ip", "") for d in profil.get("devices") or [] if d.get("ip")]
    nom = profil.get("name") or key
    config.get("profiles", {}).pop(key, None)
    _save_config(config)
    # La clé du profil supprimé ne doit plus donner accès au réseau : hostapd déconnecte
    # de lui-même les appareils dont la clé a disparu du fichier. Si c'était le dernier
    # profil, le Wi-Fi des enfants n'est plus diffusé du tout.
    try:
        _queue("apply_wifi_keys", {})
    except OSError as e:
        print(f"[Dashboard] clé du profil {key} encore active jusqu'au prochain boot : {e}")

    # Les appareils quittent les groupes du profil pour le groupe par défaut. En
    # passerelle, un appareil que plus aucun profil ne reconnaît ne sort pas (fail-safe
    # fermé) ; en mode DNS, il redevient un appareil ordinaire de la maison.
    mode = modes.OFF if enforcement_mode(config) == "gateway" else modes.FREE
    pihole_ok = False
    try:
        pihole = get_monitor().pihole
        loop = asyncio.get_event_loop()
        if ips:
            await loop.run_in_executor(None, lambda: apply_device_access(
                pihole, ips, mode=mode, group="Default", profile=key,
                reason="profil_supprimé"))
        pihole_ok = await loop.run_in_executor(None, pihole.delete_profile_groups, key)
    except Exception as e:
        print(f"[Dashboard] nettoyage Pi-hole du profil {key} incomplet : {e}")

    _scheduler.clear_temp_override(key)
    db.clear_profile_state(key)
    for ip in ips:
        db.clear_device_override(ip)
    db.log_event("", "info", "", f"Profil supprimé : {nom}",
                 message_key="event.profile_deleted", params={"name": nom})
    try:
        get_monitor().notify()
    except MonitorUnavailable:
        pass
    # Supprimer un profil ne touchait PAS ses données : daily_usage, dns_timeline et
    # events conservaient sa clé indéfiniment, sans plus aucun moyen de les atteindre
    # depuis l'interface. Le parent choisit maintenant explicitement.
    purged = db.purge_profile_history(key) if purge_history else None
    return JSONResponse({"ok": True, "purged": purged, "pihole_cleaned": pihole_ok})


class HistoryPurge(BaseModel):
    password: str


@app.post("/api/profiles/{key}/purge-history")
async def purge_profile_history(key: str, body: HistoryPurge, request: Request):
    """Efface tout l'historique d'un enfant, sans toucher à sa configuration.

    Exige une re-saisie du mot de passe, comme le mode adulte : c'est une action
    irréversible sur des données que l'enfant peut légitimement demander à voir effacées.
    """
    if not _check_session(request):
        return JSONResponse({"ok": False, "error_code": "unauthenticated",
                             "error": "Non authentifié"}, status_code=401)
    if not _PROFILE_KEY_RE.match(key):
        return JSONResponse({"ok": False, "error_code": "invalid_profile_key",
                             "error": "Clé de profil invalide"}, status_code=400)
    config = _load_config()
    if not secrets.compare_digest(body.password, config.get("dashboard_password", "")):
        return JSONResponse({"ok": False, "error_code": "bad_password",
                             "error": "Mot de passe incorrect"}, status_code=403)
    purged = db.purge_profile_history(key)
    total = sum(v for v in purged.values() if isinstance(v, int))
    db.log_event(key, "privacy_purge", "",
                 f"Historique effacé par le parent ({total} enregistrements)",
                 message_key="event.history_purged", params={"n": total})
    return JSONResponse({"ok": True, "purged": purged, "total": total})


# ------------------------------------------------------------------ #
#  Takeover — mode adulte sur poste partagé                          #
# ------------------------------------------------------------------ #

class TakeoverBody(BaseModel):
    duration_minutes: int = Field(..., ge=1, le=480)
    password: str

def _find_profile_for_ip(config: dict, ip: str) -> str | None:
    """Profil auquel appartient une adresse. `config` doit être celle qui S'APPLIQUE
    (cf. _config_with_identity), sinon un appareil reconnu par sa clé reste introuvable."""
    for pkey, profile in config.get("profiles", {}).items():
        if any(d["ip"] == ip for d in profile.get("devices", [])):
            return pkey
    return None


@app.post("/api/device/{ip}/takeover")
async def device_takeover(ip: str, body: TakeoverBody, request: Request):
    if not _check_session(request):
        return JSONResponse({"ok": False, "error_code": "unauthenticated", "error": "Non authentifié"}, status_code=401)
    config = _config_with_identity()
    if not secrets.compare_digest(body.password, config.get("dashboard_password", "")):
        return JSONResponse({"ok": False, "error_code": "bad_password", "error": "Mot de passe incorrect"}, status_code=403)
    profile_key = _find_profile_for_ip(config, ip)
    if not profile_key:
        return JSONResponse({"ok": False, "error_code": "device_not_found", "error": "Appareil non trouvé"}, status_code=404)
    db.set_device_override(ip, body.duration_minutes)
    m = get_monitor()
    # Mode adulte : appareil non filtré → accès autorisé (mode ouvert → ACCEPT en gateway).
    ok = await asyncio.get_running_loop().run_in_executor(None, lambda: apply_device_access(
        m.pihole, [ip],
        mode=modes.FREE, group="adult-override",
        profile=profile_key, reason="mode_adulte",
    ))
    db.log_event(profile_key, "info", ip,
                 f"Mode adulte activé — {body.duration_minutes} min",
                 message_key="event.adult_mode_started",
                 params={"min": body.duration_minutes})
    return JSONResponse({"ok": ok})


@app.post("/api/device/{ip}/release")
async def device_release(ip: str, request: Request):
    if not _check_session(request):
        return JSONResponse({"ok": False, "error_code": "unauthenticated", "error": "Non authentifié"}, status_code=401)
    config = _config_with_identity()
    profile_key = _find_profile_for_ip(config, ip)
    if not profile_key:
        return JSONResponse({"ok": False, "error_code": "device_not_found", "error": "Appareil non trouvé"}, status_code=404)
    db.clear_device_override(ip)
    m = get_monitor()
    slot = get_slot_at(profile_key, datetime.now())
    ok = await asyncio.get_running_loop().run_in_executor(None, lambda: apply_device_access(
        m.pihole, [ip],
        mode=slot["mode"], group=f"{profile_key}-{slot['mode']}",
        profile=profile_key, reason="fin_mode_adulte",
    ))
    db.log_event(profile_key, "info", ip, "Mode adulte annulé — retour profil enfant",
                 message_key="event.adult_mode_cancelled")
    return JSONResponse({"ok": ok})


# ------------------------------------------------------------------ #
#  Vie privée — rétention et partage avec l'IA                        #
# ------------------------------------------------------------------ #

class DetailedHistory(BaseModel):
    date: str
    password: str


@app.post("/api/profiles/{key}/detailed-history")
async def detailed_history(key: str, body: DetailedHistory, request: Request):
    """Consultation du détail horaire d'une journée, aux niveaux `summary` et `minimal`.

    Un parent inquiet doit pouvoir regarder — on ne retire pas cette possibilité. Mais
    elle devient une action DÉLIBÉRÉE : mot de passe redemandé, portée limitée à une
    seule date, et inscription au journal d'événements (onglet Événements du parent).
    Le principe : la surveillance exceptionnelle reste possible, la surveillance
    routinière devient impossible par construction.
    """
    if not _check_session(request):
        return JSONResponse({"ok": False, "error_code": "unauthenticated",
                             "error": "Non authentifié"}, status_code=401)
    if not _PROFILE_KEY_RE.match(key):
        return JSONResponse({"ok": False, "error_code": "invalid_profile_key",
                             "error": "Clé de profil invalide"}, status_code=400)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", body.date or ""):
        return JSONResponse({"ok": False, "error_code": "invalid_date",
                             "error": "Date invalide (AAAA-MM-JJ)"}, status_code=400)
    config = _load_config()
    if not secrets.compare_digest(body.password, config.get("dashboard_password", "")):
        return JSONResponse({"ok": False, "error_code": "bad_password",
                             "error": "Mot de passe incorrect"}, status_code=403)
    if key not in config.get("profiles", {}):
        return JSONResponse({"ok": False, "error_code": "unknown_profile",
                             "error": "Profil inconnu"}, status_code=404)

    # Tracé AVANT restitution : une consultation qui échouerait ensuite reste une
    # consultation demandée.
    db.log_event(key, "privacy_access", "",
                 f"Consultation détaillée de l'historique du {body.date} par le parent",
                 message_key="event.privacy_access", params={"date": body.date})

    import claude_agent
    raw = claude_agent._execute_parent_tool(
        "query_history", {"profile": key, "date": body.date}, config,
        detailed_access=True,
    )
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        data = {"error": "unavailable"}
    return JSONResponse({"ok": True, "history": data})



@app.get("/api/version")
async def version_info():
    """Version installée. Purement locale : aucune requête réseau, aucune télémétrie."""
    return JSONResponse(_version_info())


@app.get("/api/timezone")
async def get_timezone():
    """Fuseau du boîtier et heure locale correspondante.

    Le fuseau gouverne TOUS les horaires du produit : créneaux, coucher, dérogations,
    rapport du soir. Le parent doit pouvoir le vérifier et le corriger, notamment après
    un déménagement ou s'il a configuré le boîtier depuis un téléphone en déplacement.
    """
    tz = ""
    try:
        tz = os.path.realpath("/etc/localtime").split("/zoneinfo/")[-1]
    except Exception:
        tz = ""
    if "/zoneinfo" in tz or not tz:
        tz = (_load_config().get("timezone") or "")
    return JSONResponse({"timezone": tz, "now": datetime.now().strftime("%H:%M")})


class TimezoneUpdate(BaseModel):
    timezone: str


@app.post("/api/timezone")
async def set_timezone(body: TimezoneUpdate):
    # timedatectl exige les droits root : passage obligé par la file d'actions.
    try:
        _queue("set_timezone", {"timezone": body.timezone.strip()})
    except OSError as e:
        return _queue_error("set_timezone", e)
    return JSONResponse({"ok": True})


@app.get("/api/privacy")
async def privacy_settings():
    config = _load_config()
    return JSONResponse({
        "retention_days": privacy.retention_days(config),
        "share_with_ai":  privacy.share_with_ai(config),
        "ai_key_present": bool((config.get("openrouter") or {}).get("api_key")),
        "last_purge":     db.last_purge_at(),
        # Seuils en dessous desquels les revues perdent leur matière — affichés pour que
        # le parent comprenne la conséquence avant de réduire la rétention.
        "weekly_min":     db.RETENTION_WEEKLY_MIN,
        "monthly_min":    db.RETENTION_MONTHLY_MIN,
    })


class PrivacyUpdate(BaseModel):
    retention_days: int | None = Field(None, ge=0, le=3650)
    share_with_ai: bool | None = None


@app.post("/api/privacy")
async def update_privacy(body: PrivacyUpdate):
    config = _load_config()
    section = config.setdefault("privacy", {})
    if body.retention_days is not None:
        section["retention_days"] = body.retention_days
    if body.share_with_ai is not None:
        section["share_with_ai"] = body.share_with_ai
    _save_config(config)
    return JSONResponse({"ok": True,
                         "retention_days": privacy.retention_days(config),
                         "share_with_ai":  privacy.share_with_ai(config)})


@app.post("/api/privacy/purge")
async def run_purge_now():
    """Applique la rétention immédiatement, sans attendre le passage hebdomadaire."""
    config = _load_config()
    return JSONResponse({"ok": True, "result": db.purge_old_data(privacy.retention_days(config))})


# ------------------------------------------------------------------ #
#  Backup & Restore                                                   #
# ------------------------------------------------------------------ #

# Jetons de téléchargement de sauvegarde : le ZIP contient le mot de passe parent, la
# clé OpenRouter et, en gateway, les clés Wi-Fi de la box et du réseau enfants — EN CLAIR.
# La session seule ne suffit donc pas : on exige une re-saisie du mot de passe, au même
# niveau que /api/device/{ip}/takeover, qui est pourtant une action bien moins grave.
# Le téléchargement restant un GET (lien de navigation), la preuve de mot de passe est
# échangée contre un jeton à usage unique et à durée de vie courte.
_BACKUP_TOKENS: dict[str, float] = {}
_BACKUP_TOKEN_TTL = 60.0          # secondes


def _issue_backup_token() -> str:
    now = time.time()
    for tok, exp in list(_BACKUP_TOKENS.items()):
        if exp < now:
            _BACKUP_TOKENS.pop(tok, None)
    token = secrets.token_urlsafe(32)
    _BACKUP_TOKENS[token] = now + _BACKUP_TOKEN_TTL
    return token


def _consume_backup_token(token: str) -> bool:
    exp = _BACKUP_TOKENS.pop(token, None)      # usage unique : retiré dès la lecture
    return exp is not None and exp >= time.time()


class BackupAuth(BaseModel):
    password: str


@app.post("/api/backup/authorize")
async def backup_authorize(request: Request, body: BackupAuth):
    if not _check_session(request):
        return JSONResponse({"ok": False, "error_code": "unauthenticated",
                             "error": "Non authentifié"}, status_code=401)
    config = _load_config()
    if not secrets.compare_digest(body.password, config.get("dashboard_password", "")):
        return JSONResponse({"ok": False, "error_code": "bad_password",
                             "error": "Mot de passe incorrect"}, status_code=403)
    return JSONResponse({"ok": True, "token": _issue_backup_token()})


@app.get("/backup")
async def backup(request: Request, token: str = ""):
    if not _check_session(request):
        return RedirectResponse("/login")
    if not _consume_backup_token(token):
        # Pas de contenu partiel : sans preuve de mot de passe, rien ne sort.
        return JSONResponse({"ok": False, "error_code": "backup_token_required",
                             "error": "Mot de passe requis pour télécharger la sauvegarde"},
                            status_code=403)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if os.path.exists(CONFIG_PATH):
            zf.write(CONFIG_PATH, arcname="config.json")
        if os.path.exists(_DB_PATH):
            zf.write(_DB_PATH, arcname="protectado.db")
    buf.seek(0)
    filename = f"protectado-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.post("/api/restore")
async def restore(request: Request, file: UploadFile = File(...), password: str = Form("")):
    # Restaurer écrase config.json ET la base : c'est un remplacement complet du contrôle
    # parental (y compris le mot de passe admin, qui devient celui de l'archive fournie).
    # Même exigence que le téléchargement.
    if not _check_session(request):
        return JSONResponse({"ok": False, "error_code": "unauthenticated",
                             "error": "Non authentifié"}, status_code=401)
    config = _load_config()
    if not secrets.compare_digest(password, config.get("dashboard_password", "")):
        return JSONResponse({"ok": False, "error_code": "bad_password",
                             "error": "Mot de passe incorrect"}, status_code=403)
    data = await file.read()
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = zf.namelist()
            if "config.json" not in names:
                return JSONResponse(
                    {"ok": False, "error_code": "backup_missing_config", "error": "Archive invalide (config.json manquant)"},
                    status_code=400,
                )
            raw_cfg = zf.read("config.json")
            try:
                json.loads(raw_cfg)
            except json.JSONDecodeError:
                return JSONResponse(
                    {"ok": False, "error_code": "backup_bad_json", "error": "config.json invalide (JSON malformé)"},
                    status_code=400,
                )
            os.makedirs(DATA_DIR, exist_ok=True)
            zf.extract("config.json", DATA_DIR)
            if "protectado.db" in names:
                zf.extract("protectado.db", DATA_DIR)
    except zipfile.BadZipFile:
        return JSONResponse({"ok": False, "error_code": "backup_bad_zip", "error": "Fichier ZIP invalide"}, status_code=400)
    if monitor:
        monitor.reload_config()
    return JSONResponse({"ok": True})


# ------------------------------------------------------------------ #
#  Mise à jour                                                        #
# ------------------------------------------------------------------ #

_UPDATE_LOG     = os.path.join(DATA_DIR, "update.log")
_UPDATE_TRIGGER = os.path.join(DATA_DIR, "update.trigger")


@app.post("/api/update")
async def trigger_update(request: Request):
    if not _check_session(request):
        return JSONResponse({"ok": False, "error_code": "unauthenticated", "error": "Non authentifié"}, status_code=401)
    try:
        # Vider le log avant de déclencher — garantit que le dashboard
        # n'affiche que la session en cours dès le premier poll
        try:
            open(_UPDATE_LOG, "w").close()
        except Exception:
            pass
        open(_UPDATE_TRIGGER, "w").close()
    except Exception as e:
        return JSONResponse({"ok": False, "error_code": "server_error",
                             "error": str(e)}, status_code=500)
    return JSONResponse({"ok": True})


@app.get("/api/update/log")
async def update_log(request: Request):
    if not _check_session(request):
        return JSONResponse({"ok": False}, status_code=401)
    try:
        with open(_UPDATE_LOG) as f:
            return Response(f.read(), media_type="text/plain")
    except FileNotFoundError:
        return Response("", media_type="text/plain")




def _migrate_domain_rules() -> int:
    """Transforme les anciens blocages par colonne en exceptions, et renvoie leur nombre.

    `blocked_work` et `blocked_permissive` étaient deux colonnes de la table domains :
    donc globales, et incapables d'exprimer une AUTORISATION, qui est la moitié de
    l'usage réel. Elles deviennent des lignes de domain_rules à profil vide, c'est-à-dire
    « pour tous les enfants », ce qui conserve exactement leur effet. Les colonnes ne sont
    plus lues ensuite ; elles restent dans le schéma des bases existantes, sans effet.
    """
    poses = 0
    with db.get_db() as conn:
        try:
            rows = conn.execute(
                "SELECT domain, blocked_work, blocked_permissive FROM domains "
                "WHERE blocked_work=1 OR blocked_permissive=1").fetchall()
        except Exception:
            return 0          # base créée après l'abandon des colonnes
        deja = {(r["profile"], r["domain"], r["mode"])
                for r in conn.execute("SELECT profile, domain, mode FROM domain_rules")}
    for row in rows:
        for colonne, mode in (("blocked_work", modes.HOMEWORK),
                              ("blocked_permissive", modes.FREE)):
            if row[colonne] and ("", row["domain"], mode) not in deja:
                db.set_domain_rule("", row["domain"], mode, allow=False)
                poses += 1
    return poses


def _migrate_modes() -> int:
    """Traduit l'ancien vocabulaire des modes dans config.json. Renvoie le nombre de
    créneaux réécrits.

    Les plannings écrits avant le renommage portent `blocked`, `work`, `permissive`. La
    lecture les traduit déjà (modes.normalize), mais tant que le fichier garde les anciens
    noms, chaque écriture de profil les recopie, et l'interface propose des valeurs que le
    parent ne voit nulle part. On réécrit donc une fois, au démarrage.

    Les dérogations en base sont laissées telles quelles : elles expirent d'elles-mêmes
    (dérogation temporaire) ou ne valent que pour une date passée (dérogation de journée),
    et la lecture les traduit.
    """
    config = _load_config()
    reecrits = 0
    for profile in (config.get("profiles") or {}).values():
        for jour, creneaux in ((profile or {}).get("schedule") or {}).items():
            for creneau in creneaux or []:
                ancien = (creneau or {}).get("mode")
                nouveau = modes.normalize(ancien)
                if nouveau and nouveau != ancien:
                    creneau["mode"] = nouveau
                    reecrits += 1
    if reecrits:
        _save_config(config)
    return reecrits


def _migrate_wifi_keys() -> list:
    """Donne sa clé Wi-Fi à chaque profil enfant qui n'en a pas encore, et la sert.

    La clé unique du réseau enfants a disparu, remplacée par une clé par profil
    (cf. wifi_keys.py) : sans cette migration, un boîtier mis à jour n'aurait plus
    AUCUNE clé, donc plus de Wi-Fi enfants du tout. Les nouvelles clés sont lisibles
    dans l'onglet Profils, et le parent doit les dicter une fois par appareil, l'ancienne
    clé commune ne fonctionnant plus.

    Renvoie la liste des profils nouvellement dotés (vide si rien à faire).
    """
    config = _load_config()
    created = []
    for key, profile in list((config.get("profiles") or {}).items()):
        profile = profile or {}
        if profile.get("mode") == "monitoring" or wifi_keys.valid(profile.get("wifi_key")):
            continue
        profile["wifi_key"] = wifi_keys.generate()
        config["profiles"][key] = profile
        created.append(key)
    if created:
        _save_config(config)
    return created


# ------------------------------------------------------------------ #
#  Point d'entrée                                                     #
# ------------------------------------------------------------------ #

def _startup():
    """Migrations puis démarrage du moniteur. Appelé par _lifespan, boîtier configuré."""
    # Migration : figer l'étiquette de pseudonymisation des profils créés avant son
    # introduction. Tant qu'elle n'est pas posée, elle est déduite du rang
    # alphabétique de la clé et change de personne dès qu'un enfant est ajouté.
    try:
        _cfg = _load_config()
        _before = json.dumps(_cfg.get("profiles", {}), sort_keys=True)
        for _key, _p in list((_cfg.get("profiles") or {}).items()):
            if (_p or {}).get("mode") != "monitoring":
                privacy.assign_alias(_cfg, _key)
        if json.dumps(_cfg.get("profiles", {}), sort_keys=True) != _before:
            _save_config(_cfg)
            print("[Démarrage] étiquettes de pseudonymisation figées pour les profils existants")
    except Exception as e:
        print(f"[Démarrage] attribution des étiquettes ignorée ({e})")
    # Migration : les exceptions par domaine (cf. _migrate_domain_rules).
    try:
        _n = _migrate_domain_rules()
        if _n:
            print(f"[Démarrage] {_n} blocage(s) de domaine repris en exception")
    except Exception as e:
        print(f"[Démarrage] migration des exceptions ignorée ({e})")
    # Migration : le vocabulaire des modes (cf. _migrate_modes).
    try:
        _n = _migrate_modes()
        if _n:
            print(f"[Démarrage] vocabulaire des modes mis à jour dans {_n} créneau(x)")
    except Exception as e:
        print(f"[Démarrage] migration du vocabulaire des modes ignorée ({e})")
    # Migration : une clé Wi-Fi par profil enfant (cf. _migrate_wifi_keys).
    try:
        _created = _migrate_wifi_keys()
        if _created:
            print(f"[Démarrage] clé Wi-Fi générée pour {len(_created)} profil(s) : "
                  f"{', '.join(_created)}. À dicter sur leurs appareils.")
        # Servir les clés à hostapd à CHAQUE démarrage, même sans changement : le
        # fichier de clés vit hors de la sauvegarde et peut manquer après une
        # restauration ou une réinstallation de la posture.
        try:
            _queue("apply_wifi_keys", {})
        except OSError as e:
            print(f"[Démarrage] clés non servies à hostapd ({e}), le point d'accès "
                  f"les relira à son prochain démarrage")
    except Exception as e:
        print(f"[Démarrage] migration des clés Wi-Fi ignorée ({e})")
    try:
        _start_monitor()
    except Exception as e:
        print(f"[Démarrage] monitor non démarré ({e}) — dashboard en mode dégradé")


if __name__ == "__main__":
    import uvicorn
    # L'objet app, pas « dashboard:app » : cette chaîne ferait réimporter ce fichier sous
    # le nom dashboard, avec des globales distinctes de celles de __main__.
    uvicorn.run(app, host="0.0.0.0", port=8080, reload=False)
