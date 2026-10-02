# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Arnaud Ortais
# Dual-licensed: AGPL-3.0 (open source) or Commercial License — see LICENSE and LICENSE-COMMERCIAL.
"""
station_identity.py — qui est derrière une adresse, sans faire confiance à l'adresse.

CE QUE ÇA REMPLACE. Le filtrage rattachait un appareil à un profil par son adresse IP,
elle-même issue de son adresse MAC. Les téléphones changent de MAC (adresse privée par
réseau, réseau oublié puis rejoint, réglage de confidentialité basculé), et une MAC neuve
donnait un hôte inconnu : rattaché à aucun profil, donc soumis ni aux listes de blocage
ni au planning. Le contournement ne demandait aucune compétence.

D'OÙ VIENT L'IDENTITÉ MAINTENANT. De la clé Wi-Fi utilisée à l'association. Chaque profil
enfant a la sienne (cf. wifi_keys.py) et hostapd annonce, pour chaque station connectée,
quelle clé a servi : c'est le champ `keyid`, qui vaut la clé de profil. Un secret ne tourne
pas comme une adresse.

POURQUOI INTERROGER hostapd PLUTÔT QU'ÉCOUTER SES ÉVÉNEMENTS. `hostapd_cli all_sta` donne
l'ÉTAT COMPLET des stations à l'instant de l'appel. Un flux d'événements donne des
transitions, qu'il faut ne jamais rater, recoller après une reconnexion du socket, et
réconcilier au démarrage. L'état est idempotent : si un rafraîchissement est perdu, le
suivant répare tout seul. Sur un boîtier familial où les associations se comptent par
dizaines par jour, la simplicité vaut mieux que la latence.

L'ADRESSE VIENT DU BAIL, PAS DE L'ASSOCIATION. L'association précède le DHCP : au moment
où hostapd annonce la station, elle n'a pas encore d'adresse. Le bail dnsmasq fournit le
couple MAC vers IP, donc une station identifiée mais sans bail est signalée SANS adresse.
Il n'y a rien à appliquer pour elle, et elle n'a de toute façon pas d'adresse pour émettre.

Ce module ne parle à personne : il analyse, fusionne et rend. C'est l'action runner (root)
qui interroge hostapd et écrit le fichier, et l'agent sandboxé qui le lit.
"""

import ipaddress
import json
import os
import re
import tempfile
from datetime import datetime

# Sous-réseau distribué par l'AP enfants (cf. action_runner.KIDS_GW = .1).
KIDS_NET = ipaddress.ip_network("192.168.50.0/24")

# Bail DHCP de l'AP enfants (cf. bootstrap/ap-persist.sh, dhcp-leasefile).
LEASES_FILE = "/var/lib/misc/protectado-ap.leases"

# Fichier publié par le runner et lu par l'agent. Même canal que gateway_status.json :
# le privilégié écrit, le sandboxé lit.
IDENTITY_NAME = "station_identity.json"

# Au-delà de cet âge, le fichier n'est plus considéré comme une image de la réalité : le
# runner est peut-être arrêté. L'agent s'abstient alors de piloter quoi que ce soit avec,
# plutôt que d'appliquer un état périmé.
MAX_AGE_SECONDS = 180

