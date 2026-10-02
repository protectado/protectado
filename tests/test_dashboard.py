# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Arnaud Ortais
# Dual-licensed: AGPL-3.0 (open source) or Commercial License — see LICENSE and LICENSE-COMMERCIAL.
"""
test_dashboard.py — le tableau de bord, appelé par sa vraie pile HTTP.

Fixture `client` : session parent ouverte, moniteur remplacé par un double. Les tests
qui éprouvent le démarrage de l'application construisent la leur.
"""

import json
import time
from datetime import datetime, timedelta
from types import SimpleNamespace as NS

import pytest

import modes

IP = "192.168.50.52"
PLUS_TARD = lambda: (datetime.now() + timedelta(hours=1)).isoformat()   # noqa: E731


def _evenement(db, cle, domaine=IP, quand=None, profil="alice", type_="warning"):
    with db.get_db() as conn:
        conn.execute("INSERT INTO events (timestamp, profile, type, domain, message, "
                     "message_key, params) VALUES (?,?,?,?,?,?,?)",
                     ((quand or datetime.now()).isoformat(), profil, type_, domaine, "",
                      cle, "{}"))


# ------------------------------------------------------------------ #
#  « À regarder » : les alertes, et elles seules                      #
# ------------------------------------------------------------------ #
#  La colonne `type` vaut « warning » pour les alertes comme pour chaque tentative
#  bloquée. Or un blocage est le produit qui fonctionne : les mélanger remplirait la
#  carte d'un bruit permanent, et une carte toujours pleine ne se regarde plus.

def test_seules_les_alertes_du_produit_sont_retenues(db):
    for _ in range(30):
        _evenement(db, "event.blocked_attempt", "exemple.fr")
    _evenement(db, "event.mode_change", "", type_="info")
    assert db.get_alerts() == []
    _evenement(db, "event.dns_bypass_suspected")
    _evenement(db, "event.station_unknown_key", "02:aa:bb:cc:dd:09", profil="global")
    # La tentative refusée n'est pas une alerte ; le tunnel est montré par ses sessions.
    _evenement(db, "event.bypass_attempt_doh", "192.168.50.85", type_="info")
    assert {a["message_key"] for a in db.get_alerts()} == {
        "event.dns_bypass_suspected", "event.station_unknown_key"}
    assert len(db.get_recent_events(limit=50)) == 34          # le journal garde tout


@pytest.mark.parametrize("age_h,fenetre,retenue", [(50, None, False), (46, None, True),
                                                   (10, 4, False), (10, 24, True)])
def test_une_alerte_sort_d_elle_meme_de_la_fenetre(db, age_h, fenetre, retenue):
    """Réémise tant que la cause persiste : pas besoin de bouton « vu », qui permettrait
    de masquer une cause toujours active."""
    _evenement(db, "event.dns_bypass_suspected", quand=datetime.now() - timedelta(hours=age_h))
    alertes = db.get_alerts(hours=fenetre) if fenetre else db.get_alerts()
    assert bool(alertes) is retenue


def test_une_ligne_par_appareil_datee_de_sa_derniere_occurrence(db):
    recent = datetime.now() - timedelta(hours=1)
    for heures in (20, 12, 6):
        _evenement(db, "event.dns_bypass_suspected", quand=datetime.now() - timedelta(hours=heures))
    _evenement(db, "event.dns_bypass_suspected", quand=recent)
    _evenement(db, "event.dns_bypass_suspected", "192.168.50.53")
    alertes = {a["domain"]: a for a in db.get_alerts()}
    assert set(alertes) == {IP, "192.168.50.53"}
    assert alertes[IP]["count"] == 4
    assert alertes[IP]["timestamp"][:16] == recent.isoformat()[:16]


def test_le_nombre_d_alertes_est_borne(db):
    for i in range(40):
        _evenement(db, "event.dns_bypass_suspected", f"192.168.50.{i + 10}")
    assert len(db.get_alerts()) == 10


def test_les_alertes_voyagent_avec_le_statut(client, db):
    assert client.get("/api/status").json()["alerts"] == []
    _evenement(db, "event.dns_bypass_suspected")
    assert [a["message_key"] for a in client.get("/api/status").json()["alerts"]] == [
        "event.dns_bypass_suspected"]


# ------------------------------------------------------------------ #
#  Le mot de passe de Pi-hole                                         #
# ------------------------------------------------------------------ #

def test_le_mot_de_passe_pihole_derriere_la_session_seulement(client, config, monkeypatch):
    import dashboard
    assert client.get("/api/pihole/credentials").json()["password"] == \
        config.data["pihole"]["password"]
    infos = client.get("/api/network/info").json()
    assert config.data["pihole"]["password"] not in str(infos)
    assert infos["pihole_host"] == config.data["pihole"]["host"].rstrip("/")
    monkeypatch.setattr(dashboard, "_check_session", lambda request: False)
    assert client.get("/api/pihole/credentials").status_code == 401


