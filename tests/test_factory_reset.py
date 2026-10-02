# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Arnaud Ortais
# Dual-licensed: AGPL-3.0 (open source) or Commercial License — see LICENSE and LICENSE-COMMERCIAL.
"""
test_factory_reset.py — la sortie d'usine efface ce qui appartient à la famille, et
seulement cela.

Arborescence imitant un boîtier sous tmp_path ; aucune commande n'est lancée.
"""

import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

import factory_reset as fr

INSTALL = "/opt/protectado"

A_EFFACER = [f"{INSTALL}/data/{n}" for n in fr.DATA_FILES] + list(fr.SYSTEM_FILES) + [
    "/var/log/pihole/pihole.log.2.gz", "/var/log/pihole/FTL.log.1",
    "/opt/protectado-bk-1759380000/config.json",
    "/opt/protectado-bk-1759380000/protectado.db",
]
A_CONSERVER = [f"{INSTALL}/{n}" for n in (
    "monitor.py", ".venv/bin/python3", "data/version.json", "data/branch",
    "data/maintenance.json", "data/selfheal.state", "data/update.failures",
    "data/update.log", "data/services.local.json", "data/pairing_code")] + [
    "/var/lib/protectado/migrations.done", "/etc/protectado/dnsmasq-ap.conf",
    "/etc/protectado/agent.json", "/etc/cron.d/protectado",
    # Le journal courant de Pi-hole est vidé par « pihole flush », pas supprimé : seules
    # les archives tournées (*.log.*) partent avec les fichiers.
    "/var/log/pihole/pihole.log", "/var/log/protectado-bootstrap.log",
]
VIDES = list(fr.LOGS_TRUNCATE)

GRAVITE = """
CREATE TABLE "group" (id INTEGER PRIMARY KEY, enabled BOOLEAN, name TEXT UNIQUE,
                      description TEXT);
CREATE TABLE client (id INTEGER PRIMARY KEY, ip TEXT UNIQUE, comment TEXT);
CREATE TABLE client_by_group (client_id INTEGER, group_id INTEGER);
CREATE TABLE domainlist (id INTEGER PRIMARY KEY, type INTEGER, domain TEXT, comment TEXT);
CREATE TABLE domainlist_by_group (domainlist_id INTEGER, group_id INTEGER);
INSERT INTO "group" VALUES (0, 1, 'Default', 'The default group'),
    (1, 1, 'alice-off', 'Protectado — alice-off'), (2, 1, 'invites', 'à la main');
INSERT INTO client VALUES (1, '192.168.50.52', 'Tablette'),
    (2, '192.168.50.53', 'Protectado — 192.168.50.53'), (3, '192.168.1.9', 'NAS');
INSERT INTO client_by_group VALUES (1, 1), (2, 0), (3, 2);
INSERT INTO domainlist VALUES (1, 3, '.*', 'protectado:blocked:alice'),
    (2, 1, 'pub.exemple.fr', 'liste du parent');
INSERT INTO domainlist_by_group VALUES (1, 1), (2, 0);
"""


def _poser(racine: Path, chemin: str, contenu: str = "x"):
    f = racine / chemin.lstrip("/")
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(contenu)
    return f


@pytest.fixture
def boitier(tmp_path):
    for chemin in A_EFFACER + A_CONSERVER + VIDES:
        _poser(tmp_path, chemin, "historique")
    gravite = tmp_path / fr.GRAVITY_DB.lstrip("/")
    gravite.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(gravite) as conn:
        conn.executescript(GRAVITE)
    return tmp_path


def test_la_reinitialisation_efface_ce_qui_appartient_a_la_famille(boitier):
    resultat = fr.run(racine=str(boitier), install_dir=INSTALL, commandes=False)
    assert resultat["erreurs"] == []
    for chemin in A_EFFACER:
        assert not (boitier / chemin.lstrip("/")).exists(), chemin
    assert not list(boitier.glob("opt/protectado-bk-*"))
    for chemin in A_CONSERVER:
        assert (boitier / chemin.lstrip("/")).read_text() == "historique", chemin
    for chemin in VIDES:
        assert (boitier / chemin.lstrip("/")).read_text() == "", chemin


def test_pihole_ne_garde_que_ce_que_protectado_n_a_pas_cree(boitier):
    fr.run(racine=str(boitier), install_dir=INSTALL, commandes=False)
    with sqlite3.connect(boitier / fr.GRAVITY_DB.lstrip("/")) as conn:
        def lire(sql):
            return sorted(tuple(r) for r in conn.execute(sql))
        assert lire('SELECT name FROM "group"') == [("Default",), ("invites",)]
        # Le client renommé par le parent (« Tablette ») part avec son groupe.
        assert lire("SELECT ip FROM client") == [("192.168.1.9",)]
        assert lire("SELECT client_id, group_id FROM client_by_group") == [(3, 2)]
        assert lire("SELECT domain FROM domainlist") == [("pub.exemple.fr",)]
        assert lire("SELECT * FROM domainlist_by_group") == [(2, 0)]


@pytest.mark.skipif(os.geteuid() != 0, reason="reset exige root")
def test_reset_full_passe_par_factory_reset(tmp_path):
    racine = Path(__file__).resolve().parent.parent
    trace = tmp_path / "appel"
    python = _poser(tmp_path, ".venv/bin/python",
                    f'#!/bin/sh\necho "$PWD $*" > {trace}\nexit 0\n')
    python.chmod(0o755)
    r = subprocess.run(["bash", str(racine / "bootstrap" / "protectado-boot.sh"),
                        "reset", "--full"],
                       env={**os.environ, "INSTALL_DIR": str(tmp_path)},
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert trace.read_text().split() == [str(tmp_path), "-m", "factory_reset"]
