# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Arnaud Ortais
# Dual-licensed: AGPL-3.0 (open source) or Commercial License — see LICENSE and LICENSE-COMMERCIAL.
"""
access_control.py — Point de passage UNIQUE des transitions de droit d'accès d'un appareil.

Toute transition du droit d'accès d'un appareil (assignation à un profil, changement
de mode/slot, retour d'override, mode adulte, retour au profil enfant, désassignation)
DOIT passer par apply_device_access(). Objectif : un seul endroit applique l'état
d'accès d'un appareil, de façon cohérente entre les deux couches :

  (a) DNS / Pi-hole : place les IP dans le bon groupe Pi-hole (comportement historique).
  (b) [mode "gateway" UNIQUEMENT] émet l'action root "apply_forward" via la file :
      mode "blocked" → DROP, sinon → ACCEPT, pour les IP concernées.

En mode "dns_only" (défaut, et champ config.network.enforcement absent), SEULE la
partie (a) s'exécute : le comportement est strictement identique à l'historique.

Le champ config.network.enforcement n'est lu QUE par ce module (côté sandbox) et par
action_runner (root). La logique métier — scheduler, monitor, agent, DB, dashboard —
ne le teste jamais : elle appelle simplement apply_device_access() et ignore le mécanisme.

NB : ce module n'exécute JAMAIS iptables lui-même (la sandbox n'a pas NET_ADMIN). En
gateway il se contente d'ÉMETTRE une action que le runner root exécutera.
"""

import json
import logging
import os
from datetime import datetime

from paths import ACTION_QUEUE_DIR, CONFIG_PATH

log = logging.getLogger("protectado.access")

# Modes d'accès qui laissent PASSER le trafic. Tout autre valeur (y compris "blocked",
# une chaîne vide ou un mode inconnu) est traitée comme bloquée : fail-safe = on ferme.
_OPEN_MODES = {"work", "permissive"}


def _read_config() -> dict:
    """Config sur disque, ou {} si illisible (⇒ repli dns_only, donc aucune action émise)."""
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except Exception as e:
        log.warning(f"config illisible ({e}) — repli dns_only, FORWARD non touché")
        return {}


def enforcement_mode(cfg: dict | None = None) -> str:
    """Mode DÉCLARÉ dans config.network.enforcement : 'dns_only' (défaut) | 'gateway'.

    Côté sandbox, ce champ n'est lu QUE via cette fonction (action_runner le relit de son
    côté, en root) : un seul endroit à auditer. Champ absent, valeur inconnue ou config
    illisible ⇒ 'dns_only', le comportement historique DNS-only.

    cfg : config déjà chargée (évite une relecture disque) ; None ⇒ lue ici.
    """
    if cfg is None:
        cfg = _read_config()
    mode = (cfg.get("network") or {}).get("enforcement", "dns_only")
    return mode if mode in ("dns_only", "gateway") else "dns_only"


def _is_monitoring(cfg: dict, profile: str) -> bool:
    """Le profil est-il en surveillance PASSIVE ? Un tel appareil (typiquement celui d'un
    parent) ne doit JAMAIS être coupé — cf. la même règle côté monitor/agent/pihole_api.

    Garde nécessaire ici parce que scheduler.get_current_slot() retombe sur "blocked"
    quand aucune plage ne correspond, et un profil monitoring n'a pas de planning : les
    appelants qui passent slot["mode"] tel quel demanderaient donc un DROP.
    """
    if not profile:
        return False
    return (cfg.get("profiles") or {}).get(profile, {}).get("mode") == "monitoring"


