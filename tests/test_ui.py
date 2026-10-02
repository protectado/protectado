# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Arnaud Ortais
# Dual-licensed: AGPL-3.0 (open source) or Commercial License — see LICENSE and LICENSE-COMMERCIAL.
"""
test_ui.py — les gabarits : les chaînes qu'ils réclament, et les liens qui ne doivent
pas tomber dans le vide.

Une clé de traduction manquante ne lève aucune erreur : elle affiche son nom technique au
parent, au milieu d'un écran traduit, dans la langue qu'on ne relit pas.
"""

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
GABARITS = sorted((ROOT / "templates").glob("*.html"))
LANGUES = ("fr", "en", "es", "pt")
ONGLETS = ("etat", "enfants", "exceptions", "reseau", "domaines", "reglages")


def _t(langue):
    return json.loads((ROOT / "i18n" / f"{langue}.json").read_text())


def _gabarit(nom):
    return (ROOT / "templates" / nom).read_text()


@pytest.mark.parametrize("gabarit", GABARITS, ids=lambda p: p.name)
def test_toute_cle_reclamee_par_un_gabarit_existe(gabarit):
    """Les clés CONSTRUITES à l'exécution (« 'error.' + code ») finissent par un point et
    sont écartées : elles sont couvertes par les jeux de clés complets."""
    texte = gabarit.read_text()
    utilisees = {k for k in (set(re.findall(r"""T\['([^']+)'\]""", texte))
                             | set(re.findall(r"""tr\('([^']+)'""", texte))
                             | set(re.findall(r"""t\['([^']+)'\]""", texte)))
                 if not k.endswith(".")}
    for langue in LANGUES:
        manquantes = sorted(utilisees - set(_t(langue)))
        assert manquantes == [], f"{gabarit.name} en {langue} : {manquantes}"


def test_les_quatre_langues_portent_les_memes_cles():
    reference = set(_t("fr"))
    for langue in LANGUES:
        cles = set(_t(langue))
        assert cles == reference, (f"{langue} : manquantes {sorted(reference - cles)}, "
                                   f"en trop {sorted(cles - reference)}")


@pytest.mark.parametrize("cle", ["event.station_unknown_key", "event.vpn_suspected",
                                 "event.bypass_attempt_doh", "event.bypass_attempt_dot"])
def test_un_evenement_d_appareil_ne_repete_pas_son_adresse(cle):
    """Défaut constaté deux fois : l'appareil, déjà en tête de ligne (journal, historique,
    alertes), apparaissait une seconde fois dans le texte."""
    for langue in LANGUES:
        texte = _t(langue)[cle]
        assert "{mac}" not in texte and "{device}" not in texte, (cle, langue)


def test_chaque_onglet_existe_et_s_ouvre():
    index = _gabarit("index.html")
    declares = re.search(r"const TABS = \[([^\]]+)\]", index).group(1)
    assert tuple(re.findall(r"'([a-z]+)'", declares)) == ONGLETS
    for onglet in ONGLETS:
        assert f'id="tab-{onglet}"' in index and f'id="btn-{onglet}"' in index
    # Un signet noté avec un ancien nom d'onglet ne doit pas tomber dans le vide.
    alias = re.search(r"_TAB_ALIASES = \{([^}]+)\}", index).group(1)
    assert all(ancien in alias for ancien in ("dashboard", "profils", "gestion"))


def test_l_ancienne_page_reseau_mene_a_son_onglet(client):
    """Réseau était une page à part, qui recopiait l'en-tête et la barre d'onglets : la
    copie a dérivé trois fois, et l'adresse restait figée au retour. C'est un onglet ;
    l'ancienne adresse y mène."""
    r = client.get("/devices", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/?tab=reseau"
    assert not (ROOT / "templates" / "devices.html").exists()


def test_chaque_sorte_d_exception_a_son_chemin_d_annulation():
    """Clés différentes selon la sorte (profil, profil+date, domaine, adresse) : les
    confondre reviendrait à annuler l'exception d'un autre."""
    index, serveur = _gabarit("index.html"), (ROOT / "dashboard.py").read_text()
    for route in ("/api/temp-overrides/", "/api/overrides/", "/api/slot-extension/",
                  "/api/temp-domain/", "/release"):
        assert route in index, route
    for route in ("/api/exceptions", "/api/slot-extension/{profile}",
                  "/api/temp-domain/{domain:path}"):
        assert route in serveur, route


def test_la_documentation_installe_depuis_la_branche_installee():
    """Le script venait de « main » alors que le code installé par défaut vient de
    « stable » : deux versions différentes. Seule une machine de test, qui le dit
    (PROTECTADO_BRANCH=main), télécharge depuis main. Et aucune commande de la
    documentation ne pousse d'historique (publication par snapshot uniquement)."""
    fichiers = [*ROOT.glob("README*.md"), *(ROOT / "bootstrap").glob("INSTALL*.md")]
    for f in fichiers:
        for ligne in f.read_text().splitlines():
            if "raw.githubusercontent.com" in ligne and "bootstrap.sh" in ligne:
                assert "/stable/" in ligne or "PROTECTADO_BRANCH=main" in ligne, f"{f.name}: {ligne}"
            assert "git push origin stable" not in ligne, f.name


def test_chaque_categorie_a_son_nom_dans_les_quatre_langues():
    """Signalé en test : les filtres de l'onglet Domaines affichaient les clés (cdn,
    entertainment…). Les noms sont construits à l'exécution (« 'category.' + c »), donc
    hors de portée du test des clés réclamées par les gabarits."""
    liste = re.search(r"const CATEGORIES = \[([^\]]+)\]", _gabarit("index.html")).group(1)
    for cat in re.findall(r"'([a-z]+)'", liste):
        for langue in LANGUES:
            assert f"category.{cat}" in _t(langue), (cat, langue)
