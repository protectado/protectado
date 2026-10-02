# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Arnaud Ortais
# Dual-licensed: AGPL-3.0 (open source) or Commercial License — see LICENSE and LICENSE-COMMERCIAL.
"""
factory_reset.py — retour à l'état neuf du boîtier (sortie d'usine), root seulement.

SEULE fonction de réinitialisation : appelée par `protectado-boot.sh reset --full` et par
le mode secours (runner). Elle efface tout ce qui appartient à la famille, et seulement
cela : l'installation (code, venv, migrations faites, version, état des mises à jour)
reste en place, l'assistant repart d'un boîtier neuf au redémarrage suivant.

L'historique de navigation vit à TROIS endroits, et une réinitialisation qui n'en efface
qu'un laisse « plus aucun historique » faux : la base de Protectado (dont la table des
domaines, construite à partir des sites visités), la base et les journaux de Pi-hole, et
les journaux du service (rapport du soir, journal systemd).

Une étape en échec n'arrête pas les suivantes : mieux vaut effacer tout ce qui peut
l'être et dire ce qui a résisté que s'arrêter à mi-chemin avec des clés en clair.
"""

import glob
import os
import shutil
import sqlite3
import subprocess
import sys

INSTALL_DIR = os.path.dirname(os.path.abspath(__file__))

# Dans data/ : configuration (profils, clés Wi-Fi des profils, mot de passe parent, clé
# OpenRouter, clé de la box), historique, et ce que le boîtier sait des appareils.
# Conservés : version.json, branch, maintenance.json, selfheal.state, update.*,
# services.local.json (appoint du mainteneur), pairing_code (secret de l'INSTALLATION :
# sans lui, l'assistant en DNS seul s'ouvrirait à tout le réseau local).
DATA_FILES = (
    "config.json", "config.json.tmp", "pending_config.json", "wifi_scan.json",
    "box_validation.json", "gateway_status.json",
    "protectado.db", "protectado.db-wal", "protectado.db-shm", "protectado.db-journal",
    "station_identity.json", "arp_scan.json", "bypass_counters.json", "flow_stats.json",
    "posture.json", "uplink_state.json", "secours.json",
)

# Hors de data/ : clés Wi-Fi des profils en clair, nom du réseau enfants (tiré du nom de
# la box), baux DHCP avec le nom d'hôte des appareils des enfants, clé de la box.
SYSTEM_FILES = (
    "/etc/protectado/hostapd-psk",
    "/etc/protectado/hostapd-ap.conf",
    "/var/lib/misc/protectado-ap.leases",
    "/etc/netplan/60-protectado-uplink.yaml",
)

# Copies de config.json et de la base laissées par l'updater (rollback échoué ou mise à
# jour interrompue) : elles survivraient à tout le reste.
BACKUP_GLOB = "/opt/protectado-bk-*"

# Vidés et non supprimés : le runner tient son journal ouvert, un fichier supprimé
# continuerait de recevoir ses lignes jusqu'au redémarrage.
LOGS_TRUNCATE = ("/var/log/protectado-report.log", "/var/log/protectado-runner.log")

# Journaux tournés de Pi-hole : « pihole flush » vide les journaux courants et la base
# des requêtes, mais pas ces archives (mesuré sur Pi-hole v6.4.3).
PIHOLE_ROTATED_GLOB = "/var/log/pihole/*.log.*"
GRAVITY_DB = "/etc/pihole/gravity.db"

COMMANDS_BEFORE = (
    # L'agent écrit dans la base et la configuration : il ne doit plus rien écrire
    # pendant qu'on efface. Le redémarrage qui suit la réinitialisation le relance.
    ["systemctl", "stop", "protectado-agent"],
)
COMMANDS_AFTER = (
    ["pihole", "reloadlists"],
    ["pihole", "flush"],                 # journaux courants et base des requêtes de FTL
    ["pihole", "arpflush"],              # tables réseau de FTL : MAC et noms d'hôte
    ["netplan", "generate"],
    ["journalctl", "--rotate"],
    ["journalctl", "--vacuum-time=1s"],  # le journal systemd garde IP, profils, domaines
)


def _sous(racine: str, chemin: str) -> str:
    return os.path.join(racine, chemin.lstrip("/"))


def _effacer(chemin: str, effaces: list, erreurs: list):
    try:
        os.remove(chemin)
        effaces.append(chemin)
    except FileNotFoundError:
        pass
    except OSError as e:
        erreurs.append(f"{chemin} : {e}")


def _supprimer_arbre(chemin: str, effaces: list, erreurs: list):
    try:
        shutil.rmtree(chemin)
        effaces.append(chemin)
    except FileNotFoundError:
        pass
    except OSError as e:
        erreurs.append(f"{chemin} : {e}")


