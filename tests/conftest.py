# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Arnaud Ortais
# Dual-licensed: AGPL-3.0 (open source) or Commercial License — see LICENSE and LICENSE-COMMERCIAL.
"""
conftest.py — harnais de test du boîtier.

PRINCIPES

1. BASE RÉELLE, JAMAIS SIMULÉE. Les tests ouvrent une vraie base sqlite dans un fichier
   temporaire et appellent `database.init_db()`. Simuler sqlite ferait passer des tests
   sur un schéma imaginaire : les défauts déjà rencontrés dans ce produit portaient
   justement sur le schéma (colonne absente d'un SELECT, table jamais purgée).

2. AUCUN SOCKET. Rien n'est émis : ni Pi-hole, ni Cloudflare, ni OpenRouter. Les doubles
   ENREGISTRENT ce qu'on leur demande, ce qui permet d'affirmer « le produit a demandé
   ceci » au lieu de « le produit n'a pas planté ».

3. PATCHER LES MODULES, PAS SEULEMENT `paths`. Les huit modules du produit écrivent
   `from paths import CONFIG_PATH`, ce qui COPIE la valeur au moment de l'import.
   Repointer `paths.CONFIG_PATH` seul ne change donc rien pour eux, et les tests
   écriraient dans la vraie configuration du poste. Les fixtures repatchent chaque
   module déjà chargé qui porte l'attribut, alias compris.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ------------------------------------------------------------------ #
#  Repointage des chemins                                             #
# ------------------------------------------------------------------ #

# Alias sous lesquels un module peut avoir importé un chemin (cf. action_runner, qui
# renomme CONFIG_PATH en _CONFIG_PATH). Repatcher l'un sans l'autre laisserait un module
# écrire au vrai emplacement.
_ALIASES = {
    "CONFIG_PATH": ("CONFIG_PATH", "_CONFIG_PATH"),
    "DB_PATH":     ("DB_PATH", "_DB_PATH"),
    "DATA_DIR":    ("DATA_DIR",),
    "ACTION_QUEUE_DIR": ("ACTION_QUEUE_DIR",),
}


def _repoint(monkeypatch, name: str, value: str):
    """Repointe un chemin dans `paths` ET dans tous les modules du produit déjà chargés."""
    import paths
    monkeypatch.setattr(paths, name, value)
    for module in list(sys.modules.values()):
        origin = getattr(module, "__file__", None)
        if not origin or not str(origin).startswith(str(ROOT)):
            continue                      # module tiers : jamais touché
        for attr in _ALIASES[name]:
            if hasattr(module, attr):
                monkeypatch.setattr(module, attr, value, raising=False)


# ------------------------------------------------------------------ #
#  Base de données                                                    #
# ------------------------------------------------------------------ #

@pytest.fixture
def db(tmp_path, monkeypatch):
    """Base vide et RÉELLE, propre à chaque test. Renvoie le module `database`."""
    import database
    _repoint(monkeypatch, "DB_PATH", str(tmp_path / "protectado.db"))
    database.init_db()
    return database


# ------------------------------------------------------------------ #
#  Configuration                                                      #
# ------------------------------------------------------------------ #

def _base_config() -> dict:
    """Configuration de test, dérivée de config.json.example.

    On part de l'exemple livré plutôt que d'un dictionnaire écrit à la main : un champ
    ajouté au produit et oublié dans l'exemple se voit alors dans les tests, et un test
    ne peut pas passer sur une forme de configuration que le produit ne produit jamais.
    """
    config = json.loads((ROOT / "config.json.example").read_text())
    config["configured"] = True
    config["dashboard_password"] = "mot-de-passe-de-test"
    # Partage IA COUPÉ par défaut. config.json.example porte une clé OpenRouter factice,
    # et `share_with_ai` vaut vrai dès qu'une clé existe : sans cet interrupteur, une
    # escalade déclenchée par un test tenterait un appel sortant. Un test qui veut
    # éprouver le chemin IA le rallume lui-même, avec un client simulé.
    config["privacy"] = {"share_with_ai": False}
    # Posture passerelle : c'est celle où le boîtier est routeur, donc celle où presque
    # toutes les règles s'appliquent. Un test qui a besoin de dns_only le dit lui-même.
    config.setdefault("network", {})
    config["network"]["enforcement"] = "gateway"
    config["network"]["kids"] = {"ssid": "Box-Protectado"}
    journee = [{"start": "00:00", "end": "23:59", "mode": "permissive"}]
    jours = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
    config["profiles"] = {
        # Deux enfants, chacun avec SA clé Wi-Fi : c'est elle qui porte l'identité de
        # l'appareil depuis que la MAC n'est plus de confiance (cf. wifi_keys.py).
        "alice": {
            "name": "Alice", "birth_year": 2014, "alias": "Enfant 1",
            "wifi_key": "bamito-kesuna-porelu-472",
            "devices": [{"ip": "192.168.50.52", "mac": "02:aa:bb:cc:dd:01"}],
            "schedule": {j: list(journee) for j in jours},
        },
        "bruno": {
            "name": "Bruno", "birth_year": 2010, "alias": "Enfant 2",
            "wifi_key": "dorali-tamuse-vakino-318",
            "devices": [{"ip": "192.168.50.53", "mac": "02:aa:bb:cc:dd:02"}],
            "schedule": {j: list(journee) for j in jours},
        },
        # Appareil parent : surveillance passive, jamais coupé, pas de clé enfants.
        "monitoring": {
            "name": "Parent", "mode": "monitoring",
            "devices": [{"ip": "192.168.1.20", "mac": "02:aa:bb:cc:dd:03"}],
        },
    }
    return config


@pytest.fixture
def config(tmp_path, monkeypatch):
    """config.json temporaire, déjà repointé pour tous les modules chargés.

    Renvoie un objet qui porte le dict (`.data`), son chemin (`.path`) et de quoi le
    réécrire (`.save()`), parce qu'un test qui modifie la configuration doit pouvoir la
    faire relire par le produit.
    """
    path = tmp_path / "config.json"

    class _Config:
        def __init__(self, data):
            self.data = data
            self.path = str(path)

        def save(self, data=None):
            if data is not None:
                self.data = data
            path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2))
            return self.data

    holder = _Config(_base_config())
    holder.save()
    _repoint(monkeypatch, "CONFIG_PATH", str(path))
    _repoint(monkeypatch, "DATA_DIR", str(tmp_path))
    return holder


# ------------------------------------------------------------------ #
#  Doubles : Pi-hole et file d'actions                                #
# ------------------------------------------------------------------ #

class FakePiHole:
    """Double de PiHoleAPI : enregistre les appels, n'émet rien.

    Toute méthode non prévue est acceptée et enregistrée, et renvoie la valeur posée
    dans `returns`. Un double qui lève sur une méthode inconnue ferait échouer un test
    pour une raison qui n'a rien à voir avec ce qu'il vérifie.
    """

    def __init__(self, **returns):
        self.calls: list[tuple] = []
        self.returns: dict = {
            "get_recent_queries": [],
            "get_network_devices": [],
            "get_clients": [],
            "assign_client_to_group": True,
            "switch_profile_mode": True,
            "setup_profiles": True,
            "get_deny_domains": [],
        }
        self.returns.update(returns)
        # Lus tels quels par monitor.reload_config pour décider de recréer le client.
        self.host = "http://localhost:81"
        self.password = "mot-de-passe-pihole"

    def queries_by_client(self, queries):
        """Seule méthode au comportement RÉEL : les tests s'en servent pour fabriquer
        des fenêtres de requêtes, et la simuler reviendrait à tester le double."""
        out: dict = {}
        for q in queries or []:
            out.setdefault(q.get("client", ""), []).append(q.get("domain", ""))
        return out

    def names(self) -> list:
        """Noms des méthodes appelées, dans l'ordre : lecture plus lisible dans un test."""
        return [name for name, _, _ in self.calls]

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)

        def recorder(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return self.returns.get(name)

        return recorder


@pytest.fixture
def pihole():
    return FakePiHole()


@pytest.fixture
def action_queue(monkeypatch):
    """Capture la file d'actions au lieu d'écrire sur disque.

    Renvoie la liste des (action, args) demandées. La file réelle est un répertoire lu
    par le service privilégié : un test qui y écrit vraiment ferait agir un runner s'il
    tourne sur la machine de développement.
    """
    import monitor
    captured: list[tuple] = []

    def _capture(self, action, args):
        captured.append((action, args))

    monkeypatch.setattr(monitor.ProtectadoMonitor, "_queue_action", _capture)
    return captured


# ------------------------------------------------------------------ #
#  Application servie en direct                                       #
# ------------------------------------------------------------------ #

@pytest.fixture
def client(db, config, pihole, monkeypatch):
    """Application servie en direct, avec une session parent ouverte.

    Le moniteur est un double : les endpoints d'annulation appellent notify() et
    _apply_pihole_mode(), et monter un vrai moniteur ferait parler à Pi-hole.
    """
    from fastapi.testclient import TestClient

    import dashboard

    class _Scanner:
        """Inventaire réseau muet : /api/status l'interroge avant toute autre chose."""

        def get_active_ips(self):
            return set()

        def scan(self):
            return []

    class _Moniteur:
        def __init__(self):
            self.config = config.data
            self.pihole = pihole
            self.scanner = _Scanner()
            self.applied: list = []
            self.notified = 0

        def _apply_pihole_mode(self, profile, mode):
            self.applied.append((profile, mode))

        # Réduction d'un nom de domaine à sa racine. C'est la VRAIE, importée du produit :
        # la simuler ferait passer des tests sur un regroupement imaginaire.
        @staticmethod
        def _root_domain(domain):
            import monitor

            return monitor.ProtectadoMonitor._root_domain(domain)

        def notify(self):
            self.notified += 1

    moniteur = _Moniteur()
    monkeypatch.setattr(dashboard, "get_monitor", lambda: moniteur)
    # Le statut est mis en cache huit secondes pour ne pas marteler Pi-hole. Deux tests
    # qui l'appellent coup sur coup liraient donc la réponse du premier, et le second
    # vérifierait un état qu'il n'a pas posé.
    monkeypatch.setattr(dashboard, "_status_cache", {})
    monkeypatch.setattr(dashboard, "_STATUS_CACHE_TTL", 0)
    # Session toujours ouverte par défaut : ce que ces tests éprouvent est le
    # comportement des endpoints, pas le portier. Un test qui veut vérifier le refus
    # repatche _check_session lui-même.
    monkeypatch.setattr(dashboard, "_check_session", lambda request: True)
    c = TestClient(dashboard.app)
    c.moniteur = moniteur
    return c


# ------------------------------------------------------------------ #
#  Garde-fou réseau, pour TOUS les tests                              #
# ------------------------------------------------------------------ #

@pytest.fixture(autouse=True)
def _aucune_connexion(monkeypatch):
    """Un test qui tenterait une connexion échoue ici, au lieu de dépendre du réseau de
    la machine qui l'exécute (ou d'appeler un vrai Pi-hole, un vrai fournisseur IA)."""
    import socket

    def interdit(*args, **kwargs):
        raise AssertionError("un test a tenté d'ouvrir une connexion réseau")

    monkeypatch.setattr(socket.socket, "connect", interdit)
    monkeypatch.setattr(socket, "create_connection", interdit)


# ------------------------------------------------------------------ #
#  Objets du produit, construits par leur vrai constructeur           #
# ------------------------------------------------------------------ #

@pytest.fixture
def moniteur(db, config, pihole, tmp_path):
    """Moniteur construit par son VRAI __init__ (aucun attribut recopié à la main, donc
    rien à tenir à jour ici quand le moniteur en gagne un), branché sur le double de
    Pi-hole, sans thread ni inventaire réseau."""
    import monitor

    m = monitor.ProtectadoMonitor(config.path)
    m.pihole = pihole
    m.scanner = type("Inventaire", (), {"scan": staticmethod(lambda: []),
                                        "get_active_ips": staticmethod(set)})()
    return m


@pytest.fixture
def pihole_api():
    """Vrai PiHoleAPI dont les appels HTTP sont remplacés : `reponses` associe un
    chemin à la réponse de GET, `appels` enregistre tout ce qui est émis."""
    import pihole_api as module

    api = module.PiHoleAPI("http://pihole.test", "x")
    api.reponses, api.appels = {}, []

    def emis(methode):
        def appel(endpoint, payload=None):
            api.appels.append((methode, endpoint, payload))
            if methode == "DELETE":
                return True
            # PUT d'une entrée de blocage : FTL renvoie l'entrée modifiée.
            return {"domains": [payload]} if endpoint.startswith("/domains/") else {}
        return appel

    api._get = lambda endpoint, params=None: (
        api.appels.append(("GET", endpoint, params)) or api.reponses.get(endpoint, {}))
    api._post, api._put, api._patch = emis("POST"), emis("PUT"), emis("PATCH")
    api._delete = lambda endpoint: emis("DELETE")(endpoint)
    return api


@pytest.fixture
def runner(monkeypatch, config, tmp_path):
    """action_runner sans aucune commande réelle.

    `runner.journal` reçoit, dans l'ordre, chaque règle iptables (« ipt », table,
    arguments…) et chaque commande lancée (« run », commande…). Les commandes rendent
    une sortie vide et réussissent ; `runner.sorties` (par préfixe de trois mots) et
    `runner.codes` (par commande exacte) permettent d'en scénariser une.
    Aucun outil optionnel n'est « installé » : un test qui veut ipset ou ip6tables le
    déclare dans `runner.outils`. L'état DoH et BYPASS du module repart de zéro.
    """
    import action_runner as r

    r.journal, r.sorties, r.codes, r.outils = [], {}, {}, set()

    def ipt(*args, table=None, check_only=False):
        r.journal.append(("ipt", table or "filter") + args)
        # Comme le vrai iptables sur une table neuve : rien n'est présent, donc -C et -D
        # échouent, tout le reste réussit.
        return not (check_only or args[0] == "-D")

    def run(cmd, *a, **k):
        r.journal.append(("run",) + tuple(cmd))
        sortie = r.sorties.get(tuple(cmd[:3]), "")
        code = r.codes.get(tuple(cmd), 0)
        return subprocess.CompletedProcess(cmd, code, sortie, "")

    monkeypatch.setattr(r, "_ipt", ipt)
    monkeypatch.setattr(r, "_ip6t", lambda *args: r.journal.append(("ip6t",) + args) or True)
    monkeypatch.setattr(r.subprocess, "run", run)
    monkeypatch.setattr(r.shutil, "which", lambda nom: f"/usr/sbin/{nom}" if nom in r.outils else None)
    monkeypatch.setattr(r, "DATA_DIR", str(tmp_path))
    for nom, valeur in (("_gateway_active", True), ("_doh_stamp", None),
                        ("_doh_available", False), ("_doh_problem", ""),
                        ("_bypass_ips", ()), ("_bypass_generation", "")):
        monkeypatch.setattr(r, nom, valeur)
    monkeypatch.setattr(r, "_write_gateway_status", lambda state, detail="": r.journal.append(
        ("statut", state, detail)))
    return r


def regles(runner, chaine, table="filter"):
    """Règles AJOUTÉES ou INSÉRÉES dans une chaîne, dans l'ordre, sans le verbe."""
    return [e[4:] if e[2] == "-A" else e[5:]
            for e in runner.journal
            if e[0] == "ipt" and e[1] == table and e[2] in ("-A", "-I") and e[3] == chaine]