def test_une_configuration_sans_pihole_ne_leve_pas(client, config):
    config.data.pop("pihole", None)
    config.save()
    assert client.get("/api/pihole/credentials").json() == {"ok": True, "password": ""}
    assert client.get("/api/network/info").json()["pihole_host"] == ""


# ------------------------------------------------------------------ #
#  Les exceptions en cours, et de quoi les reprendre                  #
# ------------------------------------------------------------------ #

def test_les_cinq_sortes_d_exception_apparaissent_avec_leur_cle(client, db, config):
    import scheduler
    assert client.get("/api/exceptions").json() == {"ok": True, "items": []}
    aujourdhui = datetime.now().date().isoformat()
    scheduler.set_temp_override("alice", modes.FREE, 30)
    db.set_schedule_override("bruno", aujourdhui, modes.OFF, "punition")
    db.set_slot_extension("alice", 20, aujourdhui, "00:00")
    db.set_temp_domain_unblock("exemple.fr", "alice", PLUS_TARD())
    db.set_device_override("192.168.50.53", 60)
    items = client.get("/api/exceptions").json()["items"]
    requis = {"temp_mode": ("profile",), "day_mode": ("profile", "date"),
              "slot_extension": ("profile",), "domain": ("domain",), "device": ("ip",)}
    assert {i["kind"] for i in items} == set(requis)
    for item in items:
        assert all(item.get(c) for c in requis[item["kind"]]), item["kind"]
    temp = next(i for i in items if i["kind"] == "temp_mode")
    assert (temp["profile"], temp["name"]) == ("alice", "Alice")


def test_une_exception_echue_n_est_plus_montree(client, db):
    hier = (datetime.now() - timedelta(days=1)).date().isoformat()
    avant_hier = (datetime.now() - timedelta(days=2)).date().isoformat()
    db.set_schedule_override("alice", hier, modes.OFF)
    db.set_slot_extension("alice", 20, avant_hier, "00:00")
    db.set_temp_domain_unblock("exemple.fr", "alice",
                               (datetime.now() - timedelta(minutes=5)).isoformat())
    assert client.get("/api/exceptions").json()["items"] == []


def test_le_temps_restant_est_calcule(client, db):
    db.set_temp_domain_unblock("exemple.fr", "", (datetime.now() + timedelta(minutes=55)).isoformat())
    assert 53 <= client.get("/api/exceptions").json()["items"][0]["minutes_left"] <= 55


def test_une_rallonge_se_reprend_et_ne_journalise_que_si_elle_existait(client, db):
    avant = len(db.get_recent_events(limit=100))
    assert client.delete("/api/slot-extension/alice").json()["ok"]
    assert len(db.get_recent_events(limit=100)) == avant
    db.set_slot_extension("alice", 20, datetime.now().date().isoformat(), "00:00")
    assert client.delete("/api/slot-extension/alice").json()["ok"]
    assert db.get_slot_extension("alice") == (0, "", "")
    assert len(db.get_recent_events(limit=100)) == avant + 1


def test_un_domaine_se_referme_pour_l_enfant_designe_et_resynchronise(client, db,
                                                                       monkeypatch):
    import claude_agent
    syncs: list = []
    monkeypatch.setattr(claude_agent, "_sync_pihole_blacklists", lambda cfg: syncs.append(1))
    for profil in ("alice", "bruno"):
        db.set_temp_domain_unblock("jeu.fr", profil, PLUS_TARD())
    assert client.delete("/api/temp-domain/jeu.fr?profile=alice").json()["ok"]
    assert [r["profile"] for r in db.get_temp_domain_unblocks()] == ["bruno"] and syncs


@pytest.mark.parametrize("appel", ["/api/temp-domain/pas-un-domaine",
                                   "/api/slot-extension/Majuscule!"])
def test_une_cle_d_annulation_invalide_est_refusee(client, appel):
    assert client.delete(appel).status_code == 400


def test_les_exceptions_exigent_une_session(client, monkeypatch):
    import dashboard
    monkeypatch.setattr(dashboard, "_check_session", lambda request: False)
    for reponse in (client.get("/api/exceptions"), client.delete("/api/slot-extension/alice"),
                    client.delete("/api/temp-domain/exemple.fr")):
        assert reponse.status_code == 401


# ------------------------------------------------------------------ #
#  Plannings et dérogations validés à l'entrée                        #
# ------------------------------------------------------------------ #

def _plage(start="08:00", end="10:00", mode=modes.FREE):
    return {"start": start, "end": end, "mode": mode}


