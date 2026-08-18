# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Arnaud Ortais
# Dual-licensed: AGPL-3.0 (open source) or Commercial License — see LICENSE and LICENSE-COMMERCIAL.
import io
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
from datetime import datetime, timedelta
from fastapi import FastAPI, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

import random
import database as db
import privacy
from monitor import ProtectadoMonitor
from paths import CONFIG_PATH, DATA_DIR, DB_PATH as _DB_PATH, ACTION_QUEUE_DIR
import scheduler as _scheduler
from scheduler import get_slot_at
from access_control import apply_device_access
import device_grace
from device_grace import KIDS_NET
import claude_agent
import threading as _threading

app = FastAPI(title="Protectado")
templates = Jinja2Templates(directory="templates")

monitor: ProtectadoMonitor | None = None

_status_cache: dict = {}          # {"data": ..., "ts": datetime}
_STATUS_CACHE_TTL = 8             # secondes — légèrement sous le refresh SSE (10s)
_ai_key_invalid: bool = False     # True si une 401 OpenRouter a été reçue depuis le dernier redémarrage

# Sessions : token → expiry datetime (TTL 24h)
_sessions: dict[str, datetime] = {}
SESSION_TTL = timedelta(minutes=30)

# Rate-limiting login : ip → liste de timestamps de tentatives
_login_attempts: dict[str, list[float]] = {}
LOGIN_WINDOW_SEC  = 300   # fenêtre 5 minutes
LOGIN_MAX_ATTEMPTS = 10   # max 10 tentatives par fenêtre

SUPPORTED_LANGS = ["fr", "en", "es", "pt"]
_PROFILE_KEY_RE = re.compile(r'^[a-z0-9_]{1,64}$')
_DOMAIN_RE      = re.compile(r'^(?:[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$')
_VALID_OVERRIDE_MODES = {"blocked", "work", "permissive", "free", "normal"}
_VALID_MODES = {"blocked", "work", "permissive"}
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
    global monitor
    if monitor:
        monitor.reload_config()


def _check_session(request: Request) -> bool:
    token = request.cookies.get("fw_session")
    if not token or token not in _sessions:
        return False
    if datetime.now() > _sessions[token]:
        del _sessions[token]
        return False
    _sessions[token] = datetime.now() + SESSION_TTL  # fenêtre glissante
    return True


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
    if _is_kids_client(request) or host == "protectado.admin":
        if (path == "/admin-info" or path == "/api/child/status"
                or path.startswith("/static") or path.startswith("/api/i18n")):
            return await call_next(request)
        return RedirectResponse(url="/admin-info", status_code=302)

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
    if path == "/login":
        return await call_next(request)
    if not _check_session(request):
        if path.startswith("/api/"):
            return JSONResponse({"ok": False, "error_code": "session_expired",
                                 "error": "session_expired"}, status_code=401)
        return RedirectResponse(url="/login", status_code=302)
    return await call_next(request)