def clean_gravity(chemin: str) -> int:
    """Retire de la base de Pi-hole ce que Protectado y a créé. Nombre de lignes retirées.

    Groupes : description « Protectado… » (create_group pose toujours ce préfixe) ; ils
    portent le nom des profils. Clients : ceux de ces groupes, et ceux dont le
    commentaire commence par « Protectado » ; un client renommé par le parent porte le
    nom de l'appareil, d'où le critère d'appartenance. Domaines : commentaire
    « protectado:… ». Le groupe 0 (Default) de Pi-hole n'est jamais touché.
    """
    if not os.path.exists(chemin):
        return 0
    with sqlite3.connect(chemin) as conn:
        groupes = [r[0] for r in conn.execute(
            'SELECT id FROM "group" WHERE id != 0 AND description LIKE \'Protectado%\'')]
        marques = ",".join("?" * len(groupes)) or "NULL"
        clients = {r[0] for r in conn.execute(
            f"SELECT client_id FROM client_by_group WHERE group_id IN ({marques})", groupes)}
        clients |= {r[0] for r in conn.execute(
            "SELECT id FROM client WHERE comment LIKE 'Protectado%'")}
        domaines = [r[0] for r in conn.execute(
            "SELECT id FROM domainlist WHERE comment LIKE 'protectado:%'")]
        n = 0
        for table, colonne, ids in (("client_by_group", "client_id", clients),
                                    ("client", "id", clients),
                                    ("domainlist_by_group", "domainlist_id", domaines),
                                    ("domainlist", "id", domaines),
                                    ("client_by_group", "group_id", groupes),
                                    ("domainlist_by_group", "group_id", groupes),
                                    ('"group"', "id", groupes)):
            ids = list(ids)
            if ids:
                n += conn.execute(f"DELETE FROM {table} WHERE {colonne} IN "
                                  f"({','.join('?' * len(ids))})", ids).rowcount
    return n


def _lancer(cmd: list, erreurs: list):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            erreurs.append(f"{' '.join(cmd)} : code {r.returncode} "
                           f"{(r.stderr or r.stdout or '').strip()[:200]}")
    except (OSError, subprocess.TimeoutExpired) as e:
        erreurs.append(f"{' '.join(cmd)} : {e}")


def run(racine: str = "/", install_dir: str = INSTALL_DIR, commandes: bool = True) -> dict:
    """Efface tout ce qui est « à effacer » (cf. tables ci-dessus). Renvoie
    {"effaces": [...], "erreurs": [...]}. `racine` et `commandes` servent aux tests :
    tous les chemins système sont pris sous `racine`, et aucune commande n'est lancée."""
    effaces, erreurs = [], []
    if commandes:
        for cmd in COMMANDS_BEFORE:
            _lancer(cmd, erreurs)
    try:
        n = clean_gravity(_sous(racine, GRAVITY_DB))
        if n:
            effaces.append(f"{GRAVITY_DB} ({n} lignes)")
    except sqlite3.Error as e:
        erreurs.append(f"{GRAVITY_DB} : {e}")
    data = os.path.join(_sous(racine, install_dir), "data")
    for nom in DATA_FILES:
        _effacer(os.path.join(data, nom), effaces, erreurs)
    for chemin in SYSTEM_FILES:
        _effacer(_sous(racine, chemin), effaces, erreurs)
    for chemin in sorted(glob.glob(_sous(racine, PIHOLE_ROTATED_GLOB))):
        _effacer(chemin, effaces, erreurs)
    for chemin in sorted(glob.glob(_sous(racine, BACKUP_GLOB))):
        _supprimer_arbre(chemin, effaces, erreurs)
    for chemin in LOGS_TRUNCATE:
        chemin = _sous(racine, chemin)
        if os.path.exists(chemin):
            try:
                open(chemin, "w").close()
                effaces.append(chemin)
            except OSError as e:
                erreurs.append(f"{chemin} : {e}")
    if commandes:
        for cmd in COMMANDS_AFTER:
            _lancer(cmd, erreurs)
    return {"effaces": effaces, "erreurs": erreurs}


if __name__ == "__main__":
    resultat = run()
    for chemin in resultat["effaces"]:
        print(f"effacé : {chemin}")
    for erreur in resultat["erreurs"]:
        print(f"ÉCHEC  : {erreur}", file=sys.stderr)
    # 2 et non 1 pour une réinitialisation partielle : 1 est le code d'un Python qui
    # plante, et protectado-boot.sh ne se rabat sur son effacement minimal que dans ce cas.
    sys.exit(2 if resultat["erreurs"] else 0)
