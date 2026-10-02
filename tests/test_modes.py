# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Arnaud Ortais
# Dual-licensed: AGPL-3.0 (open source) or Commercial License — see LICENSE and LICENSE-COMMERCIAL.
"""
test_modes.py — un seul vocabulaire des modes, et personne ne le contourne.

Le garde-fou de la fin de ce fichier a le plus de valeur à long terme : un littéral oublié
ne lève aucune erreur, il produit un mode que `normalize` ne reconnaît pas, donc un accès
fermé ou un groupe Pi-hole qui n'existe pas.
"""

import json
import re
from pathlib import Path

import pytest

import modes

ROOT = Path(__file__).resolve().parent.parent


def test_le_vocabulaire():
    assert modes.SLOT_MODES == ("off", "homework", "free")
    assert modes.OVERRIDE_MODES == ("off", "homework", "free", "normal")
    assert not modes.is_slot_mode(modes.NORMAL)


@pytest.mark.parametrize("mode,ouvert", [
    ("off", False), ("homework", True), ("free", True), ("normal", False),
    # Fail-safe : ce que personne ne reconnaît ferme.
    ("", False), (None, False), ("inconnu", False), ("libre", False), (42, False), ({}, False),
])
def test_ce_qui_ouvre_et_ce_qui_ferme(mode, ouvert):
    assert modes.is_open(mode) is ouvert


@pytest.mark.parametrize("ancien,nouveau", [
    ("blocked", "off"), ("work", "homework"), ("permissive", "free"), ("free", "free"),
    ("normal", "normal"), ("WORK", "homework"), (" work ", "homework"), ("inconnu", ""),
])
def test_l_ancien_vocabulaire_est_traduit(ancien, nouveau):
    """Un planning écrit avant le renommage doit rester applicable."""
    assert modes.normalize(ancien) == nouveau


@pytest.mark.parametrize("langue", ["fr", "en", "es", "pt"])
def test_chaque_mode_a_ses_libelles(langue):
    t = json.loads((ROOT / "i18n" / f"{langue}.json").read_text())
    for mode in modes.OVERRIDE_MODES:
        cle = modes.label_key(mode)
        assert t.get(cle, "").strip() and f"{cle}_full" in t, f"{cle} en {langue}"


# ------------------------------------------------------------------ #
#  Le garde-fou                                                       #
# ------------------------------------------------------------------ #

# Seuls fichiers autorisés à écrire un mode en clair : ceux qui déclarent le vocabulaire.
_SOURCE_AUTORISEE = {"modes.py", "access_grid.py"}
_ANCIENNES = ("blocked", "permissive")
# Exception nommée : « apply_forward » accepte encore l'ancien champ « blocked » le temps
# qu'une action déjà en file s'exécute après une mise à jour.
_TOLEREES = (
    'if "deny" not in args and "blocked" not in args:',
    'blocked = bool(args.get("deny", args.get("blocked", True)))',
)


def test_aucun_ancien_mode_ecrit_en_clair():
    coupables = []
    for fichier in sorted(ROOT.glob("*.py")):
        if fichier.name in _SOURCE_AUTORISEE:
            continue
        for numero, ligne in enumerate(fichier.read_text().splitlines(), start=1):
            nue = ligne.strip()
            if nue.startswith("#") or nue in _TOLEREES:
                continue
            if any(re.search(rf'''["']{a}["']''', ligne) for a in _ANCIENNES):
                coupables.append(f"{fichier.name}:{numero} {nue[:80]}")
    for gabarit in sorted((ROOT / "templates").glob("*.html")):
        for numero, ligne in enumerate(gabarit.read_text().splitlines(), start=1):
            if any(m in ligne for m in ('"permissive"', "'permissive'", '"blocked"',
                                        "'blocked'", "mode.permissive", "mode.blocked",
                                        "mode.work")):
                coupables.append(f"{gabarit.name}:{numero} {ligne.strip()[:80]}")
    assert coupables == [], "modes écrits en clair :\n" + "\n".join(coupables)
