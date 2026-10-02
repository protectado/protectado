# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Arnaud Ortais
# Dual-licensed: AGPL-3.0 (open source) or Commercial License — see LICENSE and LICENSE-COMMERCIAL.
"""
uplink_watch.py — le boîtier joint-il encore la box ? Tenu par le runner (root), en
posture passerelle.

POURQUOI. Si la connexion au Wi-Fi de la box devient impossible (clé changée, box
remplacée, déménagement), le boîtier redémarre toujours en passerelle avec l'ancienne
clé et le parent n'a plus aucun moyen de le joindre. Ce module mesure la perte et dit
quand basculer en mode secours. Le déclencheur est l'échec de connexion à la box, jamais
l'absence d'Internet : une panne de l'opérateur ne déclenche rien.

DEUX CAUSES, DEUX DÉLAIS (décision du mainteneur) :
- box VISIBLE mais connexion refusée (clé changée) : mode secours après 15 min ;
- box INTROUVABLE (Wi-Fi coupé la nuit, vacances, box renommée) : après 24 h.

COMMENT ON LES DISTINGUE. Pas par les codes d'échec de wpa_supplicant : mesuré sur une
box WPA2/WPA3, une mauvaise clé donne « reason=CONN_FAILED » et non « WRONG_KEY », qui
n'existe qu'en WPA2. On lit deux états par wpa_cli : connecté (wpa_state=COMPLETED) ou
non, et la présence du nom de la box dans scan_results, le cache des scans que
wpa_supplicant fait déjà seul quand il n'est pas connecté (aucun scan n'est lancé ici,
rien ne perturbe une connexion qui marche). Mesures du 2026-10-02 : clé refusée →
SCANNING et box listée ; box renommée → SCANNING et box absente du cache 21 s après.

TEMPS COMPTÉ SOUS TENSION. Le Pi n'a pas d'horloge sauvegardée : pas d'horodatage
absolu. Les compteurs sont des durées cumulées, mesurées par l'horloge monotone entre
deux relevés et écrites sur disque à chaque relevé hors ligne : un débranchement ne les
remet pas à zéro, le temps hors tension ne compte pas. Ils repartent de zéro à la
première connexion réussie. Le temps « refusé » ne compte que tant que la box est
visible, et repart de zéro quand elle disparaît : une box éteinte quelques minutes ne
doit pas garder en réserve du temps « clé refusée ».
"""

import json
import os
import subprocess
import tempfile

IFACE = "wlan_up"
STATE_NAME = "uplink_state.json"      # dans DATA_DIR, lu par le tableau de bord

AUTH_REFUSED_DELAY = 15 * 60
NOT_FOUND_DELAY = 24 * 3600
# Réservées aux tests et à la validation sur boîtier (cf. tests/e2e/SECOURS.md) : un
# boîtier de famille ne les a jamais.
ENV_AUTH = "PROTECTADO_SECOURS_AUTH_SEC"
ENV_ABSENT = "PROTECTADO_SECOURS_ABSENT_SEC"

TICK_SEC = 30
# Un écart plus long entre deux relevés (runner suspendu, horloge déréglée) n'est compté
# que pour cette durée : on ne compte que ce qu'on a vu.
MAX_STEP_SEC = 2 * TICK_SEC

AUTH_REFUSED = "auth_refused"
NOT_FOUND = "not_found"


def delays() -> tuple[int, int]:
    """(délai clé refusée, délai box introuvable), en secondes."""
    def lire(nom, defaut):
        try:
            valeur = int(os.environ.get(nom, ""))
            return valeur if valeur > 0 else defaut
        except ValueError:
            return defaut
    return lire(ENV_AUTH, AUTH_REFUSED_DELAY), lire(ENV_ABSENT, NOT_FOUND_DELAY)


def empty_state() -> dict:
    return {"connected": True, "box_visible": True, "cause": "",
            "offline_sec": 0, "refused_sec": 0}


def load(chemin: str) -> dict:
    etat = empty_state()
    try:
        with open(chemin) as f:
            lu = json.load(f)
        for cle in etat:
            if cle in lu and isinstance(lu[cle], type(etat[cle])):
                etat[cle] = lu[cle]
    except (OSError, ValueError):
        pass
    return etat


def save(chemin: str, etat: dict) -> None:
    """Écriture atomique : un débranchement pendant l'écriture laisse l'ancien relevé."""
    dossier = os.path.dirname(chemin) or "."
    fd, tmp = tempfile.mkstemp(dir=dossier, prefix=".uplink_state.")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(etat, f)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, chemin)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def step(etat: dict, connected: bool, visible: bool, ecoule: float) -> dict:
    """Nouvel état après un relevé, `ecoule` secondes après le précédent."""
    if connected:
        return empty_state()
    ecoule = int(max(0, min(ecoule, MAX_STEP_SEC)))
    nouveau = dict(etat, connected=False, box_visible=visible,
                   offline_sec=etat["offline_sec"] + ecoule)
    if visible:
        nouveau.update(cause=AUTH_REFUSED, refused_sec=etat["refused_sec"] + ecoule)
    else:
        nouveau.update(cause=NOT_FOUND, refused_sec=0)
    return nouveau


def due(etat: dict) -> str:
    """Cause du basculement en mode secours si un délai est atteint, sinon ""."""
    auth, absent = delays()
    if etat["connected"]:
        return ""
    if etat["refused_sec"] >= auth:
        return AUTH_REFUSED
    if etat["offline_sec"] >= absent:
        return NOT_FOUND
    return ""


def parse_status(texte: str) -> bool:
    """Sortie de « wpa_cli status » → connecté ?"""
    return any(l.strip() == "wpa_state=COMPLETED" for l in texte.splitlines())


def parse_visible(texte: str, ssid: str) -> bool:
    """Sortie de « wpa_cli scan_results » → la box est-elle listée ?

    Égalité EXACTE du nom : le réseau enfants s'appelle « <box>-Protectado » et figure
    dans les mêmes résultats (mesuré) ; un « commence par » prendrait le boîtier pour la
    box. Une box à nom masqué n'est pas reconnaissable : elle retombe sur le délai long.
    """
    if not ssid:
        return False
    for ligne in texte.splitlines()[1:]:
        champs = ligne.split("\t")
        if len(champs) >= 5 and champs[4] == ssid:
            return True
    return False


def read_wpa(ssid: str, iface: str = IFACE) -> tuple[bool, bool] | None:
    """(connecté, box visible), ou None si wpa_supplicant ne répond pas. Ne déclenche
    aucun scan."""
    try:
        statut = subprocess.run(["wpa_cli", "-i", iface, "status"],
                                capture_output=True, text=True, timeout=10)
        if statut.returncode != 0:
            return None
        connecte = parse_status(statut.stdout)
        if connecte:
            return True, True
        scan = subprocess.run(["wpa_cli", "-i", iface, "scan_results"],
                              capture_output=True, text=True, timeout=10)
        if scan.returncode != 0:
            return None
        return False, parse_visible(scan.stdout, ssid)
    except (OSError, subprocess.TimeoutExpired):
        return None
