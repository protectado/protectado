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
import threading
import time
import glob
import logging
import secrets
import shutil
import subprocess
from datetime import datetime

import doh_catalog
import modes
import station_identity
import system_maintenance
import wifi_keys
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
GATEWAY_RECHECK  = 30    # revérification du matériel AP (activation/perte de l'effecteur)
# Rafraîchissement de l'identité des stations (qui est associé, avec quelle clé, sur quelle
# adresse). Court, parce que c'est le délai entre « l'enfant rejoint le Wi-Fi » et « ses
# règles s'appliquent » : l'appareil doit d'abord obtenir un bail, donc quelques secondes
# ne se remarquent pas, une minute si.
IDENTITY_REFRESH = 4
# Réécriture PÉRIODIQUE du fichier d'identité, même sans changement, pour que l'agent le
# considère encore comme une image de la réalité (cf. station_identity.MAX_AGE_SECONDS).
# Elle doit rester nettement plus lente que IDENTITY_REFRESH : l'agent guette ce fichier
# et réveille son cycle complet dès qu'il bouge. Le réécrire à chaque interrogation
# faisait tourner le cycle de surveillance toutes les 4 secondes au lieu de 60, soit
# quinze fois plus d'appels à Pi-hole et d'écritures en base, et autant d'événements.
IDENTITY_STAMP_REFRESH = 60

_last_cleanup = 0.0
_last_gateway_check = 0.0
_last_identity = 0.0
_last_bypass_publish = 0.0
_last_flow_sample = 0.0
_last_identity_write = 0.0
_rate_memo: dict = {}             # mac → (instant, rx_bytes, tx_bytes) du relevé publié
_identity_signature = None      # dernière image publiée, pour ne journaliser qu'aux changements

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


def _valid_mode(mode: str) -> bool:
    """Le mode demandé est-il un mode de créneau connu ?

    La liste vivait ici en copie de celle du planificateur. Elle est dans modes.py, et
    l'ancien vocabulaire y est traduit : une action mise en file juste avant une mise à
    jour du boîtier reste donc exécutable après.
    """
    return modes.is_slot_mode(mode)


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
KIDS_CIDR  = "192.168.50.0/24"    # sous-réseau enfants : refusé par défaut en sortie
_BOOTSTRAP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bootstrap")
WIFI_SCAN       = os.path.join(DATA_DIR, "wifi_scan.json")        # résultat scan (lu par l'assistant)
BOX_VALIDATION  = os.path.join(DATA_DIR, "box_validation.json")   # résultat test box (lu par l'assistant)
ARP_SCAN        = os.path.join(DATA_DIR, "arp_scan.json")         # inventaire ARP (mode dns_only)
FWD_CHAIN  = "PROTECTADO_FWD"     # chaîne filter dédiée : DROP par appareil
DNS_CHAIN  = "PROTECTADO_DNS"     # chaîne nat dédiée : forçage DNS
BYPASS_CHAIN = "PROTECTADO_BYPASS"  # chaîne filter dédiée : contournements du DNS rejetés
FWD6_CHAIN = "PROTECTADO_FWD6"    # chaîne ip6tables : aucun transfert IPv6 via l'AP
DOH_SET    = "protectado_doh"     # ipset des résolveurs DoH connus (catalog/doh_resolvers.json)
DASH_PORT  = "8080"              # port interne du dashboard (uvicorn, non privilégié / sandboxé)
IPTABLES   = "iptables"           # backend nft sur Ubuntu ; /usr/sbin dans le PATH systemd
GATEWAY_STATUS = os.path.join(DATA_DIR, "gateway_status.json")  # état lisible par le dashboard

# Vrai UNIQUEMENT si mode gateway ET matériel AP présent ET base iptables posée.
# Tant que c'est False, l'effecteur ne touche pas à iptables (dns_only = no-op strict).
_gateway_active = False

# Blocage DoH par adresse : empreinte du catalogue chargé, disponibilité de l'ipset, et
# raison lisible quand il manque (exposée au parent, cf. _active_status).
_doh_stamp = None
_doh_available = False
_doh_problem = ""

# Adresses des stations identifiées, qui reçoivent chacune leurs règles dans
# PROTECTADO_BYPASS (compteur par appareil), et instant de la dernière reconstruction de
# la chaîne : ses compteurs repartent alors de zéro (cf. _publish_bypass_counters).
_bypass_ips: tuple = ()
_bypass_generation = ""
BYPASS_COUNTERS = "bypass_counters.json"      # dans DATA_DIR, lu par le moniteur
BYPASS_COUNTERS_REFRESH = 60   # secondes entre deux publications des compteurs

# Relevés de flux pour la détection d'un tunnel (VPN) par le moniteur : octets par
# appareil et part de sa destination principale, toutes les 5 min, sur une heure.
FLOW_SAMPLE_SEC = 60      # un relevé par minute : assez fin pour juger sur 15 min
FLOW_WINDOW_SEC = 900     # fenêtre glissante publiée, celle du moniteur (VPN_WINDOW_SEC)
FLOW_STATS = "flow_stats.json"     # dans DATA_DIR, lu par le moniteur
_flow_prev: dict = {}              # flux → octets cumulés au relevé précédent
_flow_samples: dict = {}           # ip → relevés de la dernière heure
_flow_missing_logged = False
# Premier relevé depuis le démarrage du runner fait : jusque-là, conntrack rend ce que
# chaque connexion DÉJÀ OUVERTE a transporté depuis son début (217 Mo d'un coup, vu sur
# le boîtier). Ce relevé-là sert de référence et n'est pas publié, sans quoi le volume
# du jour compterait deux fois ce qui précède le redémarrage.
_flow_started = False


