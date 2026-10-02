# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Arnaud Ortais
# Dual-licensed: AGPL-3.0 (open source) or Commercial License — see LICENSE and LICENSE-COMMERCIAL.
"""
test_scheduler.py — le créneau en cours, la réouverture annoncée, et les rallonges.

Dates de référence : le 29 septembre 2026 est un mardi, le 30 un mercredi, le 1er octobre
un jeudi.
"""

from datetime import datetime, timedelta

import pytest

import modes
import scheduler

JOURS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
LIBRE_7_20 = [{"start": "07:00", "end": "20:00", "mode": modes.FREE}]


def _planning(config, plages, profil="alice"):
    """Le même planning tous les jours, ou un planning par jour si `plages` est un dict."""
    config.data["profiles"][profil]["schedule"] = (
        {j: list(plages.get(j, [])) for j in JOURS} if isinstance(plages, dict)
        else {j: list(plages) for j in JOURS})
    config.save()


def _le(jour, h, m=0, mois=9):
    return datetime(2026, mois, jour, h, m)


# ------------------------------------------------------------------ #
#  La réouverture, plutôt que « jusqu'à 23h59 »                       #
# ------------------------------------------------------------------ #
#  Un créneau fermé rapporte 00:00-23:59 : des bornes de journée, pas une heure de fin.
#  L'interface annonçait donc « coupé jusqu'à 23h59 » à 21 h un mardi, alors que l'accès
#  rouvrait le mercredi à 7 h.

@pytest.mark.parametrize("plages,instant,attendu", [
    # Le cas du parent : plus rien après 20 h, réouverture le lendemain à 7 h.
    (LIBRE_7_20, _le(29, 21, 30), _le(30, 7)),
    # Un creux dans la journée rouvre le jour même.
    ([{"start": "07:00", "end": "12:00", "mode": modes.FREE},
      {"start": "14:00", "end": "20:00", "mode": modes.FREE}], _le(29, 12, 30), _le(29, 14)),
    (LIBRE_7_20, _le(29, 6), _le(29, 7)),
    # Une plage coupée n'est pas une ouverture ; « devoirs » en est une.
    ([{"start": "21:00", "end": "23:00", "mode": modes.OFF}] + LIBRE_7_20,
     _le(29, 20, 30), _le(30, 7)),
    ([{"start": "17:00", "end": "19:00", "mode": modes.HOMEWORK}], _le(29, 9), _le(29, 17)),
    # Un jour sans plage est enjambé.
    ({j: LIBRE_7_20 for j in JOURS if j != "wednesday"}, _le(29, 21), _le(1, 7, mois=10)),
    # Rien dans la semaine : on n'annonce rien plutôt qu'une date extrapolée.
    ([], _le(29, 21), None),
])
def test_la_prochaine_ouverture(db, config, plages, instant, attendu):
    _planning(config, plages)
    assert scheduler.next_opening("alice", instant) == attendu


@pytest.mark.parametrize("mode,attendu", [(modes.OFF, _le(1, 7, mois=10)),
                                          (modes.FREE, _le(30, 0))])
def test_une_derogation_de_journee_remplace_le_planning_du_jour(db, config, mode, attendu):
    """Coupé : le jour entier est fermé. Ouvert : dès minuit, pas à 7 h."""
    _planning(config, LIBRE_7_20)
    db.set_schedule_override("alice", "2026-09-30", mode, "test")
    assert scheduler.next_opening("alice", _le(29, 21)) == attendu


def test_l_ouverture_annoncee_est_toujours_dans_le_futur(db, config):
    _planning(config, LIBRE_7_20)
    for heure in range(24):
        ouverture = scheduler.next_opening("alice", _le(29, heure, 30))
        assert ouverture is None or ouverture > _le(29, heure, 30)