@pytest.mark.parametrize("schedule,code", [
    ({"funday": [_plage()]}, "schedule_bad_day"),
    ({"monday": "08:00-10:00"}, "schedule_bad_day"),
    ({"monday": [_plage(start="25:00")]}, "schedule_bad_time"),
    ({"monday": [{"end": "10:00", "mode": modes.FREE}]}, "schedule_bad_time"),
    ({"monday": [_plage(start="10:00", end="10:00")]}, "schedule_bad_order"),
    ({"monday": [_plage(start="22:00", end="07:00")]}, "schedule_crosses_midnight"),
    ({"monday": [_plage(mode="normal")]}, "schedule_bad_mode"),
    ({"monday": [_plage("08:00", "12:00"), _plage("11:00", "13:00")]}, "schedule_overlap"),
])
def test_un_planning_invalide_est_refuse_sans_rien_ecrire(client, config, schedule, code):
    avant = open(config.path).read()
    r = client.post("/api/profiles/alice", json={"name": "Alice", "schedule": schedule})
    assert (r.status_code, r.json()["error_code"]) == (400, code)
    assert open(config.path).read() == avant


def test_un_planning_valide_est_accepte(client):
    schedule = {"monday": [_plage("00:00", "08:00", modes.OFF), _plage("08:00", "23:59")],
                "weekend": [_plage(mode="permissive")]}
    assert client.post("/api/profiles/alice", json={"name": "Alice",
                                                    "schedule": schedule}).status_code == 200


@pytest.mark.parametrize("profil,date,code", [("inconnu", "2026-10-01", "invalid_profile"),
                                              ("alice", "demain", "invalid_date")])
def test_une_derogation_mal_formee_est_refusee(client, db, profil, date, code):
    r = client.post("/api/overrides", json={"profile": profil, "date": date, "mode": modes.OFF})
    assert (r.status_code, r.json()["error_code"]) == (400, code)
    assert db.get_schedule_overrides() == []


# ------------------------------------------------------------------ #
#  Supprimer un profil ne laisse rien derrière lui                    #
# ------------------------------------------------------------------ #

@pytest.fixture
def forward(monkeypatch):
    import access_control
    poses: list = []
    monkeypatch.setattr(access_control, "_queue_forward_action",
                        lambda ips, *, blocked, profile, reason: poses.append(
                            (list(ips), blocked, reason)) or True)
    return poses


def test_la_suppression_nettoie_etat_groupes_et_appareils(client, db, config, pihole, forward):
    import scheduler
    aujourdhui = datetime.now().date().isoformat()
    db.set_schedule_override("alice", aujourdhui, modes.OFF, "test")
    scheduler.set_temp_override("alice", modes.FREE, 30)
    db.set_slot_extension("alice", 20, aujourdhui, "00:00")
    db.set_domain_rule("alice", "jeu.fr", modes.FREE, allow=False)
    db.set_domain_rule("", "global.fr", modes.FREE, allow=False)
    db.set_temp_domain_unblock("video.fr", "alice", PLUS_TARD())
    db.set_device_override(IP, 60)

    assert client.delete("/api/profiles/alice").json()["ok"]
    assert "alice" not in json.load(open(config.path))["profiles"]
    assert ("assign_client_to_group", (IP, "Default"), {}) in pihole.calls
    assert ("delete_profile_groups", ("alice",), {}) in pihole.calls
    reste = (db.get_schedule_overrides(), db.get_temp_overrides(), db.get_temp_domain_unblocks(),
             db.get_device_override(IP), db.get_slot_extension("alice"))
    assert reste == ([], [], [], None, (0, "", ""))
    assert [r["domain"] for r in db.all_domain_rules()] == ["global.fr"]
    assert client.moniteur.notified >= 1
    # En passerelle, un appareil que plus aucun profil ne reconnaît ne sort pas.
    assert forward == [([IP], True, "profil_supprimé")]


def test_en_mode_dns_l_appareil_redevient_ordinaire(client, config, pihole, forward):
    config.data["network"]["enforcement"] = "dns_only"
    config.save()
    client.delete("/api/profiles/alice")
    assert ("assign_client_to_group", (IP, "Default"), {}) in pihole.calls and forward == []


def test_purger_l_historique_exige_le_mot_de_passe(client, config, forward):
    for corps in (None, {"password": "faux"}):
        r = client.request("DELETE", "/api/profiles/alice?purge_history=true", json=corps)
        assert r.status_code == 403
    assert "alice" in json.load(open(config.path))["profiles"]
    r = client.request("DELETE", "/api/profiles/alice?purge_history=true",
                       json={"password": config.data["dashboard_password"]})
    assert r.status_code == 200 and r.json()["purged"] is not None