def _queue_forward_action(ips, *, blocked: bool, profile: str, reason: str) -> bool:
    """Dépose une action root 'apply_forward' dans la file. True si l'écriture a réussi.

    La sandbox n'a pas NET_ADMIN : elle ne peut QUE demander. C'est action_runner (root)
    qui pose/retire le DROP, et lui seul.
    """
    try:
        os.makedirs(ACTION_QUEUE_DIR, mode=0o700, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d%H%M%S%f")
        path = os.path.join(ACTION_QUEUE_DIR, f"action-{ts}.json")
        payload = {
            "action": "apply_forward",
            "args": {"ips": list(ips), "blocked": blocked,
                     "profile": profile, "reason": reason},
            "queued_at": datetime.now().isoformat(),
        }
        with open(path, "w") as f:
            json.dump(payload, f)
        return True
    except OSError as e:
        log.error(f"file d'actions inaccessible — FORWARD non appliqué pour {list(ips)} "
                  f"[{profile or '-'} {reason or '-'}] : {e}")
        return False


def apply_device_access(pihole, ips, *, mode, group, profile="", reason=""):
    """
    Applique l'état d'accès de `ips` de façon cohérente.

    Args:
        pihole  : instance PiHoleAPI (fournie par l'appelant — la sandbox joint Pi-hole
                  sur localhost). Aucune session supplémentaire n'est créée ici.
        ips     : liste d'adresses IP concernées par la transition.
        mode    : mode d'accès EFFECTIF ("blocked" | "work" | "permissive"). Sert à
                  décider le FORWARD en gateway : "blocked" → DROP, sinon → ACCEPT.
                  Pour un appareil désassigné / en mode adulte (non filtré), passer un
                  mode non-"blocked" (p. ex. "permissive") : accès autorisé → ACCEPT.
        group   : nom du groupe Pi-hole cible (ex. "alice-work", "adult-override",
                  "Default").
        profile : clé de profil, pour le log/diagnostic (facultatif).
        reason  : libellé de la transition, pour le log/diagnostic (facultatif).

    Returns:
        bool : True si toutes les assignations Pi-hole ont réussi ET, en gateway, si
               l'action FORWARD a bien été mise en file.

    En "dns_only", seule la mutation Pi-hole ci-dessous est effectuée — exactement
    l'appel assign_client_to_group() historique, sans changement de sémantique.
    """
    ips = [ip for ip in (ips or []) if ip]

    # (a) Mutation DNS / Pi-hole — comportement historique, actif dans TOUS les modes.
    all_ok = True
    for ip in ips:
        try:
            ok = pihole.assign_client_to_group(ip, group)
        except Exception as e:
            log.warning(f"assign_client_to_group({ip}, {group}) a échoué "
                        f"[{profile or '-'} {reason or '-'}] : {e}")
            ok = False
        all_ok = all_ok and bool(ok)

    # (b) Effecteur iptables FORWARD — mode "gateway" UNIQUEMENT.
    #     En dns_only : aucune action émise, la file reste vide comme avant.
    #     Émis INDÉPENDAMMENT du succès de (a) : le droit d'accès réel ne doit pas
    #     dépendre du DNS (un échec Pi-hole ne doit pas laisser un appareil bloqué
    #     avec un FORWARD ouvert, ni l'inverse).
    cfg = _read_config() if ips else {}
    if ips and enforcement_mode(cfg) == "gateway":
        # Fail-safe : seuls "work"/"permissive" ouvrent le FORWARD. Un mode inconnu,
        # vide ou inattendu ⇒ DROP (on ferme au lieu d'ouvrir par accident).
        blocked = mode not in _OPEN_MODES
        if blocked and mode != "blocked":
            log.warning(f"mode inattendu {mode!r} — FORWARD fermé par sécurité "
                        f"[{profile or '-'} {reason or '-'}]")
        # Exception à ce fail-safe : la surveillance passive n'est PAS un blocage.
        if blocked and _is_monitoring(cfg, profile):
            log.info(f"profil monitoring {profile!r} — FORWARD laissé ouvert "
                     f"(mode {mode!r} ignoré, surveillance passive)")
            blocked = False
        queued = _queue_forward_action(ips, blocked=blocked,
                                       profile=profile, reason=reason)
        # L'échec d'émission est un échec de la transition : l'appelant doit le voir.
        all_ok = all_ok and queued

    return all_ok