def test_l_ancien_format_de_planning_est_lu_comme_le_creneau_courant(db, config):
    config.data["profiles"]["alice"]["schedule"] = {
        "weekday": LIBRE_7_20,
        "weekend": [{"start": "09:00", "end": "22:00", "mode": modes.FREE}]}
    config.save()
    assert scheduler.next_opening("alice", _le(29, 21)) == _le(30, 7)
    assert scheduler.next_opening("alice", _le(3, 7, mois=10)) == _le(3, 9, mois=10)


def test_reopening_at_part_de_l_etat_reel(db, config):
    _planning(config, LIBRE_7_20)
    assert scheduler.reopening_at("alice", _le(29, 10)) is None          # ouvert
    assert scheduler.reopening_at("alice", _le(29, 21)) == _le(30, 7)


def test_une_derogation_qui_coupe_n_annonce_pas_sa_propre_echeance(db, config):
    """Le planning est fermé aussi à l'échéance : l'accès rouvre à l'ouverture suivante,
    pas dans « encore 60 min ». Horaires relatifs à l'horloge réelle, comme l'échéance."""
    maintenant = datetime.now()
    ouvre = (maintenant + timedelta(hours=3)).replace(second=0, microsecond=0)
    ferme = ouvre + timedelta(minutes=30)
    if ferme.date() != maintenant.date():
        pytest.skip("créneau à cheval sur minuit")
    _planning(config, [{"start": ouvre.strftime("%H:%M"), "end": ferme.strftime("%H:%M"),
                        "mode": modes.FREE}])
    scheduler.set_temp_override("alice", modes.OFF, 60)
    assert scheduler.reopening_at("alice") == ouvre


def test_une_derogation_qui_coupe_rend_la_main_a_un_planning_ouvert(db, config):
    _planning(config, [{"start": "00:00", "end": "23:59", "mode": modes.FREE}])
    scheduler.set_temp_override("alice", modes.OFF, 30)
    attendu = datetime.now() + timedelta(minutes=30)
    assert abs((scheduler.reopening_at("alice") - attendu).total_seconds()) < 90


def test_une_derogation_qui_ouvre_n_annonce_aucune_reouverture(db, config):
    _planning(config, [])
    scheduler.set_temp_override("alice", modes.FREE, 30)
    assert scheduler.reopening_at("alice") is None


def test_une_plage_illisible_en_configuration_est_ignoree(db, config):
    config.data["profiles"]["alice"]["schedule"] = {"wednesday": [
        {"start": "xx", "end": "10:00", "mode": modes.FREE},
        {"end": "12:00", "mode": modes.FREE},
        {"start": "12:00", "end": "14:00", "mode": modes.HOMEWORK}]}
    config.save()
    assert scheduler.get_current_slot("alice", _le(30, 13))["mode"] == modes.HOMEWORK
    assert scheduler.get_current_slot("alice", _le(30, 9))["mode"] == modes.OFF


# ------------------------------------------------------------------ #
#  Les rallonges : une plage ouverte, et seulement elle               #
# ------------------------------------------------------------------ #

SEMAINE = [
    {"start": "00:00", "end": "05:30", "mode": modes.OFF},
    {"start": "05:30", "end": "08:00", "mode": modes.HOMEWORK},
    {"start": "08:00", "end": "16:00", "mode": modes.OFF},
    {"start": "16:00", "end": "16:30", "mode": modes.FREE},
    {"start": "16:30", "end": "19:30", "mode": modes.HOMEWORK},
    {"start": "19:30", "end": "22:00", "mode": modes.FREE},
    {"start": "22:00", "end": "23:59", "mode": modes.OFF},
]
MINUIT = [{"start": "00:00", "end": "20:00", "mode": modes.OFF},
          {"start": "20:00", "end": "23:45", "mode": modes.FREE},
          {"start": "23:45", "end": "23:59", "mode": modes.OFF}]


def _mode(jour, h, m=0, mois=9):
    return scheduler.get_current_slot("alice", _le(jour, h, m, mois))["mode"]