def _ip6t(*args) -> bool:
    """Lance ip6tables (table filter). True si la commande a réussi."""
    r = subprocess.run(["ip6tables", *args], capture_output=True, text=True)
    if r.returncode != 0 and r.stderr.strip() and args[0] != "-D":
        log.warning(f"ip6tables {' '.join(args)} → {r.stderr.strip()}")
    return r.returncode == 0


def _ensure_ipv6_closed():
    """Aucun trafic IPv6 ne transite par le réseau enfants.

    Aujourd'hui rien n'y invite : le DHCP de l'AP ne distribue que de l'IPv4, aucune
    annonce de routeur IPv6 n'y est émise, et le routage IPv6 n'est pas activé. Mais
    rien ne le garantissait : un routage IPv6 activé plus tard (paquet installé, réglage
    système) ouvrirait une sortie qui ne passe par aucune de nos règles, toutes IPv4.

    Choix retenu : refus de TOUT transfert IPv6 depuis et vers wlan_ap, en tête de
    FORWARD. Plutôt que de désactiver IPv6 sur l'interface : le refus ne touche que le
    transit, laisse intact le trafic local de l'AP (adresses de lien) et ne dépend pas de
    l'ordre de montée de l'interface.
    """
    if not shutil.which("ip6tables"):
        log.warning("gateway : ip6tables absent, transfert IPv6 non verrouillé "
                    "(il reste inactif tant que le routage IPv6 l'est)")
        return
    _ip6t("-N", FWD6_CHAIN)
    _ip6t("-F", FWD6_CHAIN)
    _ip6t("-A", FWD6_CHAIN, "-i", AP_IFACE, "-j", "DROP")
    _ip6t("-A", FWD6_CHAIN, "-o", AP_IFACE, "-j", "DROP")
    _ip6t("-D", "FORWARD", "-j", FWD6_CHAIN)                # absent → échec ignoré
    _ip6t("-I", "FORWARD", "1", "-j", FWD6_CHAIN)


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
    """Pose la base iptables du mode gateway (idempotent) : chaînes dédiées + forçage DNS.

    POLARITÉ : LISTE BLANCHE. La chaîne se terminait sans règle de fond, avec seulement
    des DROP nommant les appareils bloqués : tout ce qui n'était pas nommé PASSAIT. Un
    appareil au MAC neuf, donc à l'IP inconnue, sortait sans filtrage et sans horaires.
    La chaîne finit maintenant par un DROP de tout le sous-réseau enfants, et les
    autorisations sont ajoutées devant, appareil par appareil, quand ils sont identifiés
    ET que leur planning les autorise. Ce qui n'est pas reconnu ne sort pas.

    La chaîne est VIDÉE ici : au démarrage comme au réarmement, on repart d'un état connu
    plutôt que d'hériter de règles d'une version antérieure. Le réseau enfants est donc
    fermé jusqu'au premier cycle de l'agent, quelques secondes plus tard.
    """
    # 1) Chaîne FORWARD dédiée + saut depuis FORWARD, AVANT les ACCEPT de base de la
    #    couche réseau (uplink-persist).
    _ipt("-N", FWD_CHAIN)                                   # existe déjà → erreur ignorée
    _ipt("-F", FWD_CHAIN)
    _reload_doh_set()            # le set doit exister avant la règle qui le cite
    # Comptage des octets par connexion, lu par _sample_flows (détection de tunnel).
    subprocess.run(["sysctl", "-w", "net.netfilter.nf_conntrack_acct=1"],
                   capture_output=True, text=True)
    _rebuild_bypass()
    # Contournements d'abord : ils sont rejetés même pour un appareil autorisé à sortir.
    # Les autorisations s'insèrent donc en position 2, juste après ce saut.
    _ipt("-A", FWD_CHAIN, "-s", KIDS_CIDR, "-j", BYPASS_CHAIN)
    # Refus de fond, en DERNIÈRE position : les autorisations s'insèrent devant.
    # Seul le sens SORTANT est policé (source = réseau enfants) ; le retour des connexions
    # établies reste géré par la couche réseau, comme avant.
    _ipt("-A", FWD_CHAIN, "-s", KIDS_CIDR, "-j", "DROP")
    _ensure_fwd_jump_first()
    _ensure_ipv6_closed()
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


def _ensure_fwd_jump_first():
    """Le saut vers PROTECTADO_FWD doit être la PREMIÈRE règle de FORWARD.

    Derrière l'ACCEPT des connexions établies ou l'ACCEPT wlan_ap → wlan_up de la couche
    réseau, un appareil qui passe en « off » garderait ses flux en cours, voire tout
    son accès. Vérifier seulement que le saut EXISTE ne suffit pas : une règle insérée
    en tête après nous le ferait reculer. Contrôle bon marché, refait à chaque
    revérification de l'effecteur.
    """
    r = subprocess.run([IPTABLES, "-S", "FORWARD"], capture_output=True, text=True)
    regles = [l.strip() for l in (r.stdout or "").splitlines() if l.startswith("-A FORWARD")]
    if regles and regles[0] == f"-A FORWARD -j {FWD_CHAIN}":
        return
    for _ in range(10):                                     # doublons éventuels compris
        if not _ipt("-D", "FORWARD", "-j", FWD_CHAIN):
            break
    _ipt("-I", "FORWARD", "1", "-j", FWD_CHAIN)
    if regles:
        log.warning("gateway : saut vers PROTECTADO_FWD remis en tête de FORWARD")


