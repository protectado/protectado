# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Arnaud Ortais
# Dual-licensed: AGPL-3.0 (open source) or Commercial License — see LICENSE and LICENSE-COMMERCIAL.
"""
test_monitor.py — le cycle de surveillance : ce qu'il applique, quand, et ce qu'il signale.

Le moniteur est construit par son vrai constructeur (fixture `moniteur`), branché sur le
double de Pi-hole ; la file d'actions est capturée (`action_queue`).
"""

import json
import threading
import time
from datetime import datetime, timedelta

import pytest

import modes

IP_A = "192.168.50.52"


def _slot(mode, start="00:00", end="23:59"):
    return {"mode": mode, "slot_start": start, "slot_end": end, "override": False}


@pytest.fixture
def planning(monkeypatch):
    """Créneau renvoyé par get_slot_at, piloté par le test (temps libre par défaut)."""
    import monitor
    etat: dict = {}
    monkeypatch.setattr(monitor, "get_slot_at",
                        lambda profil, dt: dict(etat.get(profil, _slot(modes.FREE))))
    return etat


def _modes(action_queue, profil="alice"):
    return [args["mode"] for action, args in action_queue
            if action == "apply_pihole_mode" and args["profile"] == profil]


# ------------------------------------------------------------------ #
#  Changements de plage                                               #
# ------------------------------------------------------------------ #

@pytest.mark.parametrize("avant,apres,cle", [
    (modes.OFF, modes.FREE, "event.mode_change"),
    (modes.FREE, modes.OFF, "event.access_closed"),
])
def test_un_changement_de_plage_est_applique_et_annonce(moniteur, db, action_queue,
                                                        planning, avant, apres, cle):
    """Défaut reproduit (P0-1) : la première vraie transition levait une NameError."""
    planning["alice"] = _slot(avant)
    moniteur.run_cycle()
    planning["alice"] = _slot(apres, "16:00", "18:00")
    moniteur.run_cycle()
    assert _modes(action_queue) == [avant, apres]
    cles = [e["message_key"] for e in db.get_events_for_profile("alice", limit=20)]
    assert any(k.startswith(cle) for k in cles)


def test_minuit_entre_deux_plages_libres_ne_change_rien(moniteur, db, config, action_queue,
                                                       monkeypatch):
    """Vendredi 22:00-23:59 puis samedi 00:00-01:00, libres tous les deux : le mode ne
    change pas à minuit, donc ni événement ni réapplication (23:59 = fin de journée)."""
    import monitor
    import scheduler
    config.data["profiles"]["alice"]["schedule"] = {
        "friday": [{"start": "22:00", "end": "23:59", "mode": modes.FREE}],
        "saturday": [{"start": "00:00", "end": "01:00", "mode": modes.FREE}]}
    config.save()
    horloge = [datetime(2026, 10, 2, 23, 58)]
    monkeypatch.setattr(monitor, "get_slot_at",
                        lambda profil, dt: scheduler.get_slot_at(profil, horloge[0]))
    for instant in (datetime(2026, 10, 2, 23, 58), datetime(2026, 10, 2, 23, 59, 59),
                    datetime(2026, 10, 3, 0, 0, 1)):
        horloge[0] = instant
        moniteur.run_cycle()
    assert _modes(action_queue) == [modes.FREE]
    assert not [e for e in db.get_events_for_profile("alice", limit=20)
                if (e["message_key"] or "").startswith(("event.mode_change", "event.access_"))]


def test_un_profil_en_erreur_n_empeche_pas_les_autres(moniteur, action_queue, planning,
                                                      monkeypatch):
    original = type(moniteur)._check_schedule

    def en_panne(self, profile_key, profile, active_ips):
        if profile_key == "alice":
            raise RuntimeError("planning illisible")
        return original(self, profile_key, profile, active_ips)

    monkeypatch.setattr(type(moniteur), "_check_schedule", en_panne)
    moniteur.run_cycle()
    assert _modes(action_queue, "bruno") == [modes.FREE]


def test_l_etat_est_reapplique_meme_sans_changement(moniteur, action_queue, monkeypatch):
    """Rattrape une action perdue ou un Pi-hole indisponible au moment du changement."""
    import monitor
    horloge = [1000.0]
    monkeypatch.setattr(monitor.time, "monotonic", lambda: horloge[0])
    moniteur.run_cycle()
    horloge[0] += 5 * 60
    moniteur.run_cycle()
    assert len(_modes(action_queue)) == 1
    horloge[0] += monitor.REAPPLY_INTERVAL_SEC
    moniteur.run_cycle()
    assert len(_modes(action_queue)) == 2