def get_monitor() -> ProtectadoMonitor:
    global monitor
    if monitor is None:
        monitor = ProtectadoMonitor()
        try:
            monitor.pihole.setup_profiles(monitor.config["profiles"])
        except Exception as e:
            print(f"[Setup] Avertissement Pi-hole setup : {e}")
        monitor.start(interval=60)
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
    On ne crée le répertoire que s'il manque VRAIMENT, et sans le verrouiller en 0700 :
    un mkdir en 0700 par ce processus rendrait la file illisible pour le runner… et
    inversement, un échec silencieux ici laissait l'assistant tourner sans fin.
    """
    if not os.path.isdir(ACTION_QUEUE_DIR):
        os.makedirs(ACTION_QUEUE_DIR, mode=0o770, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d%H%M%S%f")
    path = os.path.join(ACTION_QUEUE_DIR, f"action-{ts}.json")
    try:
        with open(path, "w") as f:
            json.dump({"action": action, "args": args,
                       "queued_at": datetime.now().isoformat()}, f)
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

_PASS_WORDS = ["soleil", "riviere", "montagne", "foret", "ocean", "nuage", "etoile",
               "jardin", "renard", "hibou", "cerise", "abeille", "lune", "tigre",
               "saphir", "pomme", "orage", "dauphin", "cactus", "bambou", "comete",
               "violette", "castor", "menthe", "colline", "brise", "galet", "pivoine"]

def _gen_passphrase() -> str:
    return "-".join(random.sample(_PASS_WORDS, 3))

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

@app.get("/api/child/status")
async def child_status(request: Request):
    """État affiché à l'ENFANT sur sa propre page (protectado.admin).

    Servie sans authentification : être sur le réseau enfants suffit. Le produit se
    réclame de la confiance et du dialogue ; un enfant doit pouvoir savoir seul pourquoi
    un site ne répond pas, quelles sont les règles, et ce qui est enregistré.

    NE CONTIENT JAMAIS : l'historique de navigation (un frère ou une sœur a accès à
    cette page), la liste des appareils, une clé Wi-Fi, un identifiant, ni quoi que ce
    soit du profil d'un AUTRE enfant. Mode en cours, règles, politique — pas de données.
    """
    ip = request.client.host if request.client else ""
    config = _load_config()
    key = _find_profile_for_ip(config, ip)
    out = {
        "recognized": bool(key),
        "retention_days": privacy.retention_days(config),
        "share_with_ai": privacy.share_with_ai(config),
        # Ce que le boîtier enregistre, en une phrase — rendu côté client via i18n.
        "records": "domains_only",
    }
    if not key:
        return JSONResponse(out)

    profile = config["profiles"][key]
    now = datetime.now()
    slot = get_slot_at(key, now)
    day_keys = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    today = day_keys[now.weekday()]
    out.update({
        "name":  profile.get("name", ""),      # son PROPRE prénom, sur son propre appareil
        "mode":  slot.get("mode"),
        "slot_start": slot.get("slot_start", ""),
        "slot_end":   slot.get("slot_end", ""),
        "privacy_level": privacy.level_of(profile),
        # Planning de la journée — les règles, pas les données.
        "today": [
            {"start": sl.get("start"), "end": sl.get("end"), "mode": sl.get("mode")}
            for sl in (profile.get("schedule", {}).get(today) or [])
        ],
        # Consultations détaillées récentes le concernant (§ 11.2) : l'enfant est informé
        # que son historique a été regardé, comme le parent l'est dans son journal.
        "recent_privacy_access": [
            {"date": (e.get("timestamp") or "")[:10]}
            for e in db.get_events_for_profile(key, limit=100)
            if e.get("type") == "privacy_access"
        ][:5],
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

@app.get("/api/onboarding/genkey")
async def onboarding_genkey():
    return JSONResponse({"key": _gen_passphrase()})

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
    kids_key: str = ""          # gateway uniquement (WPA2 8–63)
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
        if not (8 <= len(body.kids_key) <= 63):
            return JSONResponse({"ok": False, "error_code": "kids_key_length", "error": "kids_key WPA2 8–63"}, status_code=400)
        kids_ssid = body.kids_ssid or f"{body.box_ssid}-Protectado"
        pending.update({"box_ssid": body.box_ssid, "box_key": body.box_key,
                        "kids_ssid": kids_ssid, "kids_key": body.kids_key})
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


async def _build_status() -> dict:
    """Construit les données de status en appelant Pi-hole."""
    loop = asyncio.get_event_loop()
    m = get_monitor()
    config = m.config

    active_ips, queries = await asyncio.gather(
        loop.run_in_executor(None, m.scanner.get_active_ips),
        loop.run_in_executor(None, lambda: m.pihole.get_recent_queries(minutes=5)),
    )
    by_ip = m.pihole.queries_by_client(queries)

    profiles_data = {}
    for pname, profile in config["profiles"].items():
        device_ips = [d["ip"] for d in profile.get("devices", [])]
        dns_queries = []
        for ip in device_ips:
            dns_queries.extend(by_ip.get(ip, []))

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

        profiles_data[pname] = {
            "name": profile["name"],
            "age": profile["age"],
            "active_devices": [ip for ip in device_ips if ip in active_ips],
            "device_ips": device_ips,
            "dns_queries_last_5min": len(dns_queries),
            "last_seen_hours": last_seen_hours,
            "is_bedtime": get_slot_at(pname, datetime.now())["mode"] == "blocked",
            "takeover": takeover,
        }

    return {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "profiles": profiles_data,
    }


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
    global _ai_key_invalid
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
    """Resync les blacklists Pi-hole pour work et permissive de tous les profils.
    Appelé après génération du rapport — synce tous les groupes, pas seulement
    le mode actuel, pour que les blacklists soient à jour même en mode blocked.
    """
    import domain_classifier as classifier
    m = get_monitor()
    api = m.pihole
    for pname, profile in m.config["profiles"].items():
        if profile.get("mode") == "monitoring" or not profile.get("devices"):
            continue
        for mode in ("work", "permissive"):
            group_name = f"{pname}-{mode}"
            group_id = api.get_group_id(group_name)
            if group_id is None:
                continue
            blacklist = classifier.get_active_blacklist(mode)
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


@app.get("/api/events")
async def events(limit: int = 50):
    return JSONResponse(db.get_recent_events(limit))


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
    with db.get_db() as conn:
        rows = conn.execute(
            "SELECT profile, date, mode, reason, created_at FROM schedule_overrides ORDER BY date DESC"
        ).fetchall()
    return JSONResponse([dict(r) for r in rows])


class OverrideCreate(BaseModel):
    profile: str
    date: str
    mode: str
    reason: str = ""


@app.post("/api/overrides")
async def create_override(body: OverrideCreate):
    if body.mode not in _VALID_OVERRIDE_MODES:
        return JSONResponse({"ok": False, "error_code": "invalid_mode", "error": "Mode invalide"}, status_code=400)
    with db.get_db() as conn:
        conn.execute("""
            INSERT INTO schedule_overrides (profile, date, mode, reason, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(profile, date) DO UPDATE SET
                mode=excluded.mode, reason=excluded.reason, created_at=excluded.created_at
        """, (body.profile, body.date, body.mode, body.reason, datetime.now().isoformat()))
    db.log_event(body.profile, "info", "",
                 f"Override planning {body.date} : mode {body.mode}"
                 + (f" — {body.reason}" if body.reason else ""),
                 message_key=("event.schedule_override_reason" if body.reason
                              else "event.schedule_override"),
                 params={"date": body.date, "mode": body.mode, "reason": body.reason})
    get_monitor().notify()
    return JSONResponse({"ok": True})


@app.delete("/api/overrides/{profile}/{date}")
async def delete_override(profile: str, date: str):
    with db.get_db() as conn:
        conn.execute(
            "DELETE FROM schedule_overrides WHERE profile=? AND date=?",
            (profile, date)
        )
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
    if body.mode not in ("permissive", "work", "blocked"):
        return JSONResponse({"ok": False, "error_code": "invalid_mode", "error": "mode invalide"}, status_code=400)
    if not (5 <= body.minutes <= 240):
        return JSONResponse({"ok": False, "error_code": "invalid_duration", "error": "durée invalide (5–240 min)"}, status_code=400)

    m = get_monitor()
    profile_cfg = m.config["profiles"].get(body.profile)
    if not profile_cfg:
        return JSONResponse({"ok": False, "error_code": "unknown_profile", "error": "profil inconnu"}, status_code=400)

    import domain_classifier as _dc
    _scheduler.set_temp_override(body.profile, body.mode, body.minutes)
    m._apply_pihole_mode(body.profile, body.mode)
    db.log_event(body.profile, "info", "",
                 f"Override temporaire (GUI) : mode {body.mode} pendant {body.minutes} min",
                 message_key="event.temp_override_started",
                 params={"mode": body.mode, "min": body.minutes})
    m.notify()

    def _restore():
        try:
            _scheduler.clear_temp_override(body.profile)
            cfg  = json.load(open(CONFIG_PATH))
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
    _scheduler.clear_temp_override(profile)
    m = get_monitor()
    slot = _scheduler.get_slot_at(profile, datetime.now())
    m._apply_pihole_mode(profile, slot["mode"])
    db.log_event(profile, "info", "",
                 f"Override temporaire annulé — retour en mode {slot['mode']}",
                 message_key="event.temp_override_cancelled",
                 params={"mode": slot["mode"]})
    m.notify()
    return JSONResponse({"ok": True})


class DomainUpdate(BaseModel):
    category: str | None = None
    blocked_work: int | None = None
    blocked_permissive: int | None = None


@app.patch("/api/domains/{domain:path}")
async def update_domain_route(domain: str, body: DomainUpdate):
    if not _DOMAIN_RE.match(domain.lower()):
        return JSONResponse({"ok": False, "error_code": "invalid_domain", "error": "Domaine invalide"}, status_code=400)
    from domain_classifier import update_domain as _update
    from claude_agent import _sync_pihole_blacklists
    _update(
        domain,
        category=body.category,
        blocked_work=body.blocked_work,
        blocked_permissive=body.blocked_permissive,
        by="parent"
    )
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
    async def event_generator():
        # Baseline = ID le plus récent au moment de la connexion.
        # Seuls les événements postérieurs à la connexion seront poussés.
        # Si la DB était vide, baseline = 0 → tout nouvel événement sera poussé.
        init = db.get_recent_events(limit=1)
        last_event_id = init[0]["id"] if init else 0

        while True:
            if await request.is_disconnected():
                break
            try:
                status_data = (await status()).body
                yield f"event: status\ndata: {status_data.decode()}\n\n"

                events_list = db.get_recent_events(limit=50)
                if events_list:
                    newest_id = events_list[0]["id"]
                    if newest_id > last_event_id:
                        new = [e for e in events_list if e["id"] > last_event_id]
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

@app.get("/devices", response_class=HTMLResponse)
async def devices_page(request: Request):
    t = _t()
    return templates.TemplateResponse(request, "devices.html", {"t": t})


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


@app.get("/api/devices")
async def list_devices():
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
                if d.get("ip"):
                    arp_devices[d["ip"]] = d
    except (OSError, ValueError):
        pass   # pas encore de scan, ou mode gateway : la section bypass reste vide

    # Typage (téléphone / console / imprimante…) déduit du fabricant de la MAC : les
    # appareils vus SEULEMENT en ARP n'ont aucune donnée FTL, c'est la seule information
    # exploitable pour les présenter au parent.
    from arp_scanner import _guess_device_type
    for ip, d in arp_devices.items():
        d.setdefault("device_type", _guess_device_type(d.get("vendor", ""), d.get("mac", "")))

    # Enregistrer automatiquement dans Pi-hole les appareils QU'IL CONNAÎT DÉJÀ.
    # Surtout PAS ceux vus uniquement en ARP : les inscrire comme clients Pi-hole les
    # ferait basculer en « connu de FTL » au chargement suivant, donc disparaître de la
    # section « bypass » — la détection s'auto-détruirait en une page. Et ça ne servirait
    # à rien : un appareil qui ne passe pas par Pi-hole n'émettra jamais de requête.
    known_client_ips = set(pihole_clients.keys())
    for ip, dev in network_devices.items():
        if ip not in known_client_ips:
            m.pihole.ensure_client_exists(ip, dev.get("mac", ""), dev.get("hostname", ""))

    # Union des trois sources
    all_ips = set(pihole_clients) | set(network_devices) | set(arp_devices)

    # Carte inverse ip → profile_key depuis config
    assigned: dict[str, str] = {}
    assigned_mac: dict[str, str] = {}
    for key, profile in config.get("profiles", {}).items():
        for device in profile.get("devices", []):
            assigned[device["ip"]] = key
            assigned_mac[device["ip"]] = device.get("mac", "")
    device_names: dict[str, str] = config.get("device_names", {})

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

        hostname = (ph.get("name") or ph.get("hostname") or
                    net.get("hostname") or arp.get("hostname") or
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
            "device_type":     arp.get("device_type", "unknown"),
        }

        if known_ftl:
            pihole_list.append(entry)
        else:
            bypass_list.append(entry)

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
    if body.profile_key:
        device_grace.forget(ip=body.ip, mac=body.mac)

    # Appliquer immédiatement dans Pi-hole
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
        # Appareil non géré → accès autorisé (mode non-"blocked" → ACCEPT en gateway).
        try:
            apply_device_access(
                m.pihole, [body.ip],
                mode="permissive", group="Default", reason="désassignation",
            )
        except Exception:
            pass

    m.notify()
    return JSONResponse({"ok": True})


class DeviceRename(BaseModel):
    ip: str
    name: str = ""


@app.get("/api/devices/pending")
async def pending_devices():
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
    pending = device_grace.unassigned(
        [{"ip": ip, "mac": info["mac"], "hostname": names.get(ip) or info["hostname"] or ""}
         for ip, info in sorted(known.items())],
        config,
    )
    profiles = [{"key": k, "name": v["name"]}
                for k, v in config.get("profiles", {}).items() if k != "monitoring"]
    # Appareils du réseau enfants (gateway) : annoter le délai de grâce restant, pour que
    # la carte affiche « il reste N h » puis « délai dépassé ». Aucun blocage à ce stade.
    device_grace.annotate(pending, config)
    return JSONResponse({"devices": pending, "profiles": profiles,
                         "grace_hours": device_grace.grace_hours(config) if device_grace.is_active(config) else 0})


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
    # Le parent a tranché : plus de compte à rebours pour cet appareil.
    device_grace.forget(ip=body.ip)
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
        m.pihole.set_client_comment(body.ip, name or f"Protectado — {body.ip}")
    except Exception as e:
        print(f"[Dashboard] Avertissement rename Pi-hole : {e}")

    return JSONResponse({"ok": True})


# ------------------------------------------------------------------ #
#  Profiles CRUD                                                      #
# ------------------------------------------------------------------ #

@app.get("/api/profiles")
async def list_profiles():
    config = _load_config()
    profiles = config.get("profiles", {})
    # Exposer le niveau EFFECTIF : un profil créé avant cette version n'a pas de champ
    # `privacy_level`, et l'interface afficherait « détaillé » pour un adolescent de 17
    # ans alors que le moteur applique déjà le niveau déduit de son âge.
    out = {}
    for key, p in profiles.items():
        p = dict(p or {})
        p["privacy_level"] = privacy.level_of(p)
        out[key] = p
    return JSONResponse(out)


@app.get("/api/network/info")
async def network_info():
    """Infos réseau non sensibles, pour adapter les explications affichées au parent :
    en gateway l'enfant doit rejoindre le Wi-Fi du boîtier (dont on donne le nom), en
    dns_only il reste sur le Wi-Fi de la box. La clé Wi-Fi n'est JAMAIS exposée ici."""
    net = _load_config().get("network") or {}
    enforcement = net.get("enforcement", "dns_only")
    return JSONResponse({
        "enforcement": enforcement if enforcement in ("dns_only", "gateway") else "dns_only",
        "kids_ssid": (net.get("kids") or {}).get("ssid", ""),
    })


class ProfileUpdate(BaseModel):
    name: str
    age: int | None = None
    schedule: dict = {}
    # Niveau de vie privée — vide = déduit de l'âge (cf. privacy.default_level_for_age).
    # L'âge n'est qu'un DÉFAUT : le parent peut relever ou abaisser le niveau ensuite.
    privacy_level: str = ""


@app.post("/api/profiles/pihole-setup")
async def pihole_setup():
    config = _load_config()
    m = get_monitor()
    try:
        m.pihole.setup_profiles(config.get("profiles", {}))
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"ok": False, "error_code": "server_error",
                             "error": str(e)}, status_code=500)