def _bypass_rules(ips=None) -> list[tuple]:
    """Règles de PROTECTADO_BYPASS, dans l'ordre. Sautée depuis PROTECTADO_FWD pour le
    seul trafic du réseau enfants : rien ici ne touche OUTPUT, le boîtier garde l'accès
    aux résolveurs chiffrés dont il a besoin (classification Cloudflare DoH).

    REJECT et non DROP : le client apprend tout de suite que la voie est fermée et
    retombe sur le DNS classique, intercepté par le forçage du port 53, au lieu
    d'attendre un délai. Chaque règle porte en commentaire le type de contournement.

    Chaque station identifiée reçoit sa propre copie des règles : c'est ce qui donne un
    compteur de tentatives PAR APPAREIL. Les règles génériques suivent, en filet, pour
    une adresse autorisée avant que son identité soit publiée.
    """
    regles = [
        # DNS-over-TLS (tcp/853) et DNS-over-QUIC (udp/853).
        ("-p", "tcp", "--dport", "853", "-m", "comment", "--comment", "pt:dot",
         "-j", "REJECT", "--reject-with", "tcp-reset"),
        ("-p", "udp", "--dport", "853", "-m", "comment", "--comment", "pt:dot",
         "-j", "REJECT"),
    ]
    if _doh_available:
        # DNS-over-HTTPS vers un résolveur connu : son port 443, en TCP et en QUIC.
        for proto, fin in (("tcp", ("--reject-with", "tcp-reset")), ("udp", ())):
            regles.append(("-p", proto, "--dport", "443", "-m", "set", "--match-set",
                           DOH_SET, "dst", "-m", "comment", "--comment", "pt:doh",
                           "-j", "REJECT", *fin))
    ips = _bypass_ips if ips is None else ips
    return [("-s", ip) + r for ip in ips for r in regles] + regles


def _signaler_doh(probleme: str):
    """Blocage DoH indisponible. Journalisé quand la cause CHANGE seulement : le runner
    retente toutes les 30 s, et répéter le même message noierait le journal."""
    global _doh_available, _doh_problem
    if probleme != _doh_problem:
        log.critical(f"gateway : {probleme}")
    _doh_available, _doh_problem = False, probleme


def _reload_doh_set() -> bool:
    """Charge les adresses du catalogue DoH dans l'ipset, d'un bloc (ipset restore :
    set neuf rempli à côté puis échangé, jamais de set à moitié vide). Sans ipset,
    l'effecteur continue sans ce blocage et le dit : c'est un manque de protection, pas
    une raison de laisser tout le réseau enfants sans règles.
    """
    global _doh_available, _doh_problem, _doh_stamp
    _doh_stamp = doh_catalog.stamp(doh_catalog.PATH)
    if not shutil.which("ipset"):
        _signaler_doh("ipset absent : le blocage DNS-over-HTTPS par adresse est inactif")
        return False
    ips = sorted(doh_catalog.load(doh_catalog.PATH)["ipv4"])
    script = "\n".join([
        f"create {DOH_SET} hash:ip -exist",
        f"create {DOH_SET}_new hash:ip -exist",
        f"flush {DOH_SET}_new",
        *(f"add {DOH_SET}_new {ip}" for ip in ips),
        f"swap {DOH_SET}_new {DOH_SET}",
        f"destroy {DOH_SET}_new",
    ]) + "\n"
    r = subprocess.run(["ipset", "restore"], input=script, capture_output=True, text=True)
    if r.returncode != 0:
        _signaler_doh(f"ipset en échec : {(r.stderr or '').strip()[:160]}")
        return False
    _doh_available, _doh_problem = True, ""
    log.info(f"gateway : {len(ips)} résolveur(s) DoH chargé(s) dans {DOH_SET}")
    return True


def _maybe_reload_doh():
    """Recharge l'ipset si le catalogue a changé (mise à jour « catalogue seul »), ou
    tant que le blocage est indisponible : un ipset installé après le démarrage doit
    lever l'état « degraded » sans redémarrer le runner. Les règles ne sont reposées
    que si la disponibilité du blocage a changé."""
    if doh_catalog.stamp(doh_catalog.PATH) == _doh_stamp and _doh_available:
        return
    avant = _doh_available
    _reload_doh_set()
    if _doh_available != avant:
        _rebuild_bypass()
        _write_gateway_status(*_active_status())


def _active_status() -> tuple[str, str]:
    """(état, détail) d'un effecteur armé : « degraded » quand un blocage manque."""
    if _doh_problem:
        return "degraded", _doh_problem
    return "ok", "forçage DNS + FORWARD actifs"


def _rebuild_bypass():
    """Vide et repose PROTECTADO_BYPASS : état connu à chaque armement."""
    global _bypass_generation
    _bypass_generation = datetime.now().isoformat()
    _ipt("-N", BYPASS_CHAIN)                                # existe déjà → erreur ignorée
    _ipt("-F", BYPASS_CHAIN)
    for regle in _bypass_rules():
        _ipt("-A", BYPASS_CHAIN, *regle)


def _sync_bypass_ips(ips):
    """Reconstruit PROTECTADO_BYPASS quand l'ensemble des stations identifiées change."""
    global _bypass_ips
    cible = tuple(sorted(ips))
    if cible == _bypass_ips:
        return
    _bypass_ips = cible
    if _gateway_active:
        _rebuild_bypass()


_COUNTER_RE = re.compile(r"/\*\s*pt:([a-z]+)\s*\*/")


def _parse_bypass_counters(sortie: str) -> dict:
    """{ip: {type: paquets}} depuis « iptables -L PROTECTADO_BYPASS -v -n -x ». Seules
    les règles par appareil comptent ; le filet générique n'a pas d'appareil."""
    compteurs: dict = {}
    for ligne in (sortie or "").splitlines():
        champs = ligne.split()
        type_ = _COUNTER_RE.search(ligne)
        if not type_ or len(champs) < 9 or not champs[0].isdigit():
            continue
        source = champs[7]
        if source == "0.0.0.0/0":
            continue
        par_type = compteurs.setdefault(source.removesuffix("/32"), {})
        par_type[type_.group(1)] = par_type.get(type_.group(1), 0) + int(champs[0])
    return compteurs


