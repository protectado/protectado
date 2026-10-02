# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Arnaud Ortais
# Dual-licensed: AGPL-3.0 (open source) or Commercial License — see LICENSE and LICENSE-COMMERCIAL.
"""
system_maintenance.py — l'état système d'un boîtier déjà livré, tenu par le runner (root).

Une mise à jour ne fait qu'aligner le dépôt git, puis redémarre le runner. Tout ce qui
vit HORS du dépôt (l'updater installé dans /usr/local/sbin, la propriété des fichiers)
n'atteignait donc jamais un boîtier existant. Le runner, qui redémarre à chaque mise à
jour et tourne en root, l'applique ici à son démarrage.

PROPRIÉTÉ DU CODE. Le runner exécute en root le code de l'installation. Si ce code
appartient à l'utilisateur du service, tout processus de ce compte hors sandbox (le
rapport du soir lancé par cron, une session SSH) peut devenir root en le modifiant. Le
code appartient donc à root, sans droit d'écriture pour le groupe ni les autres ; seul
data/ reste à l'utilisateur du service, qui y écrit sa configuration et sa base.

MIGRATIONS. Ce qu'une version a besoin de changer sur le système (un paquet, un fichier de
/etc) s'écrit en migration numérotée dans bootstrap/migrations/ (procédure : README de ce
dossier). Le runner les applique dans l'ordre, une seule fois chacune, en tâche de fond
pour ne jamais retarder la protection. Elles ne viennent QUE du code de l'installation,
jamais de data/ ni de la file d'actions : l'agent ne peut ni en écrire ni en déclencher.
Et un script qui n'appartiendrait pas à root, ou que le groupe ou les autres pourraient
modifier, est refusé : exécuté en root, il serait une porte vers root.
"""

import json
import logging
import os
import pwd
import re
import subprocess
import tempfile
import time
from datetime import datetime

log = logging.getLogger("protectado.runner")

INSTALL_DIR = os.path.dirname(os.path.abspath(__file__))
UPDATER_PATH = "/usr/local/sbin/protectado-update"
UPDATER_TEMPLATE = os.path.join(INSTALL_DIR, "bootstrap", "protectado-update.sh")
DATA_NAME = "data"

MIGRATIONS_DIR = os.path.join(INSTALL_DIR, "bootstrap", "migrations")
MIGRATION_RE = r"^\d{4}-[a-z0-9-]+\.sh$"
DONE_FILE = "/var/lib/protectado/migrations.done"      # hors du dépôt : survit aux mises à jour
STATUS_FILE = os.path.join(INSTALL_DIR, DATA_NAME, "maintenance.json")   # lu par le tableau de bord
MIGRATION_TIMEOUT = 15 * 60
MIGRATION_RETRY_SEC = 3600
UPDATE_UNIT = "protectado-update.service"
UPDATE_WAIT_MAX = 30 * 60         # au-delà, l'updater est bloqué : le code repasse à root
UPDATE_POLL_SEC = 5


def service_user() -> str:
    """Utilisateur du service : celui de l'unité de l'agent, sinon le propriétaire de
    data/. Vide si on ne le sait pas : on ne touche alors à rien plutôt que de deviner."""
    r = subprocess.run(["systemctl", "show", "-p", "User", "--value", "protectado-agent"],
                       capture_output=True, text=True)
    nom = (r.stdout or "").strip()
    if nom and nom != "root":
        return nom
    try:
        uid = os.stat(os.path.join(INSTALL_DIR, DATA_NAME)).st_uid
        return pwd.getpwuid(uid).pw_name if uid != 0 else ""
    except (OSError, KeyError):
        return ""