@app.post("/api/profiles/{key}")
async def create_or_update_profile(key: str, body: ProfileUpdate):
    if not _PROFILE_KEY_RE.match(key) or key == "monitoring":
        return JSONResponse({"ok": False, "error_code": "invalid_profile_key", "error": "Clé de profil invalide"}, status_code=400)
    config = _load_config()
    existing = config.setdefault("profiles", {}).get(key, {})
    # Un profil doit AU MINIMUM porter un planning horaire — sinon il n'applique aucune
    # règle et ne sert à rien. Exigé à la création (au moins une plage dans un jour).
    has_slot = any(body.schedule.get(d) for d in (body.schedule or {}))
    if not existing and not has_slot:
        return JSONResponse({"ok": False, "error_code": "planning_required",
                             "error": "planning_required"}, status_code=400)
    level = body.privacy_level if body.privacy_level in privacy.LEVELS else (
        existing.get("privacy_level") or privacy.default_level_for_age(body.age))
    config["profiles"][key] = {
        "name":     body.name,
        "age":      body.age,
        "devices":  existing.get("devices", []),
        "schedule": body.schedule,
        "privacy_level": level,
        **({"alias": existing["alias"]} if existing.get("alias") else {}),
    }
    # Figer l'étiquette envoyée aux services tiers, DÉFINITIVEMENT. Sans cela elle était
    # déduite du rang alphabétique de la clé : créer un enfant dont la clé passe avant
    # les autres faisait changer « Enfant 1 » de personne. Le compteur ne redescend
    # jamais, donc supprimer un profil ne recycle pas son numéro.
    privacy.assign_alias(config, key)
    _save_config(config)
    return JSONResponse({"ok": True})


