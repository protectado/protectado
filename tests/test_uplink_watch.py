# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Arnaud Ortais
# Dual-licensed: AGPL-3.0 (open source) or Commercial License — see LICENSE and LICENSE-COMMERCIAL.
"""
test_uplink_watch.py — la perte de la box, comptée sous tension et à travers les
redémarrages, et les deux délais avant le mode secours.

Le relevé passe par le vrai _watch_uplink du runner ; seule la lecture de wpa_cli est
remplacée par la situation voulue, et l'horloge monotone par une horloge du test.
"""


import uplink_watch as uw

# Sortie réelle de « wpa_cli scan_results » (boîtier de test, 2026-10-02), réduite.
SCAN = ("bssid / frequency / signal level / flags / ssid\n"
        "c0:c9:e3:f7:8c:f3\t5180\t-24\t[WPA2-PSK+SAE-CCMP][WPS][ESS]\tMaBox\n"
        "ca:c9:e3:f7:8c:f3\t5180\t-24\t[WPA2-PSK-CCMP][ESS]\t\n"
        "00:c0:ca:bd:03:cf\t5180\t-10\t[WPA2-PSK-CCMP][ESS]\tMaBox-Protectado\n")


def test_un_redemarrage_ne_remet_pas_le_compteur_a_zero(uplink):
    uplink["lu"] = (False, False)
    avant = uplink["avancer"](3600)
    uplink["redemarrer"]()
    apres = uplink["avancer"](600)
    # Une heure avant, dix minutes après ; le temps débranché ne compte pas.
    assert avant["offline_sec"] == 3600
    assert apres["offline_sec"] == 3600 + 600 - uw.MAX_STEP_SEC


def test_dix_heures_sans_box_ne_declenchent_rien(uplink):
    uplink["lu"] = (False, False)
    etat = uplink["avancer"](10 * 3600)
    assert (etat["cause"], uw.due(etat)) == (uw.NOT_FOUND, "")
    assert uw.due(uplink["avancer"](14 * 3600)) == uw.NOT_FOUND


def test_un_quart_d_heure_de_cle_refusee_declenche(uplink):
    uplink["lu"] = (False, True)
    assert uw.due(uplink["avancer"](14 * 60)) == ""
    assert uw.due(uplink["avancer"](60)) == uw.AUTH_REFUSED


def test_une_box_qui_disparait_remet_le_temps_refuse_a_zero(uplink):
    uplink["lu"] = (False, True)
    uplink["avancer"](10 * 60)
    uplink["lu"] = (False, False)
    uplink["avancer"](60)
    uplink["lu"] = (False, True)
    etat = uplink["avancer"](10 * 60)
    assert uw.due(etat) == "" and etat["offline_sec"] == 21 * 60


def test_une_connexion_reussie_remet_tout_a_zero(uplink):
    uplink["lu"] = (False, True)
    uplink["avancer"](14 * 60)
    uplink["lu"] = (True, True)
    assert uplink["avancer"](60) == uw.empty_state()
    uplink["lu"] = (False, True)
    assert uw.due(uplink["avancer"](14 * 60)) == ""


def test_la_box_se_reconnait_a_son_nom_exact():
    """Le réseau enfants « MaBox-Protectado » figure dans les mêmes résultats."""
    assert uw.parse_visible(SCAN, "MaBox")
    sans_box = "\n".join(l for l in SCAN.splitlines() if not l.endswith("\tMaBox"))
    assert not uw.parse_visible(sans_box, "MaBox")
    assert not uw.parse_visible(SCAN, "")
