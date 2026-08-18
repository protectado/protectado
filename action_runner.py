# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Arnaud Ortais
# Dual-licensed: AGPL-3.0 (open source) or Commercial License — see LICENSE and LICENSE-COMMERCIAL.
"""
action_runner.py — Processus privilégié (root) hors sandbox nono.

Rôle : lire la file d'actions écrite par dashboard.py (sandboxé)
       et exécuter les opérations Pi-hole qui nécessitent une session root/root.

Mode de fonctionnement — champ `config.network.enforcement` (déclaratif, PAS d'auto-détection) :
  "dns_only" (DÉFAUT, champ absent ⇒ dns_only) : Pi-hole est le seul mécanisme de blocage.
    Le Pi n'est pas routeur — iptables FORWARD n'a aucun effet ; toute la politique d'accès
    passe par les groupes Pi-hole. (Cet invariant ne vaut QUE en "dns_only".)
  "gateway" : le Pi est routeur (AP enfants → uplink) et le droit d'accès RÉEL est porté
    par iptables — appliqué ICI (root), en complément de Pi-hole :
      - forçage DNS : le port 53 des clients AP est redirigé vers le Pi-hole local
        (neutralise un DNS mis en dur, ex. 8.8.8.8) ;
      - blocage appareil : DROP du FORWARD par IP.
    Le champ `enforcement` n'est lu QUE dans ce processus root — la logique métier
    (scheduler, monitor, agent, dashboard) l'ignore : moins d'endroits le connaissent,
    moins on risque de casser dns_only.

Ce processus est volontairement simple et auditable.
Il ne fait QUE ce qu'on lui demande explicitement.
"""

import ipaddress
import json
import os
import re
import sys
import time
import glob
import logging
import secrets
import subprocess
from datetime import datetime

from paths import CONFIG_PATH as _CONFIG_PATH, ACTION_QUEUE_DIR, DATA_DIR

# Instance Pi-hole persistante — évite de recréer une session à chaque action
_pihole_api = None

def get_pihole_api():
    global _pihole_api
    if _pihole_api is None:
        with open(_CONFIG_PATH) as f:
            config = json.load(f)
        from pihole_api import PiHoleAPI
        _pihole_api = PiHoleAPI(config["pihole"]["host"], config["pihole"]["password"])
    return _pihole_api

LOG_FILE = "/var/log/protectado-runner.log"
POLL_INTERVAL = 2        # secondes
ACTION_MAX_AGE = 300     # rejeter les actions > 5 min (évite replay d'actions figées)
CLEANUP_INTERVAL = 3600  # nettoyage des fichiers .error/.stale toutes les heures

_last_cleanup = 0.0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [runner] %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger(__name__)


# ------------------------------------------------------------------ #
#  Validation des entrées                                             #
# ------------------------------------------------------------------ #

def _valid_ip(ip: str) -> bool:
    try:
        ipaddress.ip_address(ip)
        return True
    except ValueError:
        return False


_ALLOWED_MODES = {"blocked", "work", "permissive"}

def _valid_mode(mode: str) -> bool:
    return mode in _ALLOWED_MODES


_ALLOWED_PROFILES_RE = re.compile(r'^[a-z0-9_]{1,64}$')

def _valid_profile(profile: str) -> bool:
    return bool(_ALLOWED_PROFILES_RE.match(profile))


# ------------------------------------------------------------------ #
#  Effecteur "gateway" — filtrage niveau paquet (iptables), root only #
#  Tout est gardé par le mode : en dns_only, RIEN de ce bloc n'agit.  #
# ------------------------------------------------------------------ #

AP_IFACE   = "wlan_ap"            # interface AP enfants (Alfa MT7612U)
UP_IFACE   = "wlan_up"            # interface uplink (radio interne, client box)
KIDS_GW    = "192.168.50.1"       # IP du Pi sur le réseau enfants (résolveur local)
_BOOTSTRAP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bootstrap")
WIFI_SCAN       = os.path.join(DATA_DIR, "wifi_scan.json")        # résultat scan (lu par l'assistant)
BOX_VALIDATION  = os.path.join(DATA_DIR, "box_validation.json")   # résultat test box (lu par l'assistant)
ARP_SCAN        = os.path.join(DATA_DIR, "arp_scan.json")         # inventaire ARP (mode dns_only)
FWD_CHAIN  = "PROTECTADO_FWD"     # chaîne filter dédiée : DROP par appareil
DNS_CHAIN  = "PROTECTADO_DNS"     # chaîne nat dédiée : forçage DNS
DASH_PORT  = "8080"              # port interne du dashboard (uvicorn, non privilégié / sandboxé)
IPTABLES   = "iptables"           # backend nft sur Ubuntu ; /usr/sbin dans le PATH systemd
GATEWAY_STATUS = os.path.join(DATA_DIR, "gateway_status.json")  # état lisible par le dashboard