_MAC_RE = re.compile(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$", re.IGNORECASE)

# Drapeau hostapd qui signe une poignée de main WPA RÉUSSIE. Sans lui, la station s'est
# associée au niveau radio mais n'a rien prouvé : c'est le cas d'un appareil qui rejoue
# une clé périmée, d'un voisin qui tente, ou d'un enfant qui se trompe en la tapant. Il
# s'associe, échoue au 4-way handshake, se fait déconnecter, recommence, et apparaît donc
# dans `all_sta` en boucle sans `keyid`.
#
# C'EST UN NON-ÉVÉNEMENT, et le confondre avec une anomalie d'identité remplissait le
# journal du parent de plusieurs lignes par minute pour un téléphone qui n'entrait même
# pas sur le réseau.
AUTHORIZED_FLAG = "[AUTHORIZED]"

# Attributs numériques que hostapd expose par station, et qu'on republie tels quels.
# CE SONT DES INFORMATIONS GRATUITES : elles arrivent dans la même sortie que le keyid,
# sans une requête de plus. Le parent cherche à reconnaître un appareil au milieu
# d'adresses MAC aléatoires, et « connecté depuis 3 h, 1,2 Go reçus » l'y aide bien plus
# qu'un identifiant hexadécimal.
#
# À SAVOIR SUR LES COMPTEURS : ils repartent de zéro à chaque association, donc à chaque
# rotation de MAC. Ce sont des compteurs de SESSION, pas des totaux journaliers, et il ne
# faut pas les présenter comme un cumul.
_NUMERIC_ATTRS = ("connected_time", "inactive_msec", "signal",
                  "rx_bytes", "tx_bytes", "rx_packets", "tx_packets")


def _entier(valeur):
    """Valeur numérique de hostapd, ou None. Les attributs varient selon le pilote : un
    champ absent ou illisible doit donner « inconnu », jamais zéro, qui se lirait comme
    une mesure réelle."""
    try:
        return int(str(valeur).strip())
    except (TypeError, ValueError):
        return None


def is_private_mac(mac: str) -> bool:
    """L'adresse est-elle localement administrée, c'est-à-dire tirée au hasard ?

    C'est l'information qui explique au parent pourquoi l'appareil n'a pas de fabricant
    identifiable, et pourquoi son adresse change. Le bit 0x02 du premier octet la porte.
    """
    try:
        return bool(int(str(mac).split(":")[0], 16) & 0x02)
    except (ValueError, IndexError):
        return False


def _norm_mac(value) -> str:
    v = str(value or "").strip().lower()
    return v if _MAC_RE.match(v) else ""


def parse_all_sta(text: str) -> dict:
    """Sortie de `hostapd_cli all_sta` vers {mac: {attribut: valeur}}.

    Le format est une suite de blocs : une ligne d'adresse MAC seule, puis des lignes
    `clé=valeur`. On ne présuppose ni l'ordre des attributs ni leur présence : seule
    l'adresse MAC est structurante, et `keyid` est le seul champ dont on a besoin.
    Une station sans `keyid` est conservée mais sans profil : elle est associée avec une
    clé qui n'en portait pas, ce qui ne devrait pas arriver et doit rester visible.
    """
    stations: dict = {}
    current = None
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        mac = _norm_mac(line)
        if mac:
            current = mac
            stations.setdefault(current, {})
            continue
        if current is None or "=" not in line:
            continue
        key, _, value = line.partition("=")
        stations[current][key.strip()] = value.strip()
    return stations


def parse_leases(text: str) -> dict:
    """Fichier de baux dnsmasq vers {mac: {"ip", "hostname", "expiry"}}.

    Format : `<expiration> <mac> <ip> <nom> <client-id>`. Le nom vaut `*` quand le client
    n'en annonce pas. Si la même MAC apparaît plusieurs fois, le bail le plus récent
    (expiration la plus lointaine) gagne : c'est celui qui est en vigueur.
    """
    out: dict = {}
    for raw in (text or "").splitlines():
        parts = raw.split()
        if len(parts) < 4:
            continue
        expiry, mac, ip, hostname = parts[0], _norm_mac(parts[1]), parts[2], parts[3]
        if not mac:
            continue
        try:
            expiry_n = int(expiry)
        except ValueError:
            expiry_n = 0
        try:
            ipaddress.ip_address(ip)
        except ValueError:
            continue
        previous = out.get(mac)
        if previous and previous["expiry"] >= expiry_n:
            continue
        out[mac] = {"ip": ip, "hostname": "" if hostname == "*" else hostname,
                    "expiry": expiry_n}
    return out


def read_leases(path: str = "") -> dict:
    """Baux en vigueur. Le chemin est résolu à l'APPEL, pas à la définition, pour qu'il
    reste substituable (tests, chemin de bail différent sur une autre distribution)."""
    try:
        with open(path or LEASES_FILE) as f:
            return parse_leases(f.read())
    except OSError:
        return {}


def is_kids_ip(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip) in KIDS_NET
    except ValueError:
        return False


def merge(stations: dict, leases: dict, known_profiles=None) -> list:
    """Stations associées + baux vers la liste publiée, triée par profil puis adresse MAC.

    `known_profiles` : clés de profil existantes. Un `keyid` qui n'y figure pas (profil
    supprimé dont la clé traîne encore dans hostapd) donne une station SANS profil : elle
    ne sera donc pas autorisée. On ne devine pas à qui elle appartient.

    Le champ `authorized` distingue deux situations que le nom « station associée »
    confond : celle qui a prouvé qu'elle connaît une clé, et celle qui est en train
    d'échouer à le prouver. La seconde est ordinaire (clé périmée enregistrée sur un
    téléphone, voisin curieux, faute de frappe) et ne mérite aucune attention.
    """
    known = set(known_profiles or [])
    out = []
    for mac, attrs in stations.items():
        keyid = (attrs.get("keyid") or "").strip()
        autorisee = AUTHORIZED_FLAG in (attrs.get("flags") or "")
        # Pas de poignée de main réussie, pas de profil : une station non autorisée ne
        # transporte rien, quelle que soit la clé qu'elle prétend avoir.
        profile = keyid if (autorisee and keyid and (not known or keyid in known)) else ""
        lease = leases.get(mac) or {}
        ip = lease.get("ip", "")
        station = {
            "mac": mac,
            "keyid": keyid,
            "profile": profile,
            "authorized": autorisee,
            "ip": ip if is_kids_ip(ip) else "",
            # Nom annoncé par l'appareil au DHCP. C'est le meilleur repère pour un parent,
            # et le plus stable : il survit à la rotation de l'adresse MAC. Il est ANNONCÉ
            # par l'appareil, donc jamais une preuve d'identité, seulement une aide.
            "hostname": lease.get("hostname", ""),
            "private_mac": is_private_mac(mac),
        }
        for attr in _NUMERIC_ATTRS:
            station[attr] = _entier(attrs.get(attr))
        out.append(station)
    return sorted(out, key=lambda s: (s["profile"], s["mac"]))


def add_rates(stations: list, previous: dict, now: float) -> dict:
    """Ajoute à chaque station son débit depuis le relevé précédent, en octets par
    seconde : `down_bps` (vers l'appareil, ce que l'enfant télécharge) et `up_bps`.
    Renvoie le mémo à passer au relevé suivant.

    SENS DES COMPTEURS. hostapd compte du point de vue du point d'accès : tx_bytes est ce
    qu'il a ENVOYÉ à l'appareil, rx_bytes ce qu'il en a reçu. Mesuré sur le boîtier,
    iPhone sur YouTube : tx_bytes +4,2 Mo en 30 s, rx_bytes +0,2 Mo.

    None quand on ne sait pas : premier relevé de la station, compteur reparti de zéro
    (nouvelle association, rotation de MAC), compteur absent du pilote. Un débit nul
    inventé se lirait comme « rien ne passe ».
    """
    memo = {}
    for st in stations:
        rx, tx = st.get("rx_bytes"), st.get("tx_bytes")
        st["down_bps"] = st["up_bps"] = None
        if not isinstance(rx, int) or not isinstance(tx, int):
            continue
        memo[st["mac"]] = (now, rx, tx)
        t0, rx0, tx0 = previous.get(st["mac"], (None, None, None))
        if t0 is None or now <= t0 or rx < rx0 or tx < tx0:
            continue
        st["down_bps"] = (tx - tx0) / (now - t0)
        st["up_bps"] = (rx - rx0) / (now - t0)
    return memo


def render(stations: list) -> dict:
    return {
        "updated_at": datetime.now().isoformat(),
        "stations": list(stations),
    }


def write(stations: list, path: str) -> dict:
    """Publie l'état. Écriture atomique : l'agent peut lire à tout instant."""
    payload = render(stations)
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".station_identity.")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.chmod(tmp, 0o644)          # lu par l'agent sandboxé
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return payload


