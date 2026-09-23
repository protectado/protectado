# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Arnaud Ortais
# Dual-licensed: AGPL-3.0 (open source) or Commercial License — see LICENSE and LICENSE-COMMERCIAL.
"""
device_grace.py — Délai de grâce des appareils du réseau ENFANTS (mode gateway).

Un appareil qui rejoint le Wi-Fi du boîtier a un accès Internet libre pendant
`network.new_device_grace_hours` (défaut 24 h), le temps que le parent le remarque.
Passé ce délai, s'il n'est toujours rattaché à aucun profil ni marqué « ignorer »,
il est signalé au parent (événement + carte du dashboard).

PÉRIMÈTRE DE CETTE ÉTAPE : signalement SEULEMENT — aucun blocage, aucune règle
iptables. La mise en quarantaine (accès coupé sauf page d'information) viendra
ensuite, via l'entonnoir access_control + action_runner.

Ne concerne QUE :
  - le mode `gateway` (en dns_only le Pi n'est pas routeur, rien à appliquer) ;
  - le sous-réseau enfants (les appareils des parents restent sur le Wi-Fi de la
    box et ne passent jamais par le boîtier → jamais suivis, jamais concernés).

Le suivi est indexé sur la MAC : l'IP change au gré des baux DHCP, et `first_seen`
n'est jamais réécrit pour qu'une reconnexion ne relance pas le compte à rebours.
"""

import ipaddress
import logging
from datetime import datetime, timedelta

import database as db
from access_control import enforcement_mode

log = logging.getLogger("protectado.grace")

# Sous-réseau distribué par l'AP enfants (cf. action_runner.KIDS_GW = .1).
KIDS_NET = ipaddress.ip_network("192.168.50.0/24")

KIDS_GW = "192.168.50.1"             # le boîtier lui-même : jamais un « nouvel appareil »

DEFAULT_GRACE_HOURS = 24
_MAX_GRACE_HOURS = 24 * 365          # borne de garde contre une config absurde


def unassigned(devices: list[dict], config: dict) -> list[dict]:
    """Appareils ni rattachés à un profil, ni ignorés par le parent, hors boîtier.

    Règle partagée par le dashboard (carte « nouveaux appareils ») et le monitor
    (échéance du délai de grâce) : un seul endroit à faire évoluer.
    """
    assigned = {d.get("ip") for p in (config.get("profiles") or {}).values()
                for d in (p.get("devices") or [])}
    ignored = set(config.get("ignored_devices") or [])
    return [d for d in devices
            if d.get("ip") and d["ip"] not in assigned
            and d["ip"] not in ignored and d["ip"] != KIDS_GW]


def is_kids_ip(ip: str) -> bool:
    """L'adresse appartient-elle au réseau enfants (donc derrière le boîtier) ?"""
    try:
        return ipaddress.ip_address(ip) in KIDS_NET
    except ValueError:
        return False


def grace_hours(config: dict) -> float:
    """Délai configuré (`network.new_device_grace_hours`), 24 h par défaut.

    Valeur illisible, négative ou absurde ⇒ défaut : mieux vaut un délai correct
    qu'une échéance instantanée déclenchée par une coquille dans config.json.
    """
    raw = (config.get("network") or {}).get("new_device_grace_hours", DEFAULT_GRACE_HOURS)
    try:
        hours = float(raw)
    except (TypeError, ValueError):
        log.warning(f"new_device_grace_hours illisible ({raw!r}) — défaut {DEFAULT_GRACE_HOURS} h")
        return float(DEFAULT_GRACE_HOURS)
    if not (0 < hours <= _MAX_GRACE_HOURS):
        log.warning(f"new_device_grace_hours hors bornes ({hours}) — défaut {DEFAULT_GRACE_HOURS} h")
        return float(DEFAULT_GRACE_HOURS)
    return hours


def is_active(config: dict) -> bool:
    """Le suivi s'applique-t-il ? (mode gateway uniquement)"""
    return enforcement_mode(config) == "gateway"


def annotate(devices: list[dict], config: dict) -> list[dict]:
    """Enregistre la première apparition des appareils enfants et les annote.

    Args:
        devices : appareils NON rattachés et NON ignorés, chacun {ip, mac, hostname}.
                  (le filtrage assigné/ignoré reste à la charge de l'appelant, qui
                  possède déjà cette logique).
        config  : config.json chargée.

    Ajoute à chaque appareil du réseau enfants :
        grace_first_seen, grace_hours_left (0 si échu), grace_expired (bool).
    Les appareils hors réseau enfants sont renvoyés inchangés.
    """
    if not is_active(config):
        return devices
    hours = grace_hours(config)
    limit = timedelta(hours=hours)
    now = datetime.now()
    for d in devices:
        ip = d.get("ip", "")
        if not is_kids_ip(ip):
            continue
        # Sans MAC on ne peut pas suivre l'appareil de façon stable : on se rabat sur
        # l'IP comme clé (le bail de l'AP enfants est stable en pratique) plutôt que
        # de laisser l'appareil hors de tout suivi.
        key = d.get("mac") or f"ip:{ip}"
        first_seen = db.record_device_seen(key, ip, d.get("hostname", ""))
        try:
            started = datetime.fromisoformat(first_seen)
        except ValueError:
            started = now
        remaining = (started + limit) - now
        d["grace_first_seen"] = first_seen
        # Temps ÉCOULÉ depuis la première apparition : c'est ce qu'on affiche au parent.
        # Tant qu'aucun blocage n'est appliqué, décrire l'état constaté (« sur le réseau
        # depuis N h », « en accès libre ») est honnête ; annoncer une échéance ne l'est pas.
        d["grace_hours_seen"] = max(0.0, (now - started).total_seconds() / 3600)
        # Borné au délai configuré : first_seen est écrit après la capture de `now`, donc
        # le reste calculé peut le dépasser de quelques microsecondes — le parent ne doit
        # jamais lire « encore 25 h » pour un délai de 24 h.
        d["grace_hours_left"] = min(hours, max(0.0, remaining.total_seconds() / 3600))
        d["grace_expired"] = remaining.total_seconds() <= 0
    return devices


def newly_expired(devices: list[dict]) -> list[dict]:
    """Appareils échus dont l'échéance n'a pas ENCORE été signalée au parent.

    Le drapeau notified_at évite de reloguer un événement à chaque cycle de 60 s.
    """
    already = {row["mac"]: row["notified_at"] for row in db.get_device_grace()}
    out = []
    for d in devices:
        if not d.get("grace_expired"):
            continue
        key = d.get("mac") or f"ip:{d.get('ip', '')}"
        if not already.get(key):
            out.append(d)
    return out


def mark_notified(device: dict):
    db.mark_grace_notified(device.get("mac") or f"ip:{device.get('ip', '')}")


def forget(ip: str = "", mac: str = ""):
    """Sort l'appareil du suivi (rattaché à un profil, ou ignoré par le parent)."""
    if mac:
        db.clear_device_grace(mac=mac)
    if ip:
        db.clear_device_grace(mac=f"ip:{ip}")
        db.clear_device_grace(ip=ip)