# Vrai UNIQUEMENT si mode gateway ET matériel AP présent ET base iptables posée.
# Tant que c'est False, l'effecteur ne touche pas à iptables (dns_only = no-op strict).
_gateway_active = False


def _ipt(*args, table=None, check_only=False):
    """Lance iptables. check_only=True → renvoie True/False (règle présente ?)."""
    cmd = [IPTABLES] + (["-t", table] if table else []) + list(args)
    r = subprocess.run(cmd, capture_output=True, text=True)
    if check_only:
        return r.returncode == 0
    if r.returncode != 0 and r.stderr.strip():
        log.warning(f"iptables {' '.join(args)} → {r.stderr.strip()}")
    return r.returncode == 0


def _enforcement_mode() -> str:
    """Mode DÉCLARÉ dans config.network.enforcement : 'dns_only' (défaut) | 'gateway'.
    Lu uniquement ici (root). Champ absent / valeur inconnue ⇒ 'dns_only'."""
    try:
        with open(_CONFIG_PATH) as f:
            m = (json.load(f).get("network") or {}).get("enforcement", "dns_only")
        return m if m in ("dns_only", "gateway") else "dns_only"
    except Exception:
        return "dns_only"


def _write_gateway_status(state: str, detail: str = ""):
    """Expose l'état de la couche gateway au dashboard (data/gateway_status.json)."""
    try:
        with open(GATEWAY_STATUS, "w") as f:
            json.dump({"state": state, "detail": detail,
                       "updated_at": datetime.now().isoformat()}, f)
    except OSError as e:
        log.error(f"Écriture {GATEWAY_STATUS} impossible : {e}")


def _gateway_hardware_ok():
    """(ok, detail) — l'interface AP est-elle présente ET en mode AP ? (pour ALERTER)."""
    if not os.path.exists(f"/sys/class/net/{AP_IFACE}"):
        return False, f"interface AP '{AP_IFACE}' absente (2ᵉ radio non détectée)"
    try:
        out = subprocess.run(["iw", "dev", AP_IFACE, "info"],
                             capture_output=True, text=True, timeout=5)
        if "type AP" in out.stdout:
            return True, ""
        return False, f"'{AP_IFACE}' présente mais pas en mode AP (hostapd démarré ?)"
    except (FileNotFoundError, subprocess.SubprocessError):
        return True, "iw indisponible — vérif partielle (interface présente)"


def _ensure_gateway_base():
    """Pose la base iptables du mode gateway (idempotent) : chaînes dédiées + forçage DNS."""
    # 1) Chaîne FORWARD dédiée (DROP par appareil) + saut depuis FORWARD, AVANT les ACCEPT
    #    de base de la couche réseau (uplink-persist).
    _ipt("-N", FWD_CHAIN)                                   # existe déjà → erreur ignorée
    if not _ipt("-C", "FORWARD", "-j", FWD_CHAIN, check_only=True):
        _ipt("-I", "FORWARD", "1", "-j", FWD_CHAIN)
    # 2) Forçage DNS : rediriger le port 53 des clients AP (sauf déjà destiné au Pi) vers
    #    le Pi-hole local → un DNS statique (8.8.8.8) est intercepté et filtré.
    _ipt("-N", DNS_CHAIN, table="nat")
    _ipt("-F", DNS_CHAIN, table="nat")                     # idempotent : on repart propre
    for proto in ("udp", "tcp"):
        _ipt("-A", DNS_CHAIN, "-p", proto, "--dport", "53",
             "!", "-d", KIDS_GW, "-j", "REDIRECT", "--to-ports", "53", table="nat")
    if not _ipt("-C", "PREROUTING", "-i", AP_IFACE, "-j", DNS_CHAIN,
                table="nat", check_only=True):
        _ipt("-I", "PREROUTING", "1", "-i", AP_IFACE, "-j", DNS_CHAIN, table="nat")


def _ensure_dashboard_redirect():
    """Publie le dashboard sur le :80 (intuitif pour les parents) sans que le process
    sandboxé ait besoin d'un port privilégié : REDIRECT root du :80 → :8080.
    `-m addrtype --dst-type LOCAL` ⇒ SEUL le trafic destiné AU Pi est capté ; le trafic
    web des enfants (transit, destination externe) n'est PAS touché. Pi-hole est sur :81,
    donc le :80 du Pi est libre. Idempotent, actif dans les deux modes (dns_only/gateway)."""
    rule = ("-p", "tcp", "--dport", "80", "-m", "addrtype", "--dst-type", "LOCAL",
            "-j", "REDIRECT", "--to-ports", DASH_PORT)
    if _ipt("-C", "PREROUTING", *rule, table="nat", check_only=True):
        return
    _ipt("-A", "PREROUTING", *rule, table="nat")
    log.info(f"dashboard publié sur le :80 (REDIRECT LOCAL :80 → :{DASH_PORT}).")