def test_la_rallonge_du_matin_ne_touche_aucune_autre_plage(db, config):
    """Défaut reproduit (P0-3) : à 16:10, « off » au lieu de la plage libre de 16:00."""
    _planning(config, SEMAINE)
    assert scheduler.extend_current_slot("alice", 30, now=_le(30, 7, 50))
    assert [_mode(30, 8, 10), _mode(30, 8, 31), _mode(30, 16, 10), _mode(30, 22, 10)] == \
        [modes.HOMEWORK, modes.OFF, modes.FREE, modes.OFF]


def test_la_rallonge_du_soir_prolonge_puis_s_arrete(db, config):
    _planning(config, SEMAINE)
    assert scheduler.extend_current_slot("alice", 30, now=_le(30, 21, 50))
    assert scheduler.get_current_slot("alice", _le(30, 22, 10))["slot_end"] == "22:30"
    assert [_mode(30, 22, 10), _mode(30, 22, 31)] == [modes.FREE, modes.OFF]


def test_la_rallonge_est_refusee_en_plage_coupee(db, config):
    _planning(config, SEMAINE)
    assert scheduler.extend_current_slot("alice", 30, now=_le(30, 10)) is False
    assert db.get_slot_extensions() == []


def test_les_rallonges_se_cumulent_sur_la_meme_plage_seulement(db, config):
    _planning(config, SEMAINE)
    scheduler.extend_current_slot("alice", 10, now=_le(30, 21, 40))
    scheduler.extend_current_slot("alice", 15, now=_le(30, 21, 55))
    assert [_mode(30, 22, 24), _mode(30, 22, 26)] == [modes.FREE, modes.OFF]
    scheduler.extend_current_slot("alice", 30, now=_le(30, 7, 50))    # autre plage
    assert _mode(30, 22, 5) == modes.OFF


def test_la_rallonge_traverse_minuit_et_reste_visible(db, config):
    _planning(config, MINUIT)
    assert scheduler.extend_current_slot("alice", 30, now=_le(30, 23, 40))
    assert [_mode(1, 0, 5, mois=10), _mode(1, 0, 16, mois=10)] == [modes.FREE, modes.OFF]
    actives = scheduler.active_extensions(now=_le(1, 0, 5, mois=10))
    assert [(x["profile"], x["ends_at"]) for x in actives] == [("alice", "2026-10-01T00:15:00")]


def test_la_rallonge_d_hier_ne_s_applique_pas_aujourd_hui(db, config):
    _planning(config, SEMAINE)
    scheduler.extend_current_slot("alice", 30, now=_le(29, 21, 50))
    assert _mode(30, 22, 10) == modes.OFF


def test_migration_d_une_base_sans_plage_de_rallonge(db):
    with db.get_db() as conn:
        conn.execute("DROP TABLE slot_extensions")
        conn.execute("CREATE TABLE slot_extensions (profile TEXT PRIMARY KEY, "
                     "minutes INTEGER NOT NULL, day TEXT NOT NULL, updated_at TEXT NOT NULL)")
        conn.execute("INSERT INTO slot_extensions VALUES ('alice', 20, '2026-09-30', 'x')")
    db.init_db()
    db.init_db()
    assert db.get_slot_extension("alice") == (20, "2026-09-30", "")


def test_l_exemple_de_configuration_suit_le_format_courant():
    """P4 : l'exemple livré portait encore le planning semaine/week-end et deux clés que
    plus rien ne lit (network.gateway, network.subnet), et pas de section privacy."""
    import json
    from pathlib import Path
    import privacy
    import scheduler
    exemple = json.loads((Path(__file__).resolve().parent.parent / "config.json.example")
                         .read_text())
    assert set(exemple["network"]) == {"enforcement"}
    assert exemple["privacy"]["retention_days"] == privacy.DEFAULT_RETENTION_DAYS
    planning = exemple["profiles"]["alice"]["schedule"]
    assert list(planning) == scheduler._DAY_KEYS