@app.delete("/api/profiles/{key}")
async def delete_profile(key: str, purge_history: bool = False):
    if not _PROFILE_KEY_RE.match(key):
        return JSONResponse({"ok": False, "error_code": "invalid_profile_key", "error": "Clé de profil invalide"}, status_code=400)
    config = _load_config()
    if key == "monitoring":
        return JSONResponse({"ok": False, "error_code": "monitoring_undeletable", "error": "Le profil monitoring ne peut pas être supprimé"}, status_code=400)
    config.get("profiles", {}).pop(key, None)
    _save_config(config)
    # Supprimer un profil ne touchait PAS ses données : daily_usage, dns_timeline et
    # events conservaient sa clé indéfiniment, sans plus aucun moyen de les atteindre
    # depuis l'interface. Le parent choisit maintenant explicitement.
    purged = db.purge_profile_history(key) if purge_history else None
    return JSONResponse({"ok": True, "purged": purged})


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
    for pkey, profile in config.get("profiles", {}).items():
        if any(d["ip"] == ip for d in profile.get("devices", [])):
            return pkey
    return None


@app.post("/api/device/{ip}/takeover")
async def device_takeover(ip: str, body: TakeoverBody, request: Request):
    if not _check_session(request):
        return JSONResponse({"ok": False, "error_code": "unauthenticated", "error": "Non authentifié"}, status_code=401)
    config = _load_config()
    if not secrets.compare_digest(body.password, config.get("dashboard_password", "")):
        return JSONResponse({"ok": False, "error_code": "bad_password", "error": "Mot de passe incorrect"}, status_code=403)
    profile_key = _find_profile_for_ip(config, ip)
    if not profile_key:
        return JSONResponse({"ok": False, "error_code": "device_not_found", "error": "Appareil non trouvé"}, status_code=404)
    db.set_device_override(ip, body.duration_minutes)
    m = get_monitor()
    # Mode adulte : appareil non filtré → accès autorisé (non-"blocked" → ACCEPT en gateway).
    ok = apply_device_access(
        m.pihole, [ip],
        mode="permissive", group="adult-override",
        profile=profile_key, reason="mode_adulte",
    )
    db.log_event(profile_key, "info", ip,
                 f"Mode adulte activé — {body.duration_minutes} min",
                 message_key="event.adult_mode_started",
                 params={"min": body.duration_minutes})
    return JSONResponse({"ok": ok})