def _apply_device_forward(ips, blocked: bool):
    """DROP (blocked) / rétablit (sinon) le FORWARD sortant des IP données. Idempotent."""
    if not _gateway_active:
        return
    for ip in ips:                                          # déjà validées par _valid_ip
        present = _ipt("-C", FWD_CHAIN, "-s", ip, "-j", "DROP", check_only=True)
        if blocked and not present:
            _ipt("-A", FWD_CHAIN, "-s", ip, "-j", "DROP")
            log.info(f"gateway : DROP FORWARD {ip} (appareil bloqué)")
        elif not blocked and present:
            _ipt("-D", FWD_CHAIN, "-s", ip, "-j", "DROP")
            log.info(f"gateway : FORWARD {ip} rétabli")


def _init_enforcement():
    """Au démarrage : lit le mode, vérifie le matériel en gateway, pose la base.
    NE bascule JAMAIS silencieusement en dns_only : en gateway sans AP, ALERTE."""
    global _gateway_active
    mode = _enforcement_mode()
    if mode != "gateway":
        _gateway_active = False
        _write_gateway_status("dns_only", "")
        log.info(f"enforcement={mode} — effecteur iptables inactif (Pi-hole seul).")
        return
    ok, detail = _gateway_hardware_ok()
    if not ok:
        # Le produit doit CRIER qu'il ne peut pas remplir sa fonction — pas de faux-semblant.
        _gateway_active = False
        _write_gateway_status("error", detail)
        log.critical(f"MODE GATEWAY MAIS MATÉRIEL AP INDISPONIBLE : {detail}. "
                     f"Le filtrage niveau paquet N'EST PAS actif. PAS de repli en dns_only.")
        return
    _ensure_gateway_base()
    _gateway_active = True
    _write_gateway_status("ok", "forçage DNS + FORWARD actifs" + (f" ({detail})" if detail else ""))
    log.info("MODE GATEWAY actif : forçage DNS (port 53 → Pi-hole) + chaîne FORWARD prête.")


# ------------------------------------------------------------------ #
#  Onboarding (posture CONFIG) — opérations réseau root pour l'assistant.
#  L'assistant (sandboxé) met ces actions en file ; le root les exécute et
#  écrit le résultat dans data/*.json (que l'assistant sonde).
# ------------------------------------------------------------------ #

def _wifi_ifaces() -> list[str]:
    """Interfaces Wi-Fi présentes (celles qui exposent un phy80211)."""
    out = []
    try:
        for name in sorted(os.listdir("/sys/class/net")):
            if os.path.exists(f"/sys/class/net/{name}/phy80211"):
                out.append(name)
    except OSError:
        pass
    return out


def _iface_driver(name: str) -> str:
    try:
        return os.path.basename(os.path.realpath(f"/sys/class/net/{name}/device/driver"))
    except OSError:
        return ""


def _resolve_uplink_iface() -> tuple[str, str]:
    """(interface, détail) — nom RÉEL de la radio d'uplink (côté box).

    `wlan_up` vient d'une règle udev qui peut ne pas avoir été appliquée (image
    fraîche, règle posée après l'énumération, /etc non persisté...). On ne peut pas
    s'y fier aveuglément : config-ap.sh, lui, trouve l'Alfa par son PILOTE, donc
    l'assistant peut très bien tourner alors que le renommage n'a jamais eu lieu —
    et le scan échouerait silencieusement sur une interface inexistante.

    Ordre : le nom attendu s'il existe, sinon la seule radio qui n'est PAS celle de
    l'AP (identifiée par son pilote mt76*), sinon rien.
    """
    if os.path.exists(f"/sys/class/net/{UP_IFACE}"):
        return UP_IFACE, ""
    candidates = [n for n in _wifi_ifaces()
                  if n != AP_IFACE and not _iface_driver(n).startswith("mt76")]
    if len(candidates) == 1:
        detail = (f"règle udev non appliquée : '{UP_IFACE}' absent, uplink détecté "
                  f"sur '{candidates[0]}' (pilote {_iface_driver(candidates[0]) or '?'})")
        log.warning(f"scan/uplink — {detail}")
        return candidates[0], detail
    if not candidates:
        return "", (f"aucune radio Wi-Fi d'uplink : '{UP_IFACE}' absent et aucune autre "
                    f"radio hors AP (interfaces vues : {', '.join(_wifi_ifaces()) or 'aucune'})")
    return "", (f"'{UP_IFACE}' absent et plusieurs radios candidates "
                f"({', '.join(candidates)}) — renommage udev à corriger")