# ------------------------------------------------------------------ #
#  Un seul moniteur, démarré avec l'application                       #
# ------------------------------------------------------------------ #
#  Défaut reproduit (P0-2) : uvicorn.run("dashboard:app") réimportait le module sous un
#  autre nom, et la première requête créait un second moniteur.

class _FauxMoniteur:
    construits: list = []

    def __init__(self):
        type(self).construits.append(self)
        self.config = {"profiles": {}}
        self.pihole = type("P", (), {"setup_profiles": lambda self, p: True})()
        self.demarre, self.rechargements, self.arrete = 0, 0, False

    def start(self, interval=60):
        self.demarre += 1

    def stop(self):
        self.arrete = True

    def reload_config(self):
        self.rechargements += 1


@pytest.fixture
def appli(db, config, monkeypatch):
    import dashboard
    _FauxMoniteur.construits = []
    monkeypatch.setattr(dashboard, "ProtectadoMonitor", _FauxMoniteur)
    monkeypatch.setattr(dashboard, "monitor", None)
    monkeypatch.setattr(dashboard, "_queue", lambda action, args: None)
    return dashboard


def test_un_seul_moniteur_demarre_avec_l_application(appli, config):
    from fastapi.testclient import TestClient
    with TestClient(appli.app):
        (premier,) = _FauxMoniteur.construits
        assert all(appli.get_monitor() is premier for _ in range(3)) and premier.demarre == 1
        appli._save_config(config.data)
        assert premier.rechargements == 1
    assert premier.arrete and len(_FauxMoniteur.construits) == 1