def sync_updater(user: str, path: str = UPDATER_PATH, template: str = UPDATER_TEMPLATE) -> bool:
    """Réinstalle l'updater depuis le modèle du dépôt s'il en diffère. True si réécrit.

    L'updater installé ne se met pas à jour lui-même : sans cette resynchronisation, une
    correction de l'updater n'atteindrait jamais un boîtier déjà livré. Écriture atomique,
    root:root 0755 avant le renommage.
    """
    with open(template) as f:
        attendu = f.read().replace("__USER__", user)
    try:
        with open(path) as f:
            if f.read() == attendu:
                return False
    except OSError:
        pass
    dossier = os.path.dirname(path)
    os.makedirs(dossier, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dossier, prefix=".protectado-update.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(attendu)
        os.chown(tmp, 0, 0)
        os.chmod(tmp, 0o755)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    log.warning(f"updater réinstallé depuis le dépôt ({path})")
    return True


def _securise(chemin: str, uid: int) -> bool:
    """Donne `chemin` à `uid` (lien symbolique compris, sans le suivre) et retire
    l'écriture au groupe et aux autres pour le code. True si quelque chose a changé."""
    st = os.lstat(chemin)
    change = False
    if st.st_uid != uid or (uid == 0 and st.st_gid != 0):
        os.lchown(chemin, uid, 0 if uid == 0 else -1)
        change = True
    if uid == 0 and not os.path.islink(chemin) and st.st_mode & 0o022:
        os.chmod(chemin, st.st_mode & ~0o022)
        change = True
    return change


def secure_code(install_dir: str, service_uid: int) -> bool:
    """Code à root sans écriture groupe/autres ; data/ à l'utilisateur du service.

    Parcourt toute l'installation sans jamais suivre un lien symbolique (le venv pointe
    vers le Python du système, qui ne nous appartient pas). True si quelque chose a été
    corrigé. Idempotent : sur un boîtier déjà conforme, rien n'est modifié.
    """
    change = False
    data = os.path.join(install_dir, DATA_NAME)
    for dossier, sous, fichiers in os.walk(install_dir, followlinks=False):
        dans_data = dossier == data or dossier.startswith(data + os.sep)
        uid = service_uid if dans_data else 0
        change |= _securise(dossier, uid)
        for nom in fichiers + [s for s in sous if os.path.islink(os.path.join(dossier, s))]:
            change |= _securise(os.path.join(dossier, nom), uid)
    return change


def update_running() -> bool:
    """Une mise à jour est en cours (protectado-update.service).

    L'unité est un oneshot : pendant son exécution, « systemctl is-active » répond
    « activating » avec un code de sortie non nul (mesuré : 3). Le code de sortie seul ne
    verrait donc jamais une mise à jour en cours ; c'est l'état affiché qui compte.
    """
    r = subprocess.run(["systemctl", "is-active", UPDATE_UNIT], capture_output=True, text=True)
    return (r.stdout or "").strip() in ("active", "activating", "deactivating", "reloading")


def _secure(user: str, uid: int) -> None:
    if secure_code(INSTALL_DIR, uid):
        log.warning(f"propriété du code rétablie : root pour le code, {user} pour data/")


def startup() -> tuple[str, int] | None:
    """Au démarrage du runner : updater d'abord, propriété du code ensuite, migrations
    enfin (dans le fil de fond, cf. migrations_loop).

    1. L'updater est resynchronisé tout de suite : le remplacement est atomique, un
       updater en cours d'exécution garde l'ancien fichier ouvert et n'est pas gêné.
    2. La propriété du code passe à root, SAUF si une mise à jour est en cours : c'est
       elle qui vient de redémarrer le runner, et son contrôle de santé peut encore
       décider un rollback. Un updater antérieur à la propriété root fait ce rollback
       avec git sous l'utilisateur du service, qui refuse un dépôt à root (« dubious
       ownership ») : le boîtier resterait sur la version qui a échoué. Le changement
       est alors reporté au fil de fond, une fois la mise à jour terminée ; la fonction
       renvoie (utilisateur, uid) pour qu'il le fasse.
    3. Les migrations passent toujours APRÈS la propriété root : un script qui
       n'appartient pas à root est refusé (_sure), elles échoueraient avant.
    """
    user = service_user()
    if not user:
        log.error("utilisateur du service introuvable : updater et propriété du code non vérifiés")
        return None
    try:
        uid = pwd.getpwnam(user).pw_uid
    except KeyError:
        log.error(f"utilisateur du service « {user} » inconnu du système")
        return None
    sync_updater(user)
    if update_running():
        log.warning("mise à jour en cours : propriété du code reportée à la fin de la mise à jour")
        return user, uid
    _secure(user, uid)
    return None


# ------------------------------------------------------------------ #
#  Migrations                                                         #
# ------------------------------------------------------------------ #

def list_migrations(dossier: str = MIGRATIONS_DIR) -> list[str]:
    """Noms des migrations, dans l'ordre de leur numéro."""
    try:
        return sorted(n for n in os.listdir(dossier) if re.match(MIGRATION_RE, n))
    except OSError:
        return []


def _done(chemin: str) -> list[str]:
    try:
        with open(chemin) as f:
            return [l.strip() for l in f if l.strip()]
    except OSError:
        return []


def _sure(chemin: str, racine: str) -> bool:
    """Le fichier et chacun de ses dossiers jusqu'à `racine` appartiennent à root et ne
    sont modifiables ni par le groupe ni par les autres. Un lien symbolique est refusé."""
    courant = os.path.abspath(chemin)
    racine = os.path.abspath(racine)
    while True:
        st = os.lstat(courant)
        if os.path.islink(courant) or st.st_uid != 0 or st.st_mode & 0o022:
            return False
        if courant == racine or courant == os.path.dirname(courant):
            return courant == racine
        courant = os.path.dirname(courant)


def _ecrire_statut(chemin: str, etat: str, detail: str, en_attente: list[str]):
    try:
        with open(chemin, "w") as f:
            json.dump({"state": etat, "detail": detail, "pending": en_attente,
                       "updated_at": datetime.now().isoformat()}, f)
        os.chmod(chemin, 0o644)
    except OSError as e:
        log.error(f"état des migrations non publié ({chemin}) : {e}")


def run_pending(dossier: str = MIGRATIONS_DIR, done_path: str = DONE_FILE,
                status_path: str = STATUS_FILE, racine: str = INSTALL_DIR) -> bool:
    """Applique les migrations pas encore faites, dans l'ordre. True si tout est fait.

    Un échec (ou un script non sûr) arrête la série : les suivantes peuvent dépendre de
    celle-ci. L'état est publié pour le tableau de bord, avec la cause et ce qui reste.
    """
    faites = _done(done_path)
    en_attente = [n for n in list_migrations(dossier) if n not in faites]
    for nom in list(en_attente):
        script = os.path.join(dossier, nom)
        if not _sure(script, racine):
            detail = (f"{nom} : migration non sûre (doit appartenir à root, sans écriture "
                      f"pour le groupe ni les autres), non exécutée")
            log.critical(detail)
            _ecrire_statut(status_path, "error", detail, en_attente)
            return False
        log.info(f"migration {nom}…")
        env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "INSTALL_DIR": INSTALL_DIR,
               "LANG": "C.UTF-8"}
        try:
            r = subprocess.run(["bash", script], capture_output=True, text=True,
                               timeout=MIGRATION_TIMEOUT, env=env, cwd=racine)
            sortie, code = ((r.stdout or "") + (r.stderr or "")).strip(), r.returncode
        except subprocess.TimeoutExpired:
            sortie, code = f"délai de {MIGRATION_TIMEOUT // 60} min dépassé", -1
        if code != 0:
            detail = f"{nom} en échec : {sortie[-300:]}"
            log.error(detail)
            _ecrire_statut(status_path, "error", detail, en_attente)
            return False
        os.makedirs(os.path.dirname(done_path), exist_ok=True)
        with open(done_path, "a") as f:
            f.write(nom + "\n")
            f.flush()
            os.fsync(f.fileno())
        en_attente.remove(nom)
        log.info(f"migration {nom} appliquée")
    _ecrire_statut(status_path, "ok", "", [])
    return True


def wait_update_done(maximum: int = UPDATE_WAIT_MAX) -> bool:
    """Attend la fin de la mise à jour en cours, au plus `maximum` secondes.
    False si elle tourne encore au bout du délai."""
    debut = time.monotonic()
    while update_running():
        if time.monotonic() - debut >= maximum:
            return False
        time.sleep(UPDATE_POLL_SEC)
    return True


def migrations_loop(deferred: tuple[str, int] | None = None) -> None:
    """Fil de fond du runner : rétablit d'abord la propriété du code si startup() l'a
    reportée, puis applique les migrations, et retente toutes les heures tant qu'il en
    reste (réseau absent, verrou apt…)."""
    if deferred:
        if wait_update_done():
            log.info("mise à jour terminée : propriété du code rétablie")
        else:
            log.error(f"mise à jour toujours en cours après {UPDATE_WAIT_MAX // 60} min : "
                      f"propriété du code rétablie quand même")
        try:
            _secure(*deferred)
        except Exception as e:
            log.error(f"propriété du code non rétablie : {e}")
    while True:
        try:
            if run_pending():
                return
        except Exception as e:
            log.error(f"migrations : erreur inattendue : {e}")
        time.sleep(MIGRATION_RETRY_SEC)