def scan_wifi(args: dict):
    """Scan les réseaux Wi-Fi à portée sur la radio d'uplink → data/wifi_scan.json.

    En cas d'échec, `error` est TOUJOURS renseigné avec une cause exploitable : c'est
    la seule information que le parent (et nous) aurons pour comprendre pourquoi
    l'assistant ne propose aucun réseau.
    """
    res = {"networks": [], "updated_at": datetime.now().isoformat()}
    iface, detail = _resolve_uplink_iface()
    if detail:
        res["detail"] = detail
    if not iface:
        res["error"] = detail
        log.error(f"scan_wifi : {detail}")
    else:
        res["iface"] = iface
        try:
            # Radio bloquée par rfkill (typique d'une image neuve sans domaine
            # réglementaire) : le scan ne renvoie alors RIEN, sans erreur explicite.
            subprocess.run(["rfkill", "unblock", "wifi"], check=False,
                           capture_output=True, timeout=5)
        except (FileNotFoundError, subprocess.SubprocessError):
            pass
        try:
            subprocess.run(["ip", "link", "set", iface, "up"], check=False,
                           capture_output=True, timeout=10)
            r = subprocess.run(["iw", "dev", iface, "scan"],
                               capture_output=True, text=True, timeout=25)
            cur_sig, seen = None, {}
            for ln in r.stdout.splitlines():
                s = ln.strip()
                if s.startswith("signal:"):
                    cur_sig = s.split()[1]
                elif s.startswith("SSID:"):
                    name = s[5:].strip()
                    if name and name not in seen:
                        seen[name] = {"ssid": name, "signal": cur_sig}
            res["networks"] = sorted(seen.values(), key=lambda x: x["ssid"].lower())
            if not res["networks"]:
                # Aucun réseau ET une erreur de commande : remonter le stderr, pas un
                # silence. « iw » renvoie par ex. « Network is down », « Operation not
                # permitted », « resource busy » (interface en mode AP).
                err = (r.stderr or "").strip()
                if r.returncode != 0 or err:
                    res["error"] = f"iw dev {iface} scan : {err or f'code {r.returncode}'}"
                    log.error(f"scan_wifi : {res['error']}")
            log.info(f"scan_wifi ({iface}) : {len(res['networks'])} réseaux")
        except FileNotFoundError as e:
            # 'iw' absent : Ubuntu Server ne l'installe pas par défaut.
            res["error"] = f"outil manquant ({e.filename or e}) — installer le paquet 'iw'"
            log.error(f"scan_wifi : {res['error']}")
        except Exception as e:
            res["error"] = str(e)
            log.error(f"scan_wifi : {e}")
    try:
        with open(WIFI_SCAN, "w") as f:
            json.dump(res, f)
    except OSError as e:
        log.error(f"écriture {WIFI_SCAN} : {e}")


def scan_arp(args: dict):
    """Inventaire ARP du réseau local → data/arp_scan.json. Mode dns_only UNIQUEMENT.

    À quoi ça sert : en dns_only, le Pi n'est pas routeur. Un appareil qui n'utilise pas
    Pi-hole comme résolveur est TOTALEMENT invisible — il n'apparaît ni dans les requêtes,
    ni dans la table réseau de FTL. Le scan ARP est le seul moyen de constater sa présence
    sur le réseau, donc de détecter un contournement du filtrage DNS.

    Inutile en gateway : tout le trafic des enfants transite par le boîtier et le port 53
    est forcé vers Pi-hole, donc un appareil ne peut pas se soustraire à l'observation.

    Opération privilégiée (socket brut) : elle appartient au runner root, jamais au
    processus sandboxé.
    """
    res = {"devices": [], "updated_at": datetime.now().isoformat()}
    if _enforcement_mode() != "dns_only":
        res["skipped"] = "mode gateway — scan ARP sans objet"
        log.info("scan_arp ignoré (mode gateway)")
    else:
        try:
            r = subprocess.run(["arp-scan", "--localnet", "--quiet", "--retry=2"],
                               capture_output=True, text=True, timeout=60)
            seen = {}
            for line in r.stdout.splitlines():
                parts = line.split("\t")
                if len(parts) < 2:
                    continue
                ip = parts[0].strip()
                if not _valid_ip(ip) or ip in seen:
                    continue
                seen[ip] = {"ip": ip,
                            "mac": parts[1].strip(),
                            "vendor": parts[2].strip() if len(parts) > 2 else ""}
            res["devices"] = sorted(seen.values(), key=lambda d: d["ip"])
            if not res["devices"] and r.returncode != 0:
                res["error"] = (r.stderr or "").strip() or f"code {r.returncode}"
                log.error(f"scan_arp : {res['error']}")
            log.info(f"scan_arp : {len(res['devices'])} appareils vus")
        except FileNotFoundError:
            res["error"] = "outil manquant — installer le paquet 'arp-scan'"
            log.error(f"scan_arp : {res['error']}")
        except Exception as e:
            res["error"] = str(e)
            log.error(f"scan_arp : {e}")
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(ARP_SCAN, "w") as f:
            json.dump(res, f)
    except OSError as e:
        log.error(f"écriture {ARP_SCAN} : {e}")