@app.post("/api/device/{ip}/release")
async def device_release(ip: str, request: Request):
    if not _check_session(request):
        return JSONResponse({"ok": False, "error_code": "unauthenticated", "error": "Non authentifié"}, status_code=401)
    config = _load_config()
    profile_key = _find_profile_for_ip(config, ip)
    if not profile_key:
        return JSONResponse({"ok": False, "error_code": "device_not_found", "error": "Appareil non trouvé"}, status_code=404)
    db.clear_device_override(ip)
    m = get_monitor()
    slot = get_slot_at(profile_key, datetime.now())
    ok = apply_device_access(
        m.pihole, [ip],
        mode=slot["mode"], group=f"{profile_key}-{slot['mode']}",
        profile=profile_key, reason="fin_mode_adulte",
    )
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
    seule date, et inscription au journal d'événements (visible par le parent dans
    l'onglet Événements, et par l'enfant sur sa propre page). Le principe : la
    surveillance exceptionnelle reste possible, la surveillance routinière devient
    impossible par construction.
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
    # consultation demandée, et l'enfant a le droit de le savoir.
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
    global monitor
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




# ------------------------------------------------------------------ #
#  Point d'entrée                                                     #
# ------------------------------------------------------------------ #

if __name__ == "__main__":
    import uvicorn
    # Ne démarrer le monitor QUE si le boîtier est configuré. En posture CONFIG
    # (onboarding), le dashboard sert l'assistant SANS monitor (config.json absent
    # ou incomplet → pas de Pi-hole à surveiller).
    if _is_configured():
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
        try:
            get_monitor()
        except Exception as e:
            print(f"[Démarrage] monitor non démarré ({e}) — dashboard en mode dégradé")
    else:
        print("[Démarrage] boîtier NON configuré — assistant d'onboarding (pas de monitor)")
    uvicorn.run("dashboard:app", host="0.0.0.0", port=8080, reload=False)