def test_un_appareil_revenu_sur_la_meme_adresse_est_reautorise(moniteur, config,
                                                               action_queue):
    """Défaut constaté sur boîtier : le runner retire l'autorisation d'une station qui se
    réassocie ; revenue sur la même adresse, elle restait bloquée des heures."""
    appareils = config.data["profiles"]["alice"]["devices"]
    moniteur.run_cycle()
    moniteur.config["profiles"]["alice"]["devices"] = []
    moniteur.run_cycle()
    moniteur.config["profiles"]["alice"]["devices"] = appareils
    moniteur.run_cycle()
    assert len(_modes(action_queue)) == 2


def test_un_blocage_manuel_n_est_pas_rouvert_par_le_cycle(moniteur, config, action_queue,
                                                          monkeypatch):
    import claude_agent
    monkeypatch.setattr(claude_agent, "_queue_action", lambda action, args: None)
    monkeypatch.setattr(claude_agent, "notify_monitor", lambda: None)
    claude_agent._execute_parent_tool("block_device_now", {"profile": "alice", "reason": "x"},
                                      config.data)
    moniteur.run_cycle()
    assert _modes(action_queue) == [modes.OFF]


@pytest.mark.parametrize("etat,reapplique", [("ok", True), ("degraded", True),
                                             ("error", False)])
def test_un_effecteur_arme_recoit_les_regles(moniteur, tmp_path, etat, reapplique):
    """« degraded » (armé sans le blocage DoH) coupe toujours par appareil : il doit
    recevoir les règles comme « ok »."""
    moniteur._enforcement_stamp = "avant"
    moniteur._last_slot = {"alice": (modes.FREE, False)}
    (tmp_path / "gateway_status.json").write_text(json.dumps(
        {"state": etat, "updated_at": "maintenant"}))
    moniteur._check_enforcement_rearm()
    assert (moniteur._last_slot == {}) is reapplique


# ------------------------------------------------------------------ #
#  L'escalade IA ne retient pas le cycle                              #
# ------------------------------------------------------------------ #

@pytest.fixture
def analyse_lente(monkeypatch):
    import claude_agent
    liberer, lots = threading.Event(), []

    def lente(evenements):
        lots.append(list(evenements))
        liberer.wait(5)

    monkeypatch.setattr(claude_agent, "analyze_unusual_patterns", lente)
    yield liberer, lots
    liberer.set()


def test_l_escalade_rend_la_main_et_n_a_qu_un_appel_en_vol(moniteur, analyse_lente):
    liberer, lots = analyse_lente
    moniteur._unusual_events = [{"n": 1}]
    debut = time.monotonic()
    moniteur._escalate_to_claude()
    assert time.monotonic() - debut < 0.5
    premier = moniteur._escalation_thread
    moniteur._unusual_events = [{"n": 2}]
    moniteur._escalate_to_claude()                  # le premier appel est encore en vol
    assert moniteur._unusual_events == [{"n": 2}]   # reporté, pas perdu
    liberer.set()
    premier.join(2)
    moniteur._escalate_to_claude()
    moniteur._escalation_thread.join(2)
    assert lots == [[{"n": 1}], [{"n": 2}]]


# ------------------------------------------------------------------ #
#  Tentatives de contournement refusées                               #
# ------------------------------------------------------------------ #

@pytest.fixture
def compteurs(moniteur, tmp_path):
    def publier(generation, counters):
        (tmp_path / "bypass_counters.json").write_text(json.dumps(
            {"generation": generation, "counters": counters}))
        moniteur._check_bypass_attempts()
    return publier


def _tentatives(db):
    return [e["message_key"] for e in db.get_recent_events(limit=50)
            if e["message_key"].startswith("event.bypass_attempt")]


def test_une_hausse_signale_une_fois_par_appareil_type_et_jour(moniteur, compteurs, db):
    compteurs("g1", {IP_A: {"dot": 4}})                 # première lecture : référence
    assert _tentatives(db) == []
    for n in (6, 9):
        compteurs("g1", {IP_A: {"dot": n}})
    compteurs("g1", {IP_A: {"dot": 9, "doh": 1}})
    assert sorted(_tentatives(db)) == ["event.bypass_attempt_doh", "event.bypass_attempt_dot"]
    hier = (datetime.now() - timedelta(days=1)).date().isoformat()
    moniteur._bypass_attempt_alerted = {k: hier for k in moniteur._bypass_attempt_alerted}
    compteurs("g1", {IP_A: {"dot": 10, "doh": 1}})
    assert len(_tentatives(db)) == 3