def validate_box_wifi(args: dict):
    """Teste la connexion de wlan_up à la box (ssid/key) SANS finaliser la config.
    Réutilise uplink-persist.sh 'wifi' (netplan) puis vérifie la connectivité réelle.
    Succès → wlan_up RESTE connecté (le parent atteindra le dashboard à l'IP box).
    Échec → on retire le netplan raté (la posture CONFIG reste propre). N'active PAS
    le mode gateway : c'est apply_configuration qui finalise."""
    ssid = args.get("ssid", "")
    key  = args.get("key", "")
    res  = {"ok": False, "detail": "", "ip": "", "updated_at": datetime.now().isoformat()}
    if not ssid:
        res["detail"] = "SSID box manquant"
    else:
        try:
            env = dict(os.environ, BOX_SSID=ssid, BOX_PASS=key)
            subprocess.run(["bash", os.path.join(_BOOTSTRAP, "uplink-persist.sh"), "wifi"],
                           env=env, capture_output=True, text=True, timeout=60)
            ipr = subprocess.run(["ip", "-4", "-br", "addr", "show", UP_IFACE],
                                 capture_output=True, text=True)
            parts = ipr.stdout.split()
            ip = parts[2] if len(parts) >= 3 and "/" in parts[2] else ""
            online = subprocess.run(["ping", "-c1", "-W3", "-I", UP_IFACE, "1.1.1.1"],
                                    capture_output=True).returncode == 0
            if ip and online:
                res.update(ok=True, detail="Box connectée — Internet OK", ip=ip)
            elif ip:
                res.update(ok=True, detail="Associée (Internet non confirmé)", ip=ip)
            else:
                res.update(ok=False, detail="Échec : clé erronée ou réseau hors de portée")
                # nettoyer le netplan raté — la posture CONFIG doit rester propre
                subprocess.run(["rm", "-f", "/etc/netplan/60-protectado-uplink.yaml"], check=False)
                subprocess.run(["netplan", "generate"], check=False)
        except Exception as e:
            res["detail"] = f"Erreur : {e}"
            log.error(f"validate_box_wifi : {e}")
    try:
        with open(BOX_VALIDATION, "w") as f:
            json.dump(res, f)
    except OSError as e:
        log.error(f"écriture {BOX_VALIDATION} : {e}")
    log.info(f"validate_box_wifi {ssid!r} → ok={res['ok']} ({res['detail']})")


def _detect_posture() -> str:
    """Mode matériellement possible ('gateway'|'dns_only'), via l'orchestrateur root —
    source UNIQUE de la détection (uplink actif + wifi libre). Défaut prudent : dns_only."""
    try:
        r = subprocess.run(["bash", os.path.join(_BOOTSTRAP, "protectado-boot.sh"), "caps", "apply"],
                           capture_output=True, text=True, timeout=15)
        out = (r.stdout or "").strip().splitlines()
        return "gateway" if (out and out[-1] == "gateway") else "dns_only"
    except Exception as e:
        log.warning(f"détection posture impossible ({e}) → dns_only")
        return "dns_only"


_TZ_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+_-]*(?:/[A-Za-z0-9+_-]+){0,2}$")


def _valid_timezone(value) -> str:
    """Valide un identifiant de fuseau IANA AVANT toute utilisation.

    La chaîne vient du navigateur du parent et finit dans une commande exécutée en ROOT :
    elle est donc validée ici, du côté privilégié, jamais côté dashboard. Deux barrières :
      1. une forme stricte (« Europe/Paris », « America/Argentina/Salta »), qui exclut
         d'emblée les points, les espaces et tout ce qui ressemble à une remontée de
         chemin ou à une injection de commande ;
      2. l'existence du fichier correspondant dans /usr/share/zoneinfo, vérifiée après
         résolution du chemin réel — un lien symbolique qui sortirait de l'arborescence
         est rejeté.
    Retourne "" si la valeur n'est pas un fuseau connu de CETTE machine.
    """
    tz = (value or "").strip()
    if not tz or not _TZ_RE.match(tz):
        return ""
    base = "/usr/share/zoneinfo"
    path = os.path.realpath(os.path.join(base, tz))
    if not path.startswith(base + "/") or not os.path.isfile(path):
        return ""
    return tz


def _apply_timezone(tz: str) -> None:
    """Applique le fuseau au système. Sans effet si timedatectl est absent."""
    try:
        r = subprocess.run(["timedatectl", "set-timezone", tz],
                           capture_output=True, text=True, timeout=15)
        if r.returncode == 0:
            log.info(f"fuseau horaire → {tz}")
        else:
            log.error(f"timedatectl set-timezone {tz} : {r.stderr.strip()[:200]}")
    except Exception as e:
        log.error(f"réglage du fuseau {tz} impossible : {e}")