def load(path: str) -> dict:
    """État publié, ou {} s'il est absent ou illisible (donc : ne rien en déduire)."""
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def age_seconds(identity: dict) -> float | None:
    """Âge de l'état publié, None si la date est absente ou illisible."""
    try:
        return (datetime.now() - datetime.fromisoformat(identity["updated_at"])).total_seconds()
    except (KeyError, TypeError, ValueError):
        return None


def is_fresh(identity: dict, max_age: float = MAX_AGE_SECONDS) -> bool:
    """L'état décrit-il encore la réalité ?

    Un fichier absent, sans date, ou trop vieux vaut « je ne sais pas » : l'appelant doit
    alors s'abstenir plutôt que d'appliquer un état périmé.
    """
    age = age_seconds(identity)
    return age is not None and 0 <= age <= max_age


def devices_by_profile(identity: dict) -> dict:
    """{clé de profil: [{"ip", "mac", "hostname"}]} pour les stations ADRESSÉES.

    Une station sans profil (clé inconnue) ou sans bail n'apparaît pas : il n'y a rien à
    appliquer pour elle. C'est voulu qu'elle disparaisse silencieusement du rattachement,
    et qu'elle reste visible dans le fichier publié pour le diagnostic.
    """
    out: dict = {}
    for station in (identity.get("stations") or []):
        profile = (station.get("profile") or "").strip()
        ip = station.get("ip") or ""
        if not profile or not ip:
            continue
        # Les repères d'identification suivent l'appareil jusqu'aux consommateurs (profils
        # du tableau de bord, chat, rapports) : sans eux, le parent ne voit qu'une adresse.
        out.setdefault(profile, []).append({
            "ip": ip,
            "mac": station.get("mac", ""),
            "hostname": station.get("hostname", ""),
            "private_mac": station.get("private_mac", False),
            "connected_time": station.get("connected_time"),
            "rx_bytes": station.get("rx_bytes"),
            "tx_bytes": station.get("tx_bytes"),
            "down_bps": station.get("down_bps"),
            "up_bps": station.get("up_bps"),
            "signal": station.get("signal"),
            # Silence radio de la station. C'est la SEULE mesure d'activité réelle dont
            # le produit dispose : un appareil associé n'est pas un appareil utilisé, et
            # le nombre de requêtes DNS ne le dit pas (un cache DNS rend zéro, une page
            # riche en ressources rend deux cents).
            "inactive_msec": station.get("inactive_msec"),
        })
    return out