def _publish_bypass_counters():
    """Publie les tentatives de contournement refusées, par appareil et par type, pour
    le moniteur (data/bypass_counters.json). Compteurs iptables plutôt qu'une cible
    NFLOG : ils existent déjà sur les règles posées, sans démon d'écoute ni journal de
    paquets. La génération dit au moniteur quand ils sont repartis de zéro."""
    if not _gateway_active:
        return
    r = subprocess.run([IPTABLES, "-L", BYPASS_CHAIN, "-v", "-n", "-x"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return
    try:
        with open(os.path.join(DATA_DIR, BYPASS_COUNTERS), "w") as f:
            json.dump({"updated_at": datetime.now().isoformat(),
                       "generation": _bypass_generation,
                       "counters": _parse_bypass_counters(r.stdout)}, f)
    except OSError as e:
        log.error(f"compteurs de contournement non publiés : {e}")


_KV_RE = re.compile(r"\b(src|dst|sport|dport|bytes)=(\S+)")


def _parse_conntrack(sortie: str) -> dict:
    """{(proto, src, sport, dst, dport): (src, dst, octets)} depuis « conntrack -L -o
    extended ». Le premier quadruplet d'une ligne est le sens aller ; les octets sont
    ceux des deux sens (nf_conntrack_acct doit être actif, sinon aucun champ bytes)."""
    flux = {}
    for ligne in (sortie or "").splitlines():
        champs = ligne.split()
        proto = next((c for c in champs if c in ("tcp", "udp", "icmp", "gre", "esp")), "")
        aller, octets = {}, 0
        for cle, valeur in _KV_RE.findall(ligne):
            if cle == "bytes":
                octets += int(valeur) if valeur.isdigit() else 0
            elif cle not in aller:
                aller[cle] = valeur
        if "src" in aller and "dst" in aller:
            cle = (proto, aller["src"], aller.get("sport", ""), aller["dst"], aller.get("dport", ""))
            flux[cle] = (aller["src"], aller["dst"], octets)
    return flux


def _sample_flows(now=None):
    """Un relevé : pour chaque appareil du réseau enfants, ce qui a circulé depuis le
    relevé précédent et la part de sa destination principale. Publié sur un quart
    d'heure glissant (data/flow_stats.json) ; c'est le moniteur qui en juge, en croisant avec
    les requêtes DNS de l'appareil. Rien n'est coupé ici.

    Le boîtier lui-même et le réseau enfants ne sont pas des destinations de sortie.
    """
    global _flow_prev, _flow_missing_logged, _flow_started
    if not _gateway_active:
        return
    if not shutil.which("conntrack"):
        if not _flow_missing_logged:
            _flow_missing_logged = True
            log.warning("gateway : conntrack absent, pas de détection de tunnel")
        return
    now = time.time() if now is None else now
    r = subprocess.run(["conntrack", "-L", "-o", "extended"], capture_output=True, text=True)
    if r.returncode != 0:
        return
    courant = _parse_conntrack(r.stdout)
    if not _flow_started:
        _flow_started = True
        _flow_prev = {cle: v[2] for cle, v in courant.items()}
        return
    par_appareil: dict = {}
    for cle, (src, dst, octets) in courant.items():
        if not station_identity.is_kids_ip(src) or station_identity.is_kids_ip(dst):
            continue
        delta = max(0, octets - _flow_prev.get(cle, 0))
        par_dst = par_appareil.setdefault(src, {})
        par_dst[dst] = par_dst.get(dst, 0) + delta
    _flow_prev = {cle: v[2] for cle, v in courant.items()}

    for ip, par_dst in par_appareil.items():
        total = sum(par_dst.values())
        top_dst, top = max(par_dst.items(), key=lambda kv: kv[1])
        _flow_samples.setdefault(ip, []).append({
            "t": now, "bytes": total, "top_dst": top_dst,
            "top_share": round(top / total, 2) if total else 0.0})
    for ip in list(_flow_samples):
        _flow_samples[ip] = [e for e in _flow_samples[ip] if now - e["t"] <= FLOW_WINDOW_SEC]
        if not _flow_samples[ip]:
            del _flow_samples[ip]
    try:
        with open(os.path.join(DATA_DIR, FLOW_STATS), "w") as f:
            json.dump({"updated_at": datetime.now().isoformat(),
                       "window_sec": FLOW_WINDOW_SEC, "samples": _flow_samples}, f)
    except OSError as e:
        log.error(f"relevés de flux non publiés : {e}")


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


# Ports que Pi-hole doit écouter, et RIEN d'autre. Le « o » signifie « facultatif » : FTL
# démarre même si le port est déjà pris, ce qui évite qu'un conflit transforme une
# interface d'administration indisponible en résolveur DNS mort.
PIHOLE_PORTS = "81o,[::]:81o"


def _ensure_pihole_ports():
    """Retire l'écoute TLS de Pi-hole sur le 443. Idempotent, silencieux si déjà fait.

    LE DÉFAUT. L'installation plaçait l'interface de Pi-hole sur « 81o,443os,[::]:81o,
    [::]:443os » : le 81 en clair, et le 443 en TLS. Protectado, lui, se sert sur le :80
    (REDIRECT vers le :8080, cf. _ensure_dashboard_redirect) et n'a jamais rien à faire
    sur le 443.

    CE QUE ÇA PRODUISAIT. Quand le boîtier n'est pas encore prêt — le runner n'a pas
    encore posé le REDIRECT, ou l'agent n'écoute pas encore sur le 8080 — une visite sur
    http://protectado.local échoue sur le :80. Les navigateurs récents tentent alors le
    HTTPS, tombent sur le 443 de Pi-hole, et le parent atterrit sur l'interface
    d'administration de Pi-hole avec un certificat auto-signé, au lieu de son tableau de
    bord. Pire s'il y revient : le navigateur a mémorisé un site HTTPS pour ce nom.

    Aucun port n'écoutant plus le 443, la tentative HTTPS échoue franchement et le
    navigateur revient au HTTP, qui est la seule adresse que le produit publie.

    Le DNS (:53) n'est pas concerné, et le :81 en clair reste servi : c'est celui que la
    carte « Interface Pi-hole » des réglages donne au parent.
    """
    if not shutil.which("pihole-FTL"):
        return
    actuel = subprocess.run(["pihole-FTL", "--config", "webserver.port"],
                            capture_output=True, text=True)
    sortie = (actuel.stdout or "").strip()

    # La DÉCISION ne porte que sur la présence de « 443 » dans la sortie, et pas sur une
    # égalité avec la valeur qu'on veut écrire. Le format d'affichage de pihole-FTL n'est
    # pas un contrat : s'il encadre la valeur de guillemets ou la préfixe du nom du
    # réglage, une comparaison exacte ne correspondrait JAMAIS, et ce runner
    # réécrirait la configuration puis redémarrerait le résolveur à chaque démarrage.
    if actuel.returncode != 0 or not sortie:
        # Illisible : on ne touche à rien. Écrire à l'aveugle coûterait une coupure DNS
        # par démarrage du runner, pour un défaut qui n'est peut-être pas là.
        log.warning("webserver.port de Pi-hole illisible — écoute du 443 non vérifiée "
                    f"(code {actuel.returncode}) : {(actuel.stderr or '').strip()[:120]}")
        return
    if "443" not in sortie:
        return
    log.info(f"webserver.port de Pi-hole : {sortie} → {PIHOLE_PORTS}")
    r = subprocess.run(["pihole-FTL", "--config", "webserver.port", PIHOLE_PORTS],
                       capture_output=True, text=True)
    if r.returncode != 0:
        log.error("Impossible de retirer l'écoute TLS de Pi-hole sur le 443 : "
                  f"{r.stderr.strip()[:200]}")
        return
    # Le réglage n'est lu qu'au démarrage du serveur web intégré. Sans ce redémarrage, le
    # 443 reste ouvert jusqu'au prochain reboot, donc le défaut persiste toute la session.
    rr = subprocess.run(["systemctl", "restart", "pihole-FTL"],
                        capture_output=True, text=True)
    if rr.returncode != 0:
        log.error(f"Redémarrage de pihole-FTL échoué : {rr.stderr.strip()[:200]}")
        return
    log.info("Pi-hole n'écoute plus que le :81 en clair — le 443 est libéré")


def _forward_allowed_ips() -> set:
    """Adresses actuellement AUTORISÉES dans la chaîne (lecture de l'état réel)."""
    r = subprocess.run([IPTABLES, "-S", FWD_CHAIN], capture_output=True, text=True)
    if r.returncode != 0:
        return set()
    out = set()
    for line in (r.stdout or "").splitlines():
        m = re.match(r"^-A\s+%s\s+-s\s+([0-9.]+)(?:/32)?\s+-j\s+ACCEPT\s*$" % re.escape(FWD_CHAIN),
                     line.strip())
        if m:
            out.add(m.group(1))
    return out


def _apply_device_forward(ips, blocked: bool):
    """Autorise (blocked=False) ou retire l'autorisation (blocked=True) des IP données.

    En liste blanche, bloquer n'est pas ajouter une règle mais RETIRER l'autorisation :
    le refus de fond du sous-réseau enfants reprend la main. L'autorisation s'insère en
    position 2, après le saut vers PROTECTADO_BYPASS et devant ce refus. Idempotent dans
    les deux sens.

    Seules les adresses du réseau enfants sont concernées : une adresse hors de ce
    sous-réseau n'a rien à faire dans cette chaîne, et l'y accepter court-circuiterait
    les règles de la couche réseau pour un appareil qui ne nous appartient pas.
    """
    if not _gateway_active and not _rearm_gateway():
        return
    for ip in ips:                                          # déjà validées par _valid_ip
        if not station_identity.is_kids_ip(ip):
            log.warning(f"apply_forward : {ip} hors du réseau enfants, ignorée")
            continue
        present = _ipt("-C", FWD_CHAIN, "-s", ip, "-j", "ACCEPT", check_only=True)
        if blocked and present:
            _ipt("-D", FWD_CHAIN, "-s", ip, "-j", "ACCEPT")
            log.info(f"gateway : autorisation retirée pour {ip} (appareil bloqué)")
        elif not blocked and not present:
            _ipt("-I", FWD_CHAIN, "2", "-s", ip, "-j", "ACCEPT")
            log.info(f"gateway : {ip} autorisée à sortir")


def _init_enforcement():
    """Au démarrage : lit le mode, vérifie le matériel en gateway, pose la base.
    NE bascule JAMAIS silencieusement en dns_only : en gateway sans AP, ALERTE.

    Le résultat n'est PAS définitif : il est réévalué par _rearm_gateway() (cf. ci-dessous).
    """
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
    etat, texte = _active_status()
    _write_gateway_status(etat, texte + (f" ({detail})" if detail else ""))
    log.info("MODE GATEWAY actif : forçage DNS (port 53 → Pi-hole) + chaîne FORWARD prête.")


def _rearm_gateway() -> bool:
    """Réessaie d'activer l'effecteur si le matériel AP est (re)venu. Renvoie l'état.

    POURQUOI : l'activation était décidée UNE FOIS au démarrage du runner. Or ce service
    ne démarre qu'après network.target, sans aucun ordre vis-à-vis de protectado-ap.service :
    au boot, il gagne facilement la course contre hostapd et trouve wlan_ap pas encore
    passée en mode AP. Il se déclarait alors inactif POUR TOUJOURS — jusqu'au prochain
    redémarrage du service. Le boîtier continuait de tourner, Pi-hole continuait de
    classer les appareils dans le groupe « bloqué », l'interface annonçait un appareil
    bloqué… et aucun paquet n'était jamais coupé. Un enfant « bloqué » gardait ses
    sessions en cours ET pouvait en ouvrir de nouvelles.

    Le même mécanisme couvre le dongle débranché puis rebranché à chaud.
    """
    global _gateway_active
    if _enforcement_mode() != "gateway":
        return False
    ok, detail = _gateway_hardware_ok()
    if not ok:
        if _gateway_active:      # on avait le matériel, on l'a perdu : le dire, fort.
            _gateway_active = False
            _write_gateway_status("error", detail)
            log.critical(f"MATÉRIEL AP PERDU : {detail}. Filtrage niveau paquet INACTIF.")
        return False
    if _gateway_active:
        _ensure_fwd_jump_first()
        return True
    _ensure_gateway_base()
    _gateway_active = True
    etat, texte = _active_status()
    _write_gateway_status(etat, texte + (f" ({detail})" if detail else ""))
    log.warning("Effecteur gateway ACTIVÉ après coup (matériel AP disponible) — "
                "les blocages niveau paquet s'appliquent de nouveau.")
    return True


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
            if not box_ssid:
                log.error("apply_configuration refusé : gateway demande un box_ssid.")
                return
            net["enforcement"] = "gateway"
            net["box"]  = {"ssid": box_ssid, "key": box_key}
            # Plus de clé unique pour le réseau enfants : chaque profil porte la sienne
            # (cf. wifi_keys.py). Tant qu'aucun profil enfant n'existe, il n'y a aucune
            # clé, donc l'AP n'est pas diffusé. C'est l'état normal en sortie d'assistant.
            net["kids"] = {"ssid": kids_ssid}
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
      off       → groupe alice-off      (wildcard DNS .* → aucun accès)
      homework  → groupe alice-homework (ce que la grille de l'enfant autorise aux devoirs)
      free      → groupe alice-free     (ce que sa grille autorise en temps libre)
    """
    profile    = args.get("profile", "")
    # Normalisé DÈS L'ENTRÉE : une action mise en file avant une mise à jour du boîtier
    # porte l'ancien vocabulaire, et c'est le nom du mode qui sert à construire le nom du
    # groupe Pi-hole. Traduire ici évite de créer un groupe « alice-work » à côté du
    # « alice-homework » que le reste du produit utilise.
    mode       = modes.normalize(args.get("mode", "")) or args.get("mode", "")
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
            blacklist=None if not modes.is_open(mode) else blacklist
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
                blacklist=None if not modes.is_open(mode) else blacklist
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
    if _gateway_active or _rearm_gateway():
        try:
            _apply_device_forward(device_ips, blocked=not modes.is_open(mode))
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

    # Le champ s'appelle « deny ». Il s'appelait « blocked », comme l'ancien nom du mode
    # « accès coupé » : deux notions différentes sous le même mot dans la même charge
    # utile. L'ancien nom reste accepté le temps qu'une action déjà en file s'exécute.
    if "deny" not in args and "blocked" not in args:
        # Fail-safe : une action mal formée ferme, elle n'ouvre pas.
        log.warning(f"apply_forward sans champ « deny » — DROP par sécurité {ips}")
    blocked = bool(args.get("deny", args.get("blocked", True)))

    if not _gateway_active and not _rearm_gateway():
        # Distinguer les deux cas : en dns_only c'est normal, en gateway c'est une panne.
        if _enforcement_mode() == "gateway":
            log.critical(f"apply_forward IMPOSSIBLE — effecteur gateway inactif (matériel AP). "
                         f"{ips} N'EST PAS bloqué au niveau paquet.")
        else:
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


_HOSTAPD_AP_CONF = "/etc/protectado/hostapd-ap.conf"
_AP_UNITS        = ("protectado-ap.service", "protectado-ap-dhcp.service")
_HOSTAPD_CTRL    = "/var/run/hostapd"
AP_IFACE_CTRL    = AP_IFACE                 # nom du socket de contrôle = nom d'interface


def _unit_active(unit: str) -> bool:
    r = subprocess.run(["systemctl", "is-active", "--quiet", unit])
    return r.returncode == 0


def _hostapd_reload_psk() -> bool:
    """Recharge le fichier de clés SANS couper l'AP. True si hostapd a confirmé.

    hostapd déconnecte de lui-même les appareils dont la clé ne figure plus dans le
    fichier, et laisse les autres connectés. C'est exactement le comportement voulu
    quand on régénère la clé d'un seul enfant : les autres ne sont pas touchés.
    """
    if not os.path.exists(os.path.join(_HOSTAPD_CTRL, AP_IFACE_CTRL)):
        log.warning(f"pas de socket de contrôle hostapd ({_HOSTAPD_CTRL}/{AP_IFACE_CTRL}) : "
                    f"rechargement à chaud impossible")
        return False
    r = subprocess.run(["hostapd_cli", "-p", _HOSTAPD_CTRL, "-i", AP_IFACE_CTRL,
                        "reload_wpa_psk"], capture_output=True, text=True)
    if r.returncode != 0 or "FAIL" in (r.stdout or ""):
        log.warning(f"reload_wpa_psk refusé : {(r.stderr or r.stdout).strip()[:200]}")
        return False
    return True


def _hostapd_all_sta() -> str | None:
    """État complet des stations associées, ou None si hostapd ne répond pas.

    None n'est PAS un réseau vide : c'est « je ne sais pas ». L'appelant ne doit alors
    surtout pas conclure que plus personne n'est connecté, ce qui retirerait toutes les
    autorisations et couperait les enfants pour une raison qui n'a rien à voir avec eux.
    """
    if not os.path.exists(os.path.join(_HOSTAPD_CTRL, AP_IFACE_CTRL)):
        return None
    r = subprocess.run(["hostapd_cli", "-p", _HOSTAPD_CTRL, "-i", AP_IFACE_CTRL, "all_sta"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        log.warning(f"hostapd_cli all_sta a échoué : {(r.stderr or r.stdout).strip()[:200]}")
        return None
    return r.stdout or ""


def _refresh_identity():
    """Publie qui est connecté, avec quelle clé et sur quelle adresse.

    C'EST LE REMPLAÇANT DU RATTACHEMENT PAR ADRESSE IP. La station est reconnue par la
    clé Wi-Fi qu'elle a utilisée pour s'associer (`keyid`, cf. wifi_keys.py), donc son
    adresse MAC peut tourner autant qu'elle veut. Le résultat est écrit dans
    data/station_identity.json, que l'agent sandboxé lit à chaque cycle.

    RÉCONCILIATION. Une autorisation FORWARD qui traîne sur une adresse dont plus aucune
    station identifiée n'est titulaire est retirée ici. Sans ça, une adresse libérée puis
    réattribuée par le DHCP à un autre appareil hériterait d'un accès ouvert. L'agent
    décide QUI a droit de sortir selon le planning ; le runner ne fait que refermer ce
    qui ne correspond plus à personne.
    """
    global _identity_signature, _last_identity_write
    if _enforcement_mode() != "gateway":
        return
    raw = _hostapd_all_sta()
    if raw is None:
        return                      # hostapd muet : on ne publie rien plutôt que du faux
    try:
        with open(_CONFIG_PATH) as f:
            known = list((json.load(f).get("profiles") or {}).keys())
    except Exception:
        known = []
    stations = station_identity.merge(
        station_identity.parse_all_sta(raw),
        station_identity.read_leases(),
        known_profiles=known,
    )

    # RIEN DE NOUVEAU, RIEN À ÉCRIRE. L'agent guette ce fichier pour réappliquer les
    # règles sans attendre son prochain cycle : une écriture inutile lui fait refaire
    # tout le travail de surveillance pour rien. On ne republie donc que sur changement
    # réel, plus une fois par minute pour garder l'état frais.
    # La signature ne porte QUE ce qui décide d'une règle : appareil, profil, adresse,
    # autorisation. Surtout PAS les compteurs d'octets ni la durée de connexion, qui
    # changent en permanence : les y mettre republierait le fichier à chaque interrogation
    # et ferait tourner le cycle de l'agent toutes les 4 secondes, ce qui est précisément
    # le défaut que cette comparaison corrige. Les compteurs se rafraîchissent donc au
    # rythme de la réécriture périodique, ce qui suffit largement à un affichage.
    signature = tuple((st["mac"], st["profile"], st["ip"], st["authorized"])
                      for st in stations)
    change = signature != _identity_signature
    if not change and (time.time() - _last_identity_write) < IDENTITY_STAMP_REFRESH:
        return

    # Débit depuis la publication précédente (une minute au plus), pour l'affichage
    # « en ce moment » du tableau de bord.
    global _rate_memo
    _rate_memo = station_identity.add_rates(stations, _rate_memo, time.time())
    path = os.path.join(DATA_DIR, station_identity.IDENTITY_NAME)
    try:
        station_identity.write(stations, path)
        _last_identity_write = time.time()
    except OSError as e:
        log.error(f"identité des stations non publiée ({path}) : {e}")
        return

    if change:
        _identity_signature = signature
        if stations:
            log.info("stations : " + ", ".join(
                f"{st['mac']} → "
                + (st["profile"] or ("clé inconnue" if st["authorized"]
                                     else "non authentifiée"))
                + (f" @{st['ip']}" if st["ip"] else " (sans bail)")
                for st in stations))
        else:
            log.info("stations : aucune station associée")

    # Refermer ce qui ne correspond plus à une station identifiée.
    if _gateway_active:
        identified = station_identity.identified_ips({"stations": stations})
        _sync_bypass_ips(identified)
        for ip in _forward_allowed_ips() - identified:
            _ipt("-D", FWD_CHAIN, "-s", ip, "-j", "ACCEPT")
            log.info(f"gateway : autorisation retirée pour {ip} (plus aucune station)")


def apply_wifi_keys(args: dict):
    """Réécrit le fichier de clés depuis config.json et l'applique à hostapd.

    UNE CLÉ PAR PROFIL ENFANT. L'identité de l'appareil vient de la clé utilisée à
    l'association, plus de son adresse MAC : une MAC qui tourne ne fait plus sortir
    l'appareil de son profil. Cf. wifi_keys.py pour le raisonnement complet.

    Trois cas, dans cet ordre :
      - posture dns_only : aucun AP, rien à écrire, on ne touche à rien ;
      - plus aucune clé (dernier profil enfant supprimé) : l'AP est ARRÊTÉ. Un réseau
        visible que personne ne peut rejoindre serait pire qu'un réseau absent ;
      - au moins une clé : l'AP est démarré s'il ne tournait pas, sinon rechargé à chaud.

    Le rechargement à chaud est ce qui permet de régénérer la clé d'un enfant sans
    déconnecter toute la maison, ce que faisait l'ancien changement de clé unique.
    """
    if _enforcement_mode() != "gateway":
        log.info("apply_wifi_keys ignoré (posture dns_only, pas de Wi-Fi enfants)")
        return
    try:
        with open(_CONFIG_PATH) as f:
            cfg = json.load(f)
    except Exception as e:
        log.error(f"apply_wifi_keys : config.json illisible : {e}")
        return
    try:
        n = wifi_keys.write(cfg)
    except OSError as e:
        log.error(f"apply_wifi_keys : {wifi_keys.PSK_FILE} non écrit : {e}")
        return

    if n == 0:
        # Aucun profil enfant : on arrête l'AP et on le DIT. Le tableau de bord lit cet
        # état pour expliquer au parent pourquoi le Wi-Fi des enfants n'apparaît pas.
        for unit in reversed(_AP_UNITS):
            subprocess.run(["systemctl", "stop", unit], capture_output=True, text=True)
        _write_gateway_status("no_keys", "aucun profil enfant, Wi-Fi des enfants non diffusé")
        log.info("apply_wifi_keys : aucune clé, AP enfants arrêté")
        return

    if not _unit_active(_AP_UNITS[0]):
        for unit in _AP_UNITS:
            r = subprocess.run(["systemctl", "start", unit], capture_output=True, text=True)
            if r.returncode != 0:
                log.error(f"apply_wifi_keys : démarrage {unit} échoué, "
                          f"{r.stderr.strip()[:200]}")
                return
        log.info(f"apply_wifi_keys : {n} clé(s), AP enfants démarré")
        _init_enforcement()
        return

    if not _hostapd_reload_psk():
        # Repli : redémarrer l'AP applique les clés à coup sûr, au prix d'une coupure de
        # quelques secondes pour tous les appareils. Mieux qu'une clé qui ne fonctionne
        # pas alors que le parent vient de la dicter.
        r = subprocess.run(["systemctl", "restart", _AP_UNITS[0]],
                           capture_output=True, text=True)
        if r.returncode != 0:
            log.error(f"apply_wifi_keys : redémarrage AP échoué, {r.stderr.strip()[:200]}")
            return
        log.info(f"apply_wifi_keys : {n} clé(s) appliquée(s), AP redémarré (repli)")
        return
    log.info(f"apply_wifi_keys : {n} clé(s) appliquée(s), rechargées à chaud")


HANDLERS = {
    "apply_pihole_mode": apply_pihole_mode,
    "apply_forward":     apply_forward,       # gateway : FORWARD par appareil
    "scan_arp":          scan_arp,            # dns_only : détection de contournement DNS
    "scan_wifi":         scan_wifi,           # onboarding : scan box (assistant)
    "validate_box_wifi": validate_box_wifi,   # onboarding : test clé box (assistant)
    "apply_configuration": apply_configuration,  # onboarding : finalisation (bouton « finir »)
    "set_timezone":      set_timezone,        # changement de fuseau depuis le dashboard
    "apply_wifi_keys":   apply_wifi_keys,     # clés Wi-Fi par profil enfant
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

def pending_action_files() -> list[str]:
    """Actions prêtes, dans l'ordre de dépôt. Seul le motif « action-*.json » est lu :
    un fichier en cours d'écriture porte un autre nom (cf. action_queue.TMP_PREFIX)."""
    return sorted(glob.glob(os.path.join(ACTION_QUEUE_DIR, "action-*.json")))


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
    # …et Pi-hole ne doit PAS tenir le 443 : sinon un boîtier pas encore prêt renvoie le
    # parent vers l'interface de Pi-hole en HTTPS au lieu de son tableau de bord.
    # Posé ici et non dans l'installation seule : les boîtiers déjà installés ne repassent
    # jamais par bootstrap.sh, et ils ont tous le 443 ouvert.
    _ensure_pihole_ports()
    # État système hors du dépôt (updater installé, propriété du code), qu'une mise à
    # jour seule n'atteint pas. APRÈS l'armement de l'effecteur : la protection passe
    # d'abord, et un échec ici ne doit jamais arrêter le runner.
    try:
        system_maintenance.startup()
    except Exception as e:
        log.error(f"maintenance système au démarrage en échec : {e}")
    # Migrations système (paquets, fichiers de /etc) : en tâche de fond, elles peuvent
    # attendre le réseau ou un verrou apt sans jamais retenir la file d'actions.
    threading.Thread(target=system_maintenance.migrations_loop, daemon=True,
                     name="migrations").start()

    while True:
        global _last_cleanup, _last_gateway_check, _last_identity, _last_bypass_publish
        global _last_flow_sample
        now_ts = time.time()
        if now_ts - _last_cleanup > CLEANUP_INTERVAL:
            _cleanup_stale_files()
            _last_cleanup = now_ts
        # Surveillance de l'effecteur : il ne doit jamais rester inactif sans que le
        # boîtier s'en aperçoive, ni continuer à se croire actif si la radio a disparu.
        if now_ts - _last_gateway_check > GATEWAY_RECHECK:
            _last_gateway_check = now_ts
            if _rearm_gateway():
                _maybe_reload_doh()
        # Identité des stations : c'est ce qui remplace le rattachement par adresse IP.
        if now_ts - _last_identity > IDENTITY_REFRESH:
            _last_identity = now_ts
            _refresh_identity()
        if now_ts - _last_bypass_publish > BYPASS_COUNTERS_REFRESH:
            _last_bypass_publish = now_ts
            _publish_bypass_counters()
        if now_ts - _last_flow_sample > FLOW_SAMPLE_SEC:
            _last_flow_sample = now_ts
            _sample_flows()

        for f in pending_action_files():
            process_action_file(f)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