def apply_configuration(args: dict):
    """FINALISE l'onboarding (bouton « finir » de l'assistant). Écrit config.json
    (source de vérité). Deux postures selon le MATÉRIEL :
      • gateway  — uplink actif + wifi libre : AP enfants + NAT (reboot pour appliquer) ;
      • dns_only — sinon : filtrage DNS classique sur le réseau existant (pas d'AP, pas de reboot).
    GARDE-FOU : gateway est REFUSÉ si le matériel ne le permet pas — l'assistant sandboxé
    ne décide jamais seul du matériel ; il met juste cette action en file."""
    mode     = args.get("mode", "")
    admin_pw = args.get("admin_password", "")
    if mode not in ("gateway", "dns_only"):
        mode = "gateway" if args.get("box_ssid") else "dns_only"   # rétro-compat
    if len(admin_pw) < 6:
        log.error("apply_configuration refusé — mot de passe admin requis (6+).")
        return
    # Garde-fou matériel : jamais de gateway si le boîtier n'en est pas capable.
    if mode == "gateway" and _detect_posture() != "gateway":
        log.critical("apply_configuration REFUSÉ — gateway demandé mais matériel insuffisant "
                     "(uplink actif + wifi libre requis). Aucune écriture.")
        return
    try:
        cfg = {}
        if os.path.exists(_CONFIG_PATH):
            with open(_CONFIG_PATH) as f:
                cfg = json.load(f)
        net = cfg.setdefault("network", {})
        # Pi-hole local : mot de passe API (le monitor/agent en a besoin au démarrage).
        pihole_pw = secrets.token_urlsafe(16)
        subprocess.run(["pihole", "setpassword", pihole_pw], capture_output=True, check=False)
        cfg["pihole"] = {"host": "http://localhost:81", "password": pihole_pw}
        cfg["dashboard_password"] = admin_pw
        cfg.setdefault("profiles", {})     # profils enfants créés ensuite dans le dashboard
        lang = args.get("language")
        cfg["language"] = lang if lang in ("fr", "en", "es", "pt") else cfg.get("language", "fr")
        # Domaine réglementaire Wi-Fi — lu par ap-persist/config-ap/uplink-persist via
        # net-common.sh:pt_country(). Écrit dans les DEUX postures : en dns_only aucune
        # radio n'est pilotée aujourd'hui, mais le boîtier peut passer en gateway plus
        # tard (ajout du dongle) et le pays ne serait alors plus demandé.
        country = (args.get("country") or "").strip().upper()
        if len(country) == 2 and country.isalpha():
            net["country"] = country

        # Fuseau horaire. C'est la donnée la plus critique de tout l'onboarding : TOUT le
        # produit raisonne en heure locale — créneaux, coucher, dérogations, crons du
        # rapport et de la purge. Une image flashée à Toronto puis déployée en France
        # appliquerait le coucher de 22 h à 16 h. Rien ne réglait ce fuseau jusqu'ici.
        #
        # La valeur vient du NAVIGATEUR du parent (Intl), donc précise et purement locale,
        # sans géolocalisation ni appel à un tiers. Le pays ne suffirait pas : les
        # États-Unis, le Canada et le Brésil comptent plusieurs fuseaux, l'Espagne et le
        # Portugal aussi avec les Canaries et les Açores.
        tz = _valid_timezone(args.get("timezone"))
        if tz:
            cfg["timezone"] = tz
            _apply_timezone(tz)

        if mode == "gateway":
            box_ssid  = args.get("box_ssid", "")
            box_key   = args.get("box_key", "")
            # SSID enfants = nom du Wi-Fi box + "-Protectado" (ex. AbyssTerritory-Protectado).
            kids_ssid = (args.get("kids_ssid") or f"{box_ssid}-Protectado")
            kids_key  = args.get("kids_key", "")
            if not box_ssid or not (8 <= len(kids_key) <= 63):
                log.error("apply_configuration refusé — gateway : box_ssid + clé enfants WPA2 (8–63) requis.")
                return
            net["enforcement"] = "gateway"
            net["box"]  = {"ssid": box_ssid, "key": box_key}
            net["kids"] = {"ssid": kids_ssid, "key": kids_key}
            cfg["configured"] = True
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(_CONFIG_PATH, "w") as f:
                json.dump(cfg, f, indent=2)
            log.info("apply_configuration : config.json écrit (configured=true, gateway)")

            # Poser la posture GATEWAY (BOOT_ONLY : configs écrites, appliquées au reboot).
            env = dict(os.environ, BOOT_ONLY="1", BOX_SSID=box_ssid, BOX_PASS=box_key)
            for cmd in (["ap-persist.sh", "install"],
                        ["uplink-persist.sh", "wifi"],
                        ["uplink-persist.sh", "install"]):
                r = subprocess.run(["bash", os.path.join(_BOOTSTRAP, cmd[0]), cmd[1]],
                                   env=env, capture_output=True, text=True)
                if r.returncode != 0:
                    log.warning(f"apply_configuration : {' '.join(cmd)} → rc={r.returncode} {r.stderr.strip()[:200]}")
            log.info("apply_configuration : reboot → bascule en GATEWAY, extinction config-AP.")
            subprocess.run(["systemctl", "reboot"], check=False)
        else:
            # DNS-only : filtrage sur le réseau existant, aucune bascule réseau ⇒ pas de reboot.
            net["enforcement"] = "dns_only"
            net.pop("box", None); net.pop("kids", None)
            cfg["configured"] = True
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(_CONFIG_PATH, "w") as f:
                json.dump(cfg, f, indent=2)
            log.info("apply_configuration : config.json écrit (configured=true, dns_only)")
            # Recharger l'agent/dashboard pour initialiser le monitor avec la config.
            subprocess.run(["systemctl", "restart", "protectado-agent"], check=False)
    except Exception as e:
        log.error(f"apply_configuration : {e}")