@pytest.mark.parametrize("apres,attendu", [(0, 0), (2, 1)])
def test_une_remise_a_zero_n_est_pas_une_tentative(compteurs, db, apres, attendu):
    """Chaîne reconstruite : la référence repart de zéro, sans faux événement."""
    compteurs("g1", {IP_A: {"dot": 12}})
    compteurs("g2", {IP_A: {"dot": apres}})
    assert len(_tentatives(db)) == attendu


def test_rien_n_est_signale_en_mode_dns(moniteur, compteurs, db):
    moniteur.config["network"]["enforcement"] = "dns_only"
    compteurs("g1", {IP_A: {"dot": 1}})
    compteurs("g1", {IP_A: {"dot": 5}})
    assert _tentatives(db) == []


# ------------------------------------------------------------------ #
#  Tunnel probable (VPN) : signalé, jamais coupé                      #
# ------------------------------------------------------------------ #
#  Seuils EXTRAPOLÉS, non calibrés sur un vrai VPN (cf. monitor.VPN_*). Ces tests
#  vérifient la logique, pas la justesse des valeurs.

def _releves(tmp_path, echantillons):
    (tmp_path / "flow_stats.json").write_text(json.dumps(
        {"window_sec": 3600, "samples": {IP_A: echantillons}}))


def _heure(octets, share, dst="203.0.113.9", n=15):
    """`n` relevés d'une minute (15 : la fenêtre entière)."""
    maintenant = time.time()
    return [{"t": maintenant - 60 * i, "bytes": octets, "top_dst": dst, "top_share": share}
            for i in range(n)]


def _vpn(db):
    return [e for e in db.get_recent_events(limit=50) if e["message_key"] == "event.vpn_suspected"]


@pytest.mark.parametrize("echantillons,dns,signale", [
    # Un quart d'heure concentré sur une adresse, sans DNS : la signature d'un tunnel.
    (_heure(2_000_000, 0.97), 0, True),
    # Téléphone en veille : une connexion de notifications, trop peu d'octets.
    (_heure(20_000, 1.0), 0, False),
    # Vidéo en continu : concentrée aussi, mais l'appareil résout des noms variés.
    (_heure(5_000_000, 0.95), 40, False),
    # Mesuré sur le boîtier, iPhone en VPN : 13 requêtes, toutes vers la vérification de
    # connectivité d'iOS, hors du tunnel. Un seul nom : c'est bien un tunnel.
    (_heure(5_000_000, 1.0), -13, True),
    # Navigation ordinaire : destinations dispersées.
    (_heure(2_000_000, 0.4), 0, False),
    # Une rafale de dix minutes ne fait pas un quart d'heure.
    (_heure(2_000_000, 0.97, n=10), 0, False),
])
def test_un_tunnel_probable_est_signale(moniteur, db, tmp_path, echantillons, dns, signale):
    _releves(tmp_path, echantillons)
    if dns:
        # dns > 0 : autant de noms DIFFÉRENTS ; dns < 0 : un seul nom, répété.
        noms = ([f"d{i}.exemple.fr" for i in range(dns)] if dns > 0
                else ["netcts.cdn-apple.com"] * -dns)
        moniteur._note_dns({IP_A: noms})
    moniteur._check_vpn_suspects()
    assert bool(_vpn(db)) is signale


def test_un_tunnel_ouvre_une_session_qui_dure_puis_se_ferme(moniteur, db, tmp_path,
                                                             monkeypatch):
    """Demandé par le mainteneur : « actif » tant que le tunnel dure, puis « utilisé de
    telle heure à telle heure ». Un seul événement au journal pour toute la session."""
    import monitor
    debut = time.time()
    horloge = {"t": debut}
    monkeypatch.setattr(monitor.time, "time", lambda: horloge["t"])
    _releves(tmp_path, _heure(2_000_000, 0.97))
    for _ in range(3):
        moniteur._check_vpn_suspects()
    (session,) = db.get_vpn_sessions()
    assert session["active"] and session["ip"] == IP_A and session["profile"] == "alice"
    assert session["started_at"] < session["last_seen"]       # quinze relevés d'écart
    # Le tunnel continue : un relevé de plus prolonge la session, sans second événement.
    horloge["t"] = debut + 60
    _releves(tmp_path, _heure(2_000_000, 0.97)[:14] + [
        {"t": debut + 60, "bytes": 2_000_000, "top_dst": "203.0.113.9", "top_share": 1.0}])
    moniteur._check_vpn_suspects()
    (session,) = db.get_vpn_sessions()
    assert session["active"] and session["last_seen"] > session["started_at"]
    # Plus aucun relevé : la session se ferme à son dernier relevé, et reste montrée.
    horloge["t"] = debut + 60 + monitor.VPN_END_GAP_SEC + 1
    _releves(tmp_path, [])
    moniteur._check_vpn_suspects()
    (session,) = db.get_vpn_sessions()
    assert not session["active"] and session["ended_at"] == session["last_seen"]
    assert len(_vpn(db)) == 1


