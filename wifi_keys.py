# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Arnaud Ortais
# Dual-licensed: AGPL-3.0 (open source) or Commercial License — see LICENSE and LICENSE-COMMERCIAL.
"""
wifi_keys.py — une clé Wi-Fi par profil enfant, et le fichier de clés servi à hostapd.

POURQUOI. Le filtrage doit savoir DE QUI vient le trafic. Jusqu'ici il le déduisait de
l'adresse IP, donc de l'adresse MAC, donc d'un identifiant que les téléphones changent
(adresse privée par réseau, réseau oublié puis rejoint, réglage de confidentialité
basculé). Une MAC neuve était un hôte inconnu, rattaché à aucun profil, et un hôte
inconnu n'était filtré par rien : ni les listes de son profil, ni son planning. Le
contournement ne demandait aucune compétence.

L'identité vient désormais du SECRET utilisé à l'association. Un seul SSID, une clé par
profil : hostapd lit ce fichier, essaie les clés pendant la poignée de main, et annonce
laquelle a fonctionné (« AP-STA-CONNECTED <mac> keyid=alice »). L'adresse MAC peut
changer à chaque connexion, le boîtier sait toujours à quel profil l'appareil appartient.

Effet de bord qui règle le trou par construction : un appareil qui ne connaît aucune clé
ne s'associe pas du tout. Le refus par défaut se produit au niveau radio, avant toute
question de DNS ou d'iptables, et il n'y a plus d'« appareil inconnu en accès libre ».

CONSÉQUENCE ASSUMÉE. Sans profil enfant, il n'y a aucune clé, donc le Wi-Fi des enfants
n'est pas diffusé du tout. C'est voulu : un réseau visible qui refuse tout le monde sans
explication serait pire. L'état est dit au parent dans le tableau de bord.

CE MODULE EST LA SEULE SOURCE DE VÉRITÉ du contenu du fichier de clés. Il est importé
par l'action runner (root, qui écrit et recharge hostapd à chaud) et par le
pré-démarrage de l'AP (qui le réécrit au boot depuis config.json). Le dashboard
sandboxé, lui, ne fait que demander : il n'a pas les droits sur /etc/protectado.
"""

import json
import os
import re
import tempfile

import passphrase

# Fichier lu par hostapd (wpa_psk_file). Contient les clés en clair, comme la conf
# hostapd elle-même : 0600, root. Risque déjà connu et accepté (carte SD sortie du Pi).
PSK_FILE = "/etc/protectado/hostapd-psk"

# Contrainte WPA2 sur une passphrase : 8 à 63 caractères ASCII imprimables. Même règle
# que l'ancienne clé unique — un caractère hors de cette plage passe la longueur puis
# empêche hostapd de démarrer, ce qui couperait le Wi-Fi de tous les enfants.
KEY_RE = re.compile(r"^[\x20-\x7e]{8,63}$")

# Le keyid est la clé de profil, qui est déjà contrainte à [a-z0-9_] côté dashboard.
# On revérifie ici : ce module écrit un fichier de configuration, un identifiant
# contenant une espace ou un retour à la ligne y décalerait la lecture des champs.
KEYID_RE = re.compile(r"^[a-z0-9_]{1,64}$")

# « N'importe quelle adresse MAC » dans le format wpa_psk_file. C'est tout l'intérêt :
# la clé n'est liée à aucun matériel, donc rien à mettre à jour quand la MAC tourne.
ANY_MAC = "00:00:00:00:00:00"


def generate() -> str:
    """Nouvelle clé lisible et dictable (cf. passphrase.py pour la forme et la force)."""
    return passphrase.generate()


def valid(key) -> bool:
    """La clé est-elle utilisable telle quelle par hostapd ?

    Une clé encadrée d'espaces est refusée plutôt que rognée : hostapd rognerait de son
    côté, et le parent dicterait alors une clé qui n'est pas celle qui fonctionne.
    """
    if not isinstance(key, str):
        return False
    return key == key.strip() and bool(KEY_RE.match(key))


def keys_of(config: dict) -> dict:
    """{clé de profil: clé Wi-Fi} pour les profils enfants qui en portent une de valide.

    Le profil `monitoring` (appareil parent) est exclu : il ne rejoint pas le Wi-Fi des
    enfants, il reste sur celui de la box. Un profil dont la clé est absente ou invalide
    est ignoré SANS faire échouer le rendu : une coquille sur un profil ne doit pas
    priver les autres enfants de réseau.
    """
    out = {}
    for pkey, profile in sorted((config.get("profiles") or {}).items()):
        profile = profile or {}
        if profile.get("mode") == "monitoring":
            continue
        if not KEYID_RE.match(str(pkey)):
            continue
        key = profile.get("wifi_key")
        if valid(key):
            out[pkey] = key
    return out


def render(config: dict) -> str:
    """Contenu complet du fichier wpa_psk_file, dans un ordre déterministe.

    L'ordre trié évite une réécriture (et donc un rechargement) sans changement réel.
    """
    lines = [
        "# Protectado : clés Wi-Fi des enfants, une par profil.",
        "# Généré depuis config.json : toute modification manuelle sera écrasée.",
        "# Le keyid est la clé de profil : hostapd l'annonce à l'association, ce qui",
        "# donne l'identité de l'appareil indépendamment de son adresse MAC.",
    ]
    for pkey, key in keys_of(config).items():
        lines.append(f"keyid={pkey} {ANY_MAC} {key}")
    return "\n".join(lines) + "\n"


def write(config: dict, path: str = PSK_FILE) -> int:
    """Écrit le fichier de clés et renvoie le nombre de clés servies.

    Écriture ATOMIQUE : hostapd peut relire le fichier à tout instant (rechargement à
    chaud), et un fichier tronqué lu à ce moment-là déconnecterait des appareils dont la
    clé n'aurait pas encore été réécrite.

    Les droits sont posés AVANT le renommage, pour qu'il n'existe à aucun moment un
    fichier de clés lisible par tous.
    """
    content = render(config)
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".hostapd-psk.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return len(keys_of(config))


def count(config: dict) -> int:
    """Nombre de clés valides, c'est-à-dire d'enfants qui peuvent rejoindre le réseau."""
    return len(keys_of(config))


def _load(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


if __name__ == "__main__":
    # Appelé par le pré-démarrage de l'AP (root) : réécrit le fichier de clés depuis
    # config.json, puis renvoie le nombre de clés en code de sortie utilisable comme
    # condition de démarrage.
    #   0  = au moins une clé, l'AP peut démarrer
    #   1  = aucune clé (aucun profil enfant) : l'AP ne doit PAS être diffusé
    #   2  = config illisible : on ne diffuse pas non plus, et on le dit
    import sys

    cfg_path = sys.argv[1] if len(sys.argv) > 1 else "/opt/protectado/data/config.json"
    psk_path = sys.argv[2] if len(sys.argv) > 2 else PSK_FILE
    cfg = _load(cfg_path)
    if not cfg:
        print(f"[wifi_keys] config illisible ({cfg_path}) : aucune clé écrite", flush=True)
        sys.exit(2)
    n = write(cfg, psk_path)
    if n:
        print(f"[wifi_keys] {n} clé(s) de profil écrite(s) dans {psk_path}", flush=True)
        sys.exit(0)
    print("[wifi_keys] aucun profil enfant : le Wi-Fi des enfants n'est pas diffusé",
          flush=True)
    sys.exit(1)