# ------------------------------------------------------------------ #
#  Exécuteur unique : changement de mode Pi-hole                     #
# ------------------------------------------------------------------ #

def apply_pihole_mode(args: dict):
    """
    Bascule un profil vers le bon groupe Pi-hole selon le mode/slot.
    Appelé par monitor.py à chaque changement de slot ET pour les
    blocages/déblocages manuels (chat parent, override planning...).

    Modes :
      blocked    → groupe alice-blocked (wildcard DNS .*  → tout bloqué)
      work       → groupe alice-work    (blacklist entertainment+social)
      permissive → groupe alice-permissive (blacklist adult seulement)
    """
    profile    = args.get("profile", "")
    mode       = args.get("mode", "")
    blacklist  = args.get("blacklist", [])
    device_ips = args.get("device_ips", [])

    if not _valid_profile(profile):
        log.error(f"apply_pihole_mode refusé — profil invalide : {profile!r}")
        return
    if not _valid_mode(mode):
        log.error(f"apply_pihole_mode refusé — mode invalide : {mode!r}")
        return

    # Valider les IPs de la liste
    device_ips = [ip for ip in device_ips if _valid_ip(ip)]

    log.info(f"Pi-hole : {profile} → mode {mode} ({len(device_ips)} appareils)")

    try:
        api = get_pihole_api()
        ok = api.switch_profile_mode(
            profile_name=profile,
            mode=mode,
            device_ips=device_ips,
            blacklist=None if mode == "blocked" else blacklist
        )

        if not ok:
            # Groupes Pi-hole absents (premier démarrage ou reset) — setup + retry
            log.info("Groupes Pi-hole introuvables — setup initial...")
            with open(_CONFIG_PATH) as f:
                config = json.load(f)
            api.setup_profiles(config["profiles"])
            ok = api.switch_profile_mode(
                profile_name=profile,
                mode=mode,
                device_ips=device_ips,
                blacklist=None if mode == "blocked" else blacklist
            )

        if ok:
            log.info(f"Mode {mode.upper()} appliqué pour {profile}")
        else:
            log.warning(f"Échec application mode {mode} pour {profile}")

    except Exception as e:
        log.error(f"Erreur apply_pihole_mode : {e}")

    # Effecteur gateway : le droit d'accès RÉEL est porté par iptables FORWARD.
    # Appliqué INDÉPENDAMMENT du succès Pi-hole (fail-safe : le blocage ne doit pas
    # dépendre du DNS). No-op strict en dns_only (garde _gateway_active).
    if _gateway_active:
        try:
            _apply_device_forward(device_ips, blocked=(mode == "blocked"))
        except Exception as e:
            log.error(f"Erreur effecteur gateway {profile}/{mode} : {e}")


def apply_forward(args: dict):
    """
    Applique le droit FORWARD d'UN OU PLUSIEURS appareils (mode gateway uniquement).

    Émis par access_control.apply_device_access() à chaque transition PAR APPAREIL —
    mode adulte, fin de mode adulte, override expiré, (dés)assignation de profil — qui
    ne passe pas par apply_pihole_mode() (lequel couvre les transitions PAR PROFIL).
    Sans ce handler, ces transitions changeaient le groupe Pi-hole en laissant le
    FORWARD inchangé : un appareil bloqué gardait un accès direct par IP.

    No-op strict en dns_only (garde _gateway_active), comme tout ce bloc.
    """
    raw = args.get("ips") or []
    ips = [ip for ip in raw if _valid_ip(ip)]
    profile = args.get("profile", "")
    reason  = args.get("reason", "")

    if len(ips) != len(raw):
        log.warning(f"apply_forward : {len(raw) - len(ips)} IP invalide(s) ignorée(s) "
                    f"[{profile or '-'} {reason or '-'}]")
    if not ips:
        log.error(f"apply_forward refusé — aucune IP valide [{profile or '-'} {reason or '-'}]")
        return

    if "blocked" not in args:
        # Fail-safe : une action mal formée ferme, elle n'ouvre pas.
        log.warning(f"apply_forward sans champ 'blocked' — DROP par sécurité {ips}")
    blocked = bool(args.get("blocked", True))

    if not _gateway_active:
        log.info(f"apply_forward ignoré (enforcement dns_only) — {ips}")
        return

    try:
        _apply_device_forward(ips, blocked=blocked)
        log.info(f"gateway : FORWARD {'fermé' if blocked else 'ouvert'} pour {ips} "
                 f"[{profile or '-'} {reason or '-'}]")
    except Exception as e:
        log.error(f"Erreur apply_forward {ips} [{profile or '-'} {reason or '-'}] : {e}")