def apply_to_config(config: dict, identity: dict) -> bool:
    """Remplace la liste d'appareils de chaque profil enfant par celle de l'identité.

    Règle unique, partagée par le monitor (qui applique les droits) et le tableau de bord
    (qui affiche). Sans ce partage, l'un des deux voyait des appareils que l'autre ignorait.

    NE FAIT RIEN si l'identité n'est pas fraîche : les listes existantes continuent alors
    de s'appliquer, exactement comme avant l'introduction de l'identité. Les appareils
    hors du sous-réseau enfants sont conservés : ils ne passent pas par le point d'accès.

    Renvoie True si au moins un profil a changé d'appareils.
    """
    if not is_fresh(identity):
        return False
    by_profile = devices_by_profile(identity)
    changed = False
    for pname, profile in (config.get("profiles") or {}).items():
        if (profile or {}).get("mode") == "monitoring":
            continue
        hors_ap = [d for d in (profile.get("devices") or [])
                   if not is_kids_ip((d or {}).get("ip", ""))]
        wanted = hors_ap + by_profile.get(pname, [])
        avant = _reperes(profile.get("devices"))
        apres = _reperes(wanted)
        # LA LISTE EST TOUJOURS REMPLACÉE, même quand les repères n'ont pas bougé : les
        # compteurs de trafic et la durée de connexion vivent dans ces entrées, et le
        # tableau de bord les affiche. Comparer pour décider de remplacer laissait une
        # entrée figée dès qu'une adresse était déjà connue de la configuration, et le
        # nom annoncé par l'appareil n'arrivait jamais.
        profile["devices"] = wanted
        # `changed` ne porte que sur ce qui MÉRITE une trace : appareil, adresse, nom.
        # Y mettre les compteurs ferait une ligne de journal à chaque rafraîchissement.
        if avant != apres:
            changed = True
    return changed


def _reperes(devices) -> list:
    """Ce qui identifie durablement les appareils d'une liste, trié pour comparaison."""
    return sorted(((d or {}).get("ip", ""), (d or {}).get("mac", ""),
                   (d or {}).get("hostname", ""))
                  for d in (devices or []))


def identified_ips(identity: dict) -> set:
    """Toutes les adresses actuellement rattachées à un profil connu."""
    return {d["ip"] for devices in devices_by_profile(identity).values() for d in devices}