def test_sans_moniteur_une_requete_repond_503_et_n_en_cree_pas(appli, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(appli, "_check_session", lambda request: True)
    r = TestClient(appli.app).delete("/api/overrides/alice/2026-09-30")
    assert (r.status_code, r.json()["error_code"]) == (503, "monitor_unavailable")
    assert _FauxMoniteur.construits == []


def test_pas_de_moniteur_pendant_l_assistant(appli, config):
    from fastapi.testclient import TestClient
    config.data["configured"] = False
    config.save()
    with TestClient(appli.app):
        assert _FauxMoniteur.construits == []


# ------------------------------------------------------------------ #
#  La clé Wi-Fi d'un enfant, enregistrée seule                        #
# ------------------------------------------------------------------ #

@pytest.fixture
def file_capturee(monkeypatch):
    import dashboard
    actions: list = []
    monkeypatch.setattr(dashboard, "_queue", lambda action, args: actions.append(action))
    return actions


def test_la_cle_d_un_enfant_s_enregistre_seule(client, config, file_capturee):
    """Signalé en test : le bloc de la clé n'avait aucun bouton d'enregistrement."""
    r = client.post("/api/profiles/alice/wifi-key", json={"wifi_key": "nouvelle-cle-alice-2026"})
    assert r.status_code == 200 and r.json()["wifi_key"] == "nouvelle-cle-alice-2026"
    enregistre = json.load(open(config.path))["profiles"]["alice"]
    assert enregistre["wifi_key"] == "nouvelle-cle-alice-2026"
    assert enregistre["schedule"] == config.data["profiles"]["alice"]["schedule"]
    assert file_capturee == ["apply_wifi_keys"]


@pytest.mark.parametrize("profil,cle,code", [
    ("alice", "court", "wifi_key_invalid"),
    ("alice", "dorali-tamuse-vakino-318", "wifi_key_duplicate"),   # celle de bruno
    ("monitoring", "une-cle-valide-2026", "invalid_profile_key"),
    ("inconnu", "une-cle-valide-2026", "invalid_profile_key"),
])
def test_une_cle_refusee_ne_touche_a_rien(client, config, file_capturee, profil, cle, code):
    avant = open(config.path).read()
    r = client.post(f"/api/profiles/{profil}/wifi-key", json={"wifi_key": cle})
    assert (r.status_code, r.json()["error_code"]) == (400, code)
    assert open(config.path).read() == avant and file_capturee == []


def test_deux_enfants_ne_peuvent_pas_partager_une_cle(client, config, file_capturee):
    """La clé identifie l'enfant : partagée, l'appareil du second serait rattaché au
    premier profil, avec ses horaires et sa grille."""
    profil = dict(config.data["profiles"]["bruno"], wifi_key="bamito-kesuna-porelu-472")
    r = client.post("/api/profiles/bruno", json={
        "name": profil["name"], "schedule": profil["schedule"], "wifi_key": profil["wifi_key"]})
    assert (r.status_code, r.json()["error_code"]) == (400, "wifi_key_duplicate")


# ------------------------------------------------------------------ #
#  Fichiers statiques sous la sandbox                                 #
# ------------------------------------------------------------------ #

def test_un_fichier_statique_est_servi_sans_lire_les_types_du_systeme(client, monkeypatch):
    """Défaut constaté sur boîtier : /static/keystrength.js répondait 500, la sandbox
    refusant la lecture de /etc/mime.types que Python consulte au premier fichier servi."""
    import mimetypes
    import dashboard

    def refuse(self, filename, strict=True):
        raise PermissionError(13, "Permission denied", filename)

    monkeypatch.setattr(mimetypes, "inited", False)
    monkeypatch.setattr(mimetypes, "_db", None)
    monkeypatch.setattr(mimetypes.MimeTypes, "read", refuse)
    monkeypatch.setattr(mimetypes, "knownfiles", [__file__])
    dashboard._init_mimetypes()
    r = client.get("/static/keystrength.js")
    assert r.status_code == 200 and "javascript" in r.headers["content-type"]


def test_une_migration_en_echec_voyage_avec_le_statut(client, config, tmp_path):
    assert client.get("/api/status").json()["maintenance"]["state"] == ""
    (tmp_path / "maintenance.json").write_text(json.dumps(
        {"state": "error", "detail": "0001-paquets.sh en échec : réseau", "pending": ["0001-paquets.sh"]}))
    maintenance = client.get("/api/status").json()["maintenance"]
    assert maintenance["state"] == "error" and "0001-paquets.sh" in maintenance["detail"]


def test_les_appareils_deja_vus_d_un_enfant_sont_donnes_avec_leur_derniere_visite(client, db):
    from datetime import datetime, timedelta
    hier = (datetime.now() - timedelta(days=1)).replace(hour=18, minute=5, second=0, microsecond=0)
    db.record_seen_devices("alice", [{"mac": "02:aa:bb:cc:dd:01", "hostname": "tablette",
                                      "ip": IP}], quand=hier.isoformat())
    profils = client.get("/api/profiles").json()
    (vu,) = profils["alice"]["seen_devices"]
    assert (vu["name"], vu["mac"]) == ("tablette", "02:aa:bb:cc:dd:01")
    assert (vu["seen"]["day_offset"], vu["seen"]["time"]) == (1, "18:05")
    assert profils["bruno"]["seen_devices"] == []
    assert "seen_devices" not in profils["monitoring"]


def test_supprimer_un_profil_oublie_ses_appareils_vus(client, db, forward):
    db.record_seen_devices("alice", [{"mac": "02:aa:bb:cc:dd:01", "hostname": "t", "ip": IP}])
    client.delete("/api/profiles/alice")
    assert db.get_seen_devices("alice") == []


def test_effacer_l_historique_efface_aussi_les_visites_des_appareils(client, db, config):
    db.record_seen_devices("alice", [{"mac": "02:aa:bb:cc:dd:01", "hostname": "t", "ip": IP}])
    r = client.post("/api/profiles/alice/purge-history",
                    json={"password": config.data["dashboard_password"]})
    assert r.status_code == 200 and db.get_seen_devices("alice") == []


# ------------------------------------------------------------------ #
#  Page Réseau : un appareil d'enfant déconnecté reste à son enfant   #
# ------------------------------------------------------------------ #

def test_un_appareil_d_enfant_deconnecte_n_est_pas_presente_comme_celui_du_parent(
        client, db, pihole):
    """Signalé en test : en passerelle, un appareil d'enfant déconnecté apparaissait
    rattaché à « Parent (non surveillé) », la valeur par défaut de la liste."""
    db.record_seen_devices("bruno", [{"mac": "02:aa:bb:cc:dd:09", "hostname": "console",
                                      "ip": "192.168.50.60"}])
    pihole.returns["get_network_devices"] = [
        {"ip": IP, "mac": "02:aa:bb:cc:dd:01", "hostname": "tablette"},
        {"ip": "192.168.50.60", "mac": "02:aa:bb:cc:dd:09", "hostname": "console"},
        {"ip": "192.168.50.70", "mac": "02:aa:bb:cc:dd:0a", "hostname": "inconnu"},
        {"ip": "192.168.0.30", "mac": "02:aa:bb:cc:dd:0b", "hostname": "imprimante"},
    ]
    data = client.get("/api/devices").json()
    par_ip = {d["ip"]: d for d in data["pihole"]}
    assert (par_ip[IP]["kids_network"], par_ip[IP]["connected"],
            par_ip[IP]["known_profile"]) == (True, True, "alice")
    assert (par_ip["192.168.50.60"]["connected"],
            par_ip["192.168.50.60"]["known_profile"]) == (False, "bruno")
    assert par_ip["192.168.50.70"]["known_profile"] is None
    assert par_ip["192.168.0.30"]["kids_network"] is False
    # Afficher la page ne modifie pas Pi-hole (P3-3) : elle inscrivait chaque appareil
    # inconnu comme client, une lecture de la liste et une écriture par appareil, à
    # chaque chargement.
    lus = [nom for nom, *_ in pihole.calls]
    assert "ensure_client_exists" not in lus and lus.count("get_clients") == 1


# ------------------------------------------------------------------ #
#  Le journal des décisions                                           #
# ------------------------------------------------------------------ #
#  Le journal des réglages ne garde que les DÉCISIONS humaines : le bruit machine
#  (vérifications automatiques d'un appareil bloquées) et la routine (changements de
#  plage) le rendaient illisible. Le détail d'une journée reste dans l'Historique.

def _cles_decisions(client):
    return [e["message_key"] for e in client.get("/api/decisions").json()]


def test_le_journal_ne_garde_que_les_decisions(client, db):
    for cle in ("event.blocked_attempt", "event.mode_change", "event.temp_override_ended",
                "event.adult_mode_ended", "event.domain_reblocked", "event.daily_report",
                "event.manual_block", "event.temp_override_started"):
        _evenement(db, cle, profil="alice", type_="info")
    assert sorted(_cles_decisions(client)) == ["event.manual_block",
                                               "event.temp_override_started"]


def test_tout_le_journal_montre_le_boitier_mais_pas_la_navigation(client, db):
    """Filtre « tout le journal » demandé par le mainteneur. Il ne doit pas devenir un
    accès à la navigation sans mot de passe : le site bloqué et son heure, ou un rapport,
    restent dans le détail d'une journée (protégé, et dont l'accès est journalisé)."""
    for cle in ("event.blocked_attempt", "event.blocked_attempt_multi", "event.daily_report",
                "event.weekly_review", "event.mode_change", "event.vpn_suspected",
                "event.bypass_attempt_doh", "event.manual_block", ""):
        _evenement(db, cle, profil="alice", type_="info")
    cles = {e["message_key"] for e in client.get("/api/decisions?scope=all").json()}
    assert cles == {"event.mode_change", "event.vpn_suspected", "event.bypass_attempt_doh",
                    "event.manual_block"}
    # Chaque ligne dit si c'est une décision : le flux en direct s'en sert pour filtrer.
    assert {e["message_key"] for e in client.get("/api/decisions?scope=all").json()
            if e["decision"]} == {"event.manual_block"}


@pytest.mark.parametrize("reglage,montre", [(None, False), ("journal", False),
                                            ("alert", True)])
def test_un_tunnel_detecte_n_est_affiche_que_si_le_parent_l_a_choisi(client, moniteur, db,
                                                                    config, tmp_path,
                                                                    reglage, montre):
    """Détection pas encore calibrée (un appel vidéo lui ressemble) : par défaut, au
    journal complet seulement, sans bandeau dans « À regarder »."""
    if reglage:
        assert client.post("/api/network/vpn-alerts",
                           json={"vpn_alerts": reglage}).json()["ok"] is True
        # Le statut lit la configuration du moniteur, que _save_config recharge en
        # production ; le double du moniteur, lui, la relit ici.
        client.moniteur.config = json.loads(open(config.path).read())
    maintenant = time.time()
    (tmp_path / "flow_stats.json").write_text(json.dumps({"samples": {IP: [
        {"t": maintenant - 60 * i, "bytes": 2_000_000, "top_dst": "203.0.113.9",
         "top_share": 0.97} for i in range(15)]}}))
    moniteur._check_vpn_suspects()
    journal = {e["message_key"] for e in client.get("/api/decisions?scope=all").json()}
    assert "event.vpn_suspected" in journal
    assert bool(client.get("/api/status").json()["vpn_sessions"]) is montre


def test_un_tunnel_termine_peut_etre_marque_comme_vu_pas_un_tunnel_en_cours(client, db):
    """Demandé par le mainteneur : « vu » veut dire que le parent a vu et va s'en occuper.
    Un tunnel EN COURS ne se masque pas : sa cause est toujours là."""
    maintenant = datetime.now().isoformat(timespec="seconds")
    en_cours = db.open_vpn_session("alice", IP, "203.0.113.9", maintenant, maintenant)
    fini = db.open_vpn_session("alice", IP, "203.0.113.9", maintenant, maintenant)
    db.close_vpn_session(fini, maintenant)

    assert client.post(f"/api/vpn-sessions/{en_cours}/ack").status_code == 409
    assert client.post(f"/api/vpn-sessions/{fini}/ack").json()["ok"] is True
    assert [v["id"] for v in db.get_vpn_sessions()] == [en_cours]
    assert "event.vpn_acknowledged" in _cles_decisions(client)
    assert client.post("/api/vpn-sessions/9999/ack").status_code == 404


@pytest.mark.parametrize("appel,cle", [
    (lambda c: c.post("/api/profiles/carla", json={"name": "Carla", "schedule": {
        "monday": [{"start": "08:00", "end": "20:00", "mode": modes.FREE}]}}),
     "event.profile_created"),
    (lambda c: c.post("/api/profiles/alice", json={"name": "Alice", "schedule": {
        "monday": [{"start": "09:00", "end": "20:00", "mode": modes.FREE}]}}),
     "event.profile_updated"),
    (lambda c: c.post("/api/profiles/alice/wifi-key", json={"wifi_key": "une-autre-cle-2026"}),
     "event.wifi_key_changed"),
    (lambda c: c.patch("/api/domains/jeu.fr", json={"blocked_free": True}),
     "event.domain_rule_changed"),
])
def test_les_decisions_du_parent_sont_journalisees(client, file_capturee, monkeypatch,
                                                   appel, cle):
    import claude_agent
    monkeypatch.setattr(claude_agent, "_sync_pihole_blacklists", lambda cfg: None)
    assert appel(client).status_code == 200
    assert cle in _cles_decisions(client)


def test_retirer_une_derogation_se_journalise_seulement_si_elle_existait(client, db):
    assert client.delete("/api/overrides/alice/2026-10-02").json()["ok"]
    assert _cles_decisions(client) == []
    db.set_schedule_override("alice", "2026-10-02", modes.OFF)
    client.delete("/api/overrides/alice/2026-10-02")
    assert _cles_decisions(client) == ["event.schedule_override_removed"]


def test_refermer_un_domaine_n_est_plus_confondu_avec_son_echeance(client, db, monkeypatch):
    import claude_agent
    monkeypatch.setattr(claude_agent, "_sync_pihole_blacklists", lambda cfg: None)
    db.set_temp_domain_unblock("jeu.fr", "alice", PLUS_TARD())
    client.delete("/api/temp-domain/jeu.fr?profile=alice")
    assert _cles_decisions(client) == ["event.domain_unblock_cancelled"]


def test_toute_decision_a_son_texte_dans_les_quatre_langues():
    import database
    from pathlib import Path
    racine = Path(__file__).resolve().parent.parent
    for langue in ("fr", "en", "es", "pt"):
        t = json.loads((racine / "i18n" / f"{langue}.json").read_text())
        manquantes = [k for k in database.DECISION_KEYS if k not in t]
        assert manquantes == [], (langue, manquantes)


# ------------------------------------------------------------------ #
#  Contrôle de santé (P2-3)                                           #
# ------------------------------------------------------------------ #
#  « Le service est actif » ne prouvait rien : un tableau de bord dont le moniteur plante
#  à chaque cycle restait actif, et une mise à jour fautive n'était pas restaurée.

@pytest.mark.parametrize("dernier_cycle,attendu", [
    (lambda: time.time() - 30, 200),
    (lambda: time.time() - 60 * 4, 503),         # plus de trois intervalles
    (lambda: None, 503),                          # aucun cycle réussi encore
])
def test_la_sante_suit_le_dernier_cycle_reussi(appli, monkeypatch, dernier_cycle, attendu):
    from fastapi.testclient import TestClient
    moniteur = NS(last_cycle_ok=dernier_cycle(), interval=60)
    monkeypatch.setattr(appli, "monitor", moniteur)
    monkeypatch.setattr(appli, "_check_session", lambda request: False)   # sans session
    r = TestClient(appli.app).get("/api/health")
    assert r.status_code == attendu and set(r.json()) <= {"ok", "state"}


def test_sans_moniteur_la_sante_echoue_sauf_pendant_l_assistant(appli, config, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(appli, "_check_session", lambda request: False)
    assert TestClient(appli.app).get("/api/health").status_code == 503
    config.data["configured"] = False
    config.save()
    assert TestClient(appli.app).get("/api/health").status_code == 200


def test_un_cycle_reussi_est_note(moniteur):
    assert moniteur.last_cycle_ok is None
    moniteur.run_cycle()
    assert time.time() - moniteur.last_cycle_ok < 5


# ------------------------------------------------------------------ #
#  Exceptions de domaine par enfant                                   #
# ------------------------------------------------------------------ #
#  Demandé par le mainteneur : autoriser poki en temps libre pour un enfant de 6 ans,
#  sans ouvrir tout le divertissement. Le moteur savait le faire ; aucune page ne le
#  permettait, et l'onglet Domaines ne savait que bloquer, pour toute la maison.

@pytest.fixture
def synchros(monkeypatch):
    import claude_agent
    appels: list = []
    monkeypatch.setattr(claude_agent, "_sync_pihole_blacklists", lambda cfg: appels.append(1))
    return appels


def test_un_parent_autorise_un_site_pour_un_enfant_en_temps_libre(client, db, config, synchros):
    import domain_classifier as classifier
    classifier.update_domain("poki.com", category="entertainment")
    # Un enfant de 6 ans : le divertissement lui est fermé, même en temps libre.
    six_ans = {**config.data["profiles"]["alice"], "birth_year": datetime.now().year - 6}
    six_ans.pop("access", None)
    assert "poki.com" in classifier.get_active_blacklist(modes.FREE, six_ans, "alice")
    r = client.post("/api/profiles/alice/domain-rules",
                    json={"domain": "https://www.Poki.com/fr/jeux", "modes": [modes.FREE],
                          "allow": True})
    assert r.json()["ok"] is True and synchros
    assert client.get("/api/profiles/alice/domain-rules").json() == [
        {"domain": "poki.com", "mode": modes.FREE, "allow": True}]
    assert "poki.com" not in classifier.get_active_blacklist(modes.FREE, six_ans, "alice")
    assert "poki.com" in classifier.get_active_blacklist(modes.FREE, six_ans, "bruno")
    assert "event.domain_rule_changed" in _cles_decisions(client)
    # Rien pour l'autre enfant.
    assert client.get("/api/profiles/bruno/domain-rules").json() == []

    r = client.delete("/api/profiles/alice/domain-rules", params={"domain": "poki.com"})
    assert r.json()["ok"] is True
    assert client.get("/api/profiles/alice/domain-rules").json() == []


@pytest.mark.parametrize("corps,code", [
    ({"domain": "pas un domaine", "modes": [modes.FREE], "allow": True}, "invalid_domain"),
    ({"domain": "poki.com", "modes": [], "allow": True}, "invalid_mode"),
    ({"domain": "poki.com", "modes": [modes.OFF], "allow": True}, "invalid_mode"),
])
def test_une_exception_invalide_est_refusee(client, synchros, corps, code):
    r = client.post("/api/profiles/alice/domain-rules", json=corps)
    assert r.status_code == 400 and r.json()["error_code"] == code
    assert client.post("/api/profiles/inconnu/domain-rules", json={
        "domain": "poki.com", "modes": [modes.FREE], "allow": True}).status_code == 404


def test_un_site_bloque_a_tout_age_ne_s_autorise_pas(client, synchros):
    """L'autorisation serait enregistrée sans effet : on le dit au parent."""
    import domain_classifier as classifier
    classifier.update_domain("site-adulte.example", category="adult")
    r = client.post("/api/profiles/alice/domain-rules", json={
        "domain": "site-adulte.example", "modes": [modes.FREE], "allow": True})
    assert r.status_code == 400 and r.json()["error_code"] == "domain_always_blocked"
    assert client.get("/api/profiles/alice/domain-rules").json() == []


# ------------------------------------------------------------------ #
#  Session : fermée 10 min après la dernière action de l'utilisateur  #
# ------------------------------------------------------------------ #

import dashboard as _dashboard_au_chargement
_VRAIE_SESSION = _dashboard_au_chargement._check_session


def test_la_session_expire_meme_si_la_page_reste_ouverte(client, monkeypatch):
    """Constaté : un onglet ouvert gardait la session indéfiniment, et un enfant s'en est
    servi. Les requêtes automatiques de la page ne prolongent plus la session."""
    import dashboard
    horloge = [datetime(2026, 10, 2, 20, 0)]

    class Horloge(datetime):
        @classmethod
        def now(cls, tz=None):
            return horloge[0]

    monkeypatch.setattr(dashboard, "_check_session", _VRAIE_SESSION)
    monkeypatch.setattr(dashboard, "datetime", Horloge)
    dashboard._sessions["jeton"] = horloge[0] + dashboard.SESSION_TTL
    client.cookies.set("fw_session", "jeton")
    auto = {dashboard.AUTO_HEADER: "1"}

    def statut(entetes=None):
        return client.get("/api/status", headers=entetes or {}).status_code

    horloge[0] += timedelta(minutes=9)
    assert statut(auto) == 200                     # valide, mais ne prolonge pas
    horloge[0] += timedelta(minutes=2)
    assert statut(auto) == 401                     # 11 min sans action : fermée
    # Une action de l'utilisateur, elle, prolonge.
    dashboard._sessions["jeton"] = horloge[0] + dashboard.SESSION_TTL
    horloge[0] += timedelta(minutes=9)
    assert statut() == 200
    horloge[0] += timedelta(minutes=9)
    assert statut(auto) == 200
    horloge[0] += timedelta(minutes=2)
    assert not dashboard._session_alive("jeton")