def set_timezone(args: dict):
    """Change le fuseau après l'installation : déménagement, ou parent qui a configuré
    le boîtier depuis un téléphone en déplacement.

    Passe par la file d'actions parce que « timedatectl » demande les droits root, que le
    dashboard n'a pas. Même validation stricte qu'à l'onboarding : la valeur vient du
    navigateur et atterrit dans une commande root.
    """
    tz = _valid_timezone(args.get("timezone"))
    if not tz:
        log.error(f"set_timezone refusé — fuseau invalide : {args.get('timezone')!r}")
        return
    _apply_timezone(tz)
    try:
        cfg = {}
        if os.path.exists(_CONFIG_PATH):
            with open(_CONFIG_PATH) as f:
                cfg = json.load(f)
        cfg["timezone"] = tz
        with open(_CONFIG_PATH, "w") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        log.error(f"set_timezone : écriture config.json impossible : {e}")
        return
    # L'agent garde le fuseau en mémoire tant qu'il tourne : sans redémarrage, les
    # créneaux continueraient d'être évalués dans l'ancien fuseau.
    subprocess.run(["systemctl", "restart", "protectado-agent"], check=False)
    log.info(f"set_timezone : {tz} appliqué, agent redémarré")


HANDLERS = {
    "apply_pihole_mode": apply_pihole_mode,
    "apply_forward":     apply_forward,       # gateway : FORWARD par appareil
    "scan_arp":          scan_arp,            # dns_only : détection de contournement DNS
    "scan_wifi":         scan_wifi,           # onboarding : scan box (assistant)
    "validate_box_wifi": validate_box_wifi,   # onboarding : test clé box (assistant)
    "apply_configuration": apply_configuration,  # onboarding : finalisation (bouton « finir »)
    "set_timezone":      set_timezone,        # changement de fuseau depuis le dashboard
}


def _cleanup_stale_files():
    """Supprime les fichiers .error et .stale de plus d'une heure."""
    for ext in ("*.error", "*.stale"):
        for path in glob.glob(os.path.join(ACTION_QUEUE_DIR, ext)):
            try:
                if time.time() - os.path.getmtime(path) > CLEANUP_INTERVAL:
                    os.remove(path)
                    log.info(f"Nettoyage : {os.path.basename(path)}")
            except OSError:
                pass


# ------------------------------------------------------------------ #
#  Boucle principale                                                  #
# ------------------------------------------------------------------ #

def process_action_file(path: str):
    try:
        with open(path) as f:
            payload = json.load(f)

        action    = payload.get("action")
        args      = payload.get("args", {})
        queued_at = payload.get("queued_at", "")

        # Rejeter les actions trop anciennes (replay protection)
        if queued_at:
            try:
                age = (datetime.now() - datetime.fromisoformat(queued_at)).total_seconds()
                if age > ACTION_MAX_AGE:
                    log.warning(f"Action expirée ({age:.0f}s > {ACTION_MAX_AGE}s) — ignorée : {path}")
                    os.rename(path, path + ".stale")
                    return
            except (ValueError, TypeError):
                pass  # queued_at mal formé / tz-aware → on continue (best effort)

        if action not in HANDLERS:
            log.warning(f"Action inconnue '{action}' dans {path} — ignorée")
        else:
            log.info(f"Exécution : {action}({args}) [queued {queued_at}]")
            HANDLERS[action](args)

        os.remove(path)

    except Exception as e:
        log.error(f"Erreur traitement {path} : {e}")
        # Renommer pour éviter une boucle infinie
        os.rename(path, path + ".error")


def main():
    if os.geteuid() != 0:
        log.error("action_runner.py doit tourner en root (sudo)")
        sys.exit(1)

    os.makedirs(ACTION_QUEUE_DIR, exist_ok=True)
    try:
        import grp
        gid = grp.getgrnam("protectado-queue").gr_gid
        os.chown(ACTION_QUEUE_DIR, 0, gid)
        os.chmod(ACTION_QUEUE_DIR, 0o2770)  # setgid : les fichiers créés héritent du groupe
    except KeyError:
        os.chmod(ACTION_QUEUE_DIR, 0o700)
        log.warning("Groupe protectado-queue introuvable — queue accessible en root uniquement")
    log.info(f"Protectado action runner démarré — surveillance {ACTION_QUEUE_DIR}")

    # Mode de fonctionnement (gateway/dns_only) : vérif matériel + base iptables.
    # (Changer enforcement nécessite un redémarrage du runner — lu une fois au boot.)
    _init_enforcement()
    # Dashboard servi sur le :80 (parents) quel que soit le mode — Pi-hole étant sur :81.
    _ensure_dashboard_redirect()

    while True:
        global _last_cleanup
        now_ts = time.time()
        if now_ts - _last_cleanup > CLEANUP_INTERVAL:
            _cleanup_stale_files()
            _last_cleanup = now_ts

        files = sorted(glob.glob(os.path.join(ACTION_QUEUE_DIR, "action-*.json")))
        for f in files:
            process_action_file(f)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
