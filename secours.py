# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Arnaud Ortais
# Dual-licensed: AGPL-3.0 (open source) or Commercial License — see LICENSE and LICENSE-COMMERCIAL.
"""
secours.py — état du mode secours, partagé entre le runner (qui le décide et l'applique,
root) et l'agent (qui l'annonce au journal et sert la page de secours).

Le mode secours s'ouvre quand le boîtier ne joint plus la box depuis assez longtemps
(cf. uplink_watch) : sans lui, le parent n'aurait plus aucun moyen de le joindre.
Pendant le mode secours, personne n'a Internet, et wpa_supplicant continue d'essayer
l'ancienne configuration : si la box revient, le runner en sort tout seul.

DEUX CAS, selon ce que le parent peut encore atteindre :
- KIDS (cas A) : un réseau enfants est diffusé (au moins un profil a sa clé). Il reste en
  place, sans transfert vers la box ; un portail captif y sert la page de secours, que
  seul un appareil qui possède la clé d'un profil peut voir.
- SETUP (cas B) : aucun réseau enfants. Le boîtier repasse en Protectado-Setup en gardant
  sa configuration (configured=false) ; l'assistant voit le marqueur et propose de
  reconnecter ou de repartir de zéro.

Le fichier data/secours.json est écrit par le runner seulement. `seq` numérote les
entrées : l'agent ne journalise chaque entrée et chaque sortie qu'une fois, même s'il
redémarre entre-temps.
"""

import json
import os
import tempfile

STATE_NAME = "secours.json"

KIDS = "kids"
SETUP = "setup"

# Comment on en est sorti (paramètre de l'événement de sortie).
EXIT_AUTO = "auto"            # la box est revenue avec l'ancienne configuration
EXIT_NEW_BOX = "new_box"      # le parent a reconnecté le boîtier à une box


def empty_state() -> dict:
    # reset_at : échéance d'une réinitialisation demandée, sur l'horloge monotone du
    # système (la même pour le runner et l'agent), ou None.
    return {"active": False, "case": "", "cause": "", "seq": 0,
            "offline_at_entry": 0, "exit": None, "reset_at": None}


def load(chemin: str) -> dict:
    etat = empty_state()
    try:
        with open(chemin) as f:
            lu = json.load(f)
        if isinstance(lu, dict):
            etat.update({k: lu[k] for k in etat if k in lu})
    except (OSError, ValueError):
        pass
    return etat


def save(chemin: str, etat: dict) -> None:
    dossier = os.path.dirname(chemin) or "."
    fd, tmp = tempfile.mkstemp(dir=dossier, prefix=".secours.")
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


def choose_case(nb_cles: int, ap_diffuse: bool) -> str:
    """Cas A seulement si le réseau enfants existe ET est diffusé : des clés sans AP
    actif ne donneraient au parent aucun réseau où trouver la page de secours."""
    return KIDS if nb_cles > 0 and ap_diffuse else SETUP


def entered(etat: dict, cas: str, cause: str, offline_sec: int) -> dict:
    return dict(etat, active=True, case=cas, cause=cause, seq=etat["seq"] + 1,
                offline_at_entry=offline_sec, exit=None, reset_at=None)


def exited(etat: dict, action: str, offline_sec: int) -> dict:
    """`offline_sec` : temps hors ligne cumulé au moment de la sortie, donc la durée
    totale de la perte de la box (le Pi n'a pas d'horloge sauvegardée)."""
    return dict(etat, active=False, reset_at=None,
                exit={"seq": etat["seq"], "action": action, "offline_sec": offline_sec})


# ------------------------------------------------------------------ #
#  Essais du mot de passe parent (page de secours, assistant)         #
# ------------------------------------------------------------------ #
#  La page de secours est servie sans session à tout appareil qui a la clé d'un profil :
#  un adolescent a tout le temps d'essayer. Après FREE_ATTEMPTS échecs, chaque échec
#  ferme la porte un temps qui double, jusqu'à LOCK_MAX_SEC. Persisté : un
#  débranchement ne remet pas le compteur à zéro. Le Pi n'ayant pas d'horloge sauvegardée
#  (et pas d'Internet pour la régler pendant le mode secours), le blocage se mesure sur
#  l'horloge monotone ; après un redémarrage, qui la remet à zéro, il repart pour sa
#  durée entière : un redémarrage ne raccourcit jamais un blocage.

ATTEMPTS_NAME = "secours_attempts.json"     # écrit par l'agent
FREE_ATTEMPTS = 5
LOCK_BASE_SEC = 60
LOCK_MAX_SEC = 3600


def boot_id() -> str:
    try:
        with open("/proc/sys/kernel/random/boot_id") as f:
            return f.read().strip()
    except OSError:
        return ""


def _attempts_empty() -> dict:
    return {"failures": 0, "boot_id": "", "lock_until": 0.0, "lock_sec": 0}


def load_attempts(chemin: str) -> dict:
    etat = _attempts_empty()
    try:
        with open(chemin) as f:
            lu = json.load(f)
        if isinstance(lu, dict):
            etat.update({k: lu[k] for k in etat if k in lu})
    except (OSError, ValueError):
        pass
    return etat


def lock_remaining(etat: dict, maintenant: float, boot: str) -> tuple[int, dict]:
    """(secondes de blocage restantes, état à retenir)."""
    if etat["failures"] < FREE_ATTEMPTS or not etat["lock_sec"]:
        return 0, etat
    if etat["boot_id"] != boot:
        etat = dict(etat, boot_id=boot, lock_until=maintenant + etat["lock_sec"])
    return max(0, int(etat["lock_until"] - maintenant + 0.999)), etat


def failed(etat: dict, maintenant: float, boot: str) -> dict:
    n = etat["failures"] + 1
    if n < FREE_ATTEMPTS:
        return dict(etat, failures=n)
    duree = min(LOCK_MAX_SEC, LOCK_BASE_SEC * 2 ** (n - FREE_ATTEMPTS))
    return {"failures": n, "boot_id": boot, "lock_until": maintenant + duree,
            "lock_sec": duree}


def succeeded() -> dict:
    return _attempts_empty()


def save_attempts(chemin: str, etat: dict) -> None:
    save(chemin, etat)