def test_une_session_terminee_n_est_plus_montree_apres_48_heures(db):
    from datetime import datetime, timedelta
    vieux = (datetime.now() - timedelta(hours=49)).isoformat(timespec="seconds")
    sid = db.open_vpn_session("alice", IP_A, "203.0.113.9", vieux, vieux)
    db.close_vpn_session(sid, vieux)
    assert db.get_vpn_sessions() == []


def test_aucun_tunnel_signale_en_mode_dns(moniteur, db, tmp_path):
    moniteur.config["network"]["enforcement"] = "dns_only"
    _releves(tmp_path, _heure(2_000_000, 0.97))
    moniteur._check_vpn_suspects()
    assert _vpn(db) == []


# ------------------------------------------------------------------ #
#  Mémoire bornée (P3-4)                                              #
# ------------------------------------------------------------------ #
#  Trois mémoires du moniteur ne perdaient jamais une entrée : sur un boîtier qui tourne
#  des mois, elles grossissaient d'une entrée par domaine bloqué, par service signalé
#  et par domaine classé.

def test_le_moniteur_oublie_ce_qui_ne_sert_plus(moniteur):
    import monitor
    maintenant = datetime(2026, 10, 1, 12, 0)
    hier = (maintenant - timedelta(days=1)).date().isoformat()
    moniteur._unusual_seen |= {("alice", "jeu", hier), ("alice", "jeu", "2026-10-01")}
    moniteur._block_state.update({
        ("alice", "vieux.fr"): {"last_seen": maintenant - timedelta(days=2)},
        ("alice", "recent.fr"): {"last_seen": maintenant - timedelta(minutes=5)}})
    moniteur._cf_asked.update({
        "vieux.fr": maintenant - timedelta(seconds=monitor.CF_IMMEDIATE_WINDOW_SEC + 1),
        "recent.fr": maintenant - timedelta(minutes=5)})

    moniteur._purge_memory(maintenant)

    assert moniteur._unusual_seen == {("alice", "jeu", "2026-10-01")}
    assert set(moniteur._block_state) == {("alice", "recent.fr")}
    assert set(moniteur._cf_asked) == {"recent.fr"}


# ------------------------------------------------------------------ #
#  Volume Internet par enfant et par jour                             #
# ------------------------------------------------------------------ #
#  Demandé après un rapport qui jugeait « activité principalement système » une journée
#  passée en VPN : un tunnel ne produit presque aucune requête DNS, seul le volume le dit.

def test_le_volume_du_jour_se_cumule_par_enfant_sans_double_compte(moniteur, db, config, tmp_path):
    import monitor
    maintenant = time.time()
    releves = [{"t": maintenant - 60 * i, "bytes": 1_000_000, "top_dst": "203.0.113.9",
                "top_share": 0.5} for i in range(3)]
    _releves(tmp_path, releves)
    jour = datetime.fromtimestamp(maintenant).date().isoformat()
    moniteur._record_traffic()
    moniteur._record_traffic()                       # même fenêtre relue au cycle suivant
    assert db.get_daily_traffic("alice", jour) == 3_000_000
    # Un agent redémarré relit la même fenêtre de 15 min : rien n'est recompté.
    neuf = monitor.ProtectadoMonitor(config.path)
    neuf._record_traffic()
    assert db.get_daily_traffic("alice", jour) == 3_000_000
    assert db.get_daily_traffic("bruno", jour) is None   # inconnu, pas zéro


def test_une_session_de_tunnel_porte_son_volume(moniteur, db, tmp_path, monkeypatch):
    import monitor
    debut = time.time()
    horloge = {"t": debut}
    monkeypatch.setattr(monitor.time, "time", lambda: horloge["t"])
    fenetre = _heure(2_000_000, 0.97)
    _releves(tmp_path, fenetre)
    moniteur._check_vpn_suspects()
    (session,) = db.get_vpn_sessions()
    assert session["bytes"] == 15 * 2_000_000
    horloge["t"] = debut + 60
    _releves(tmp_path, fenetre[:14] + [
        {"t": debut + 60, "bytes": 3_000_000, "top_dst": "203.0.113.9", "top_share": 1.0}])
    moniteur._check_vpn_suspects()
    (session,) = db.get_vpn_sessions()
    assert session["bytes"] == 15 * 2_000_000 + 3_000_000
