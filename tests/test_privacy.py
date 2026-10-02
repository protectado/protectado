# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Arnaud Ortais
# Dual-licensed: AGPL-3.0 (open source) or Commercial License — see LICENSE and LICENSE-COMMERCIAL.
"""
test_privacy.py — une seule échelle d'âge (6-9 / 10-12 / 13-15 / 16+), le niveau de vie
privée qui en découle, et ce que le modèle apprend d'un enfant : sa tranche, rien d'autre.
"""

from datetime import datetime

import pytest

import privacy


@pytest.mark.parametrize("age,tranche", [
    (0, "6-9"), (3, "6-9"), (6, "6-9"), (9, "6-9"), (10, "10-12"), (12, "10-12"),
    (13, "13-15"), (15, "13-15"), (16, "16+"), (25, "16+"), (3.5, "6-9"),
    (None, privacy.BAND_UNKNOWN), ("", privacy.BAND_UNKNOWN), ("douze", privacy.BAND_UNKNOWN),
    ({}, privacy.BAND_UNKNOWN), (-1, privacy.BAND_UNKNOWN),
])
def test_la_tranche_d_age(age, tranche):
    """Sous 6 ans : la tranche basse. Illisible ou négatif : inconnu, jamais deviné."""
    assert privacy.age_band(age) == tranche


@pytest.mark.parametrize("age,niveau", [
    (6, privacy.DETAILED), (12, privacy.DETAILED), (13, privacy.SUMMARY),
    (15, privacy.SUMMARY), (16, privacy.MINIMAL), (99, privacy.MINIMAL),
    (None, privacy.DETAILED), ("douze", privacy.DETAILED),
])
def test_le_niveau_par_defaut_suit_la_tranche(age, niveau):
    """Sans âge, on ne suppose pas un adolescent : niveau le plus protecteur."""
    assert privacy.default_level_for_age(age) == niveau


def test_l_annee_de_naissance_decide_sauf_niveau_explicite(config):
    annee = datetime.now().year
    alice, bruno = config.data["profiles"]["alice"], config.data["profiles"]["bruno"]
    alice.update(birth_year=annee - 11)
    bruno.update(birth_year=annee - 17)
    alice.pop("privacy_level", None)
    bruno.pop("privacy_level", None)
    assert (privacy.level_of(alice), privacy.level_of(bruno)) == (privacy.DETAILED,
                                                                  privacy.MINIMAL)
    bruno["privacy_level"] = privacy.SUMMARY
    assert privacy.level_of(bruno) == privacy.SUMMARY


def test_le_modele_ne_recoit_que_la_tranche(config):
    """Ni prénom, ni âge exact dans le prompt : c'est la raison d'être des tranches."""
    import claude_agent
    profils = config.data["profiles"]
    profils["alice"]["birth_year"] = datetime.now().year - 8
    profils["bruno"]["birth_year"] = datetime.now().year - 14
    config.save()
    prompt = claude_agent._build_system_prompt()
    assert "6-9" in prompt and "13-15" in prompt
    for interdit in ("Alice", "Bruno", "8 ans", "14 ans"):
        assert interdit not in prompt
