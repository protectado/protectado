# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Arnaud Ortais
# Dual-licensed: AGPL-3.0 (open source) or Commercial License — see LICENSE and LICENSE-COMMERCIAL.
"""
test_identity_events.py — ce qui mérite d'être signalé au parent, et à quelle cadence.

Les deux défauts couverts ici ont été constatés sur un vrai boîtier : le journal du parent
recevait plusieurs lignes par minute pour un téléphone qui n'entrait même pas sur le
réseau, et le cycle de surveillance tournait quinze fois trop souvent.
"""

import json
from datetime import datetime, timedelta

import pytest

import station_identity as si


ASSOCIEE_SANS_CLE = """4e:54:1a:a7:7a:4e
flags=[AUTH][ASSOC][SHORT_PREAMBLE]
aid=1
"""

AUTORISEE_CLE_CONNUE = """02:aa:bb:cc:dd:01
flags=[AUTH][ASSOC][AUTHORIZED][WMM]
keyid=alice
"""

AUTORISEE_CLE_INCONNUE = """02:aa:bb:cc:dd:09
flags=[AUTH][ASSOC][AUTHORIZED]
keyid=profil_supprime
"""


# ------------------------------------------------------------------ #
#  Autorisée ou seulement associée                                    #
# ------------------------------------------------------------------ #

def test_une_station_sans_poignee_de_main_n_a_pas_de_profil():
    """Un appareil qui rejoue une clé périmée s'associe au niveau radio, échoue au 4-way
    handshake, se fait déconnecter et recommence. Il n'a rien prouvé."""
    stations = si.merge(si.parse_all_sta(ASSOCIEE_SANS_CLE), {}, known_profiles=["alice"])
    assert stations[0]["authorized"] is False
    assert stations[0]["profile"] == ""


def test_une_station_autorisee_avec_une_cle_connue_obtient_son_profil():
    stations = si.merge(si.parse_all_sta(AUTORISEE_CLE_CONNUE), {}, known_profiles=["alice"])
    assert stations[0]["authorized"] is True
    assert stations[0]["profile"] == "alice"


def test_une_cle_valide_sans_profil_reste_sans_profil():
    """Profil supprimé dont l'appareil est encore connecté : on ne devine pas à qui il est."""
    stations = si.merge(si.parse_all_sta(AUTORISEE_CLE_INCONNUE), {}, known_profiles=["alice"])
    assert stations[0]["authorized"] is True
    assert stations[0]["profile"] == ""


def test_une_station_non_autorisee_n_obtient_aucune_adresse_applicable():
    """Même avec un bail, elle ne doit apparaître dans aucune liste d'appareils."""
    baux = {"4e:54:1a:a7:7a:4e": {"ip": "192.168.50.61", "hostname": "tel", "expiry": 0}}
    identite = {"stations": si.merge(si.parse_all_sta(ASSOCIEE_SANS_CLE), baux,
                                    known_profiles=["alice"])}
    assert si.devices_by_profile(identite) == {}
    assert si.identified_ips(identite) == set()


# ------------------------------------------------------------------ #
#  Le journal du parent                                               #
# ------------------------------------------------------------------ #

def _publier(tmp_path, brut, known=("alice", "bruno")):
    stations = si.merge(si.parse_all_sta(brut), {}, known_profiles=list(known))
    si.write(stations, str(tmp_path / si.IDENTITY_NAME))


def test_un_appareil_qui_rejoue_une_ancienne_cle_ne_produit_aucun_evenement(db, tmp_path, moniteur):
    """LE défaut constaté : plusieurs lignes par minute pour un non-événement.

    Vingt rafraîchissements, et le journal doit rester vide.
    """
    m = moniteur
    for _ in range(20):
        _publier(tmp_path, ASSOCIEE_SANS_CLE)
        m._sync_identity()
    assert db.get_recent_events() == []


def test_une_cle_valide_sans_profil_est_signalee_une_seule_fois(db, tmp_path, moniteur):
    """Le cas vraiment anormal se signale, mais une fois, pas à chaque publication.

    Le repère précédent était l'horodatage du fichier, qui change à chaque publication :
    il ne dédupliquait donc rien.
    """
    m = moniteur
    for _ in range(20):
        _publier(tmp_path, AUTORISEE_CLE_INCONNUE)
        m._sync_identity()

    evenements = db.get_recent_events()
    assert len(evenements) == 1
    assert evenements[0]["message_key"] == "event.station_unknown_key"
    params = json.loads(evenements[0]["params"])
    assert params["mac"] == "02:aa:bb:cc:dd:09"
    assert params["keyid"] == "profil_supprime", "la clé doit être dite, pour diagnostiquer"


def test_le_signalement_revient_apres_la_fenetre_de_silence(db, tmp_path, moniteur):
    """L'anomalie persiste, donc elle doit se rappeler au parent, mais rarement."""
    import monitor

    m = moniteur
    _publier(tmp_path, AUTORISEE_CLE_INCONNUE)
    m._sync_identity()
    assert len(db.get_recent_events()) == 1

    vieux = datetime.now() - timedelta(seconds=monitor.UNKNOWN_KEY_SILENCE_SEC + 1)
    m._unknown_key_alerted = {k: vieux for k in m._unknown_key_alerted}
    _publier(tmp_path, AUTORISEE_CLE_INCONNUE)
    m._sync_identity()
    assert len(db.get_recent_events()) == 2


def test_un_appareil_normal_ne_produit_aucun_evenement(db, tmp_path, moniteur):
    m = moniteur
    _publier(tmp_path, AUTORISEE_CLE_CONNUE)
    m._sync_identity()
    assert db.get_recent_events() == []


# ------------------------------------------------------------------ #
#  Cadence de publication                                             #
# ------------------------------------------------------------------ #

def test_l_identite_n_est_republiee_que_sur_changement(config, tmp_path, monkeypatch):
    """L'agent guette ce fichier et réveille son cycle COMPLET dès qu'il bouge.

    Le republier à chaque interrogation faisait tourner la surveillance toutes les
    4 secondes au lieu de 60 : quinze fois plus d'appels à Pi-hole, d'écritures en base et
    d'événements. On ne republie donc que sur changement réel.
    """
    import action_runner as ar

    monkeypatch.setattr(ar, "_enforcement_mode", lambda: "gateway")
    monkeypatch.setattr(ar, "_gateway_active", False)   # pas de réconciliation iptables
    monkeypatch.setattr(ar, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(ar, "_identity_signature", None)
    monkeypatch.setattr(ar, "_last_identity_write", 0.0)
    monkeypatch.setattr(si, "read_leases", lambda path="": {})
    monkeypatch.setattr(ar, "_hostapd_all_sta", lambda: AUTORISEE_CLE_CONNUE)

    chemin = tmp_path / si.IDENTITY_NAME

    ar._refresh_identity()
    premier = chemin.read_text()

    # Même état : aucune réécriture, donc l'agent n'est pas réveillé.
    ar._refresh_identity()
    ar._refresh_identity()
    assert chemin.read_text() == premier

    # L'état change : publication immédiate.
    monkeypatch.setattr(ar, "_hostapd_all_sta",
                        lambda: AUTORISEE_CLE_CONNUE + AUTORISEE_CLE_INCONNUE)
    ar._refresh_identity()
    assert chemin.read_text() != premier
    assert len(json.loads(chemin.read_text())["stations"]) == 2


def test_l_identite_est_rafraichie_periodiquement_pour_rester_fraiche(
        config, tmp_path, monkeypatch):
    """Sans changement, le fichier doit tout de même être réécrit de temps en temps :
    passé MAX_AGE_SECONDS, l'agent le considère périmé et s'abstient d'agir."""
    import action_runner as ar

    monkeypatch.setattr(ar, "_enforcement_mode", lambda: "gateway")
    monkeypatch.setattr(ar, "_gateway_active", False)
    monkeypatch.setattr(ar, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(ar, "_identity_signature", None)
    monkeypatch.setattr(ar, "_last_identity_write", 0.0)
    monkeypatch.setattr(si, "read_leases", lambda path="": {})
    monkeypatch.setattr(ar, "_hostapd_all_sta", lambda: AUTORISEE_CLE_CONNUE)

    chemin = tmp_path / si.IDENTITY_NAME
    ar._refresh_identity()
    premier = chemin.read_text()
    ar._refresh_identity()
    assert chemin.read_text() == premier

    # La dernière écriture remonte à plus longtemps que le rafraîchissement périodique.
    monkeypatch.setattr(ar, "_last_identity_write",
                        ar.time.time() - ar.IDENTITY_STAMP_REFRESH - 1)
    ar._refresh_identity()
    assert chemin.read_text() != premier, "l'horodatage doit avoir été rafraîchi"
    assert ar.IDENTITY_STAMP_REFRESH < si.MAX_AGE_SECONDS, \
        "le rafraîchissement doit être plus rapide que la péremption"


def test_hostapd_muet_ne_publie_pas_un_reseau_vide(config, tmp_path, monkeypatch):
    """« Je ne sais pas » n'est pas « plus personne n'est connecté » : conclure au réseau
    vide retirerait toutes les autorisations et couperait les enfants."""
    import action_runner as ar

    monkeypatch.setattr(ar, "_enforcement_mode", lambda: "gateway")
    monkeypatch.setattr(ar, "_gateway_active", False)
    monkeypatch.setattr(ar, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(ar, "_identity_signature", None)
    monkeypatch.setattr(ar, "_last_identity_write", 0.0)
    monkeypatch.setattr(si, "read_leases", lambda path="": {})
    monkeypatch.setattr(ar, "_hostapd_all_sta", lambda: AUTORISEE_CLE_CONNUE)

    chemin = tmp_path / si.IDENTITY_NAME
    ar._refresh_identity()
    avant = chemin.read_text()

    monkeypatch.setattr(ar, "_hostapd_all_sta", lambda: None)
    ar._refresh_identity()
    assert chemin.read_text() == avant


# ------------------------------------------------------------------ #
#  Ce qui aide le parent à reconnaître un appareil                    #
# ------------------------------------------------------------------ #

RICHE = """02:aa:bb:cc:dd:01
flags=[AUTH][ASSOC][AUTHORIZED][WMM]
aid=1
connected_time=9412
inactive_msec=124
signal=-47
rx_packets=182344
rx_bytes=1288490188
tx_bytes=48221104
keyid=alice
"""

PAUVRE = """02:aa:bb:cc:dd:02
flags=[AUTH][ASSOC][AUTHORIZED]
keyid=alice
"""


def test_les_reperes_d_identification_sont_publies():
    """Tout vient de la MÊME lecture que le keyid : aucune requête de plus."""
    baux = {"02:aa:bb:cc:dd:01": {"ip": "192.168.50.52",
                                  "hostname": "iPhone-de-la-maison", "expiry": 0}}
    st = si.merge(si.parse_all_sta(RICHE), baux, known_profiles=["alice"])[0]
    assert st["hostname"] == "iPhone-de-la-maison"
    assert st["connected_time"] == 9412
    assert st["rx_bytes"] == 1288490188
    assert st["tx_bytes"] == 48221104
    assert st["signal"] == -47
    assert st["private_mac"] is True


def test_un_attribut_absent_vaut_inconnu_et_non_zero():
    """Les attributs varient selon le pilote. Un champ manquant affiché comme « 0 octet »
    se lirait comme une mesure réelle, alors que c'est une absence de mesure."""
    st = si.merge(si.parse_all_sta(PAUVRE), {}, known_profiles=["alice"])[0]
    assert st["connected_time"] is None
    assert st["rx_bytes"] is None
    assert st["signal"] is None


@pytest.mark.parametrize("mac,privee", [
    ("02:aa:bb:cc:dd:01", True),    # bit 0x02 posé
    ("4e:54:1a:a7:7a:4e", True),
    ("5a:f8:08:73:c7:cf", True),
    ("a4:83:e7:11:22:33", False),   # vrai fabricant
    ("dc:a6:32:00:11:22", False),   # Raspberry Pi
    ("pas-une-mac", False),
])
def test_adresse_privee_detectee(mac, privee):
    """Explique au parent pourquoi l'appareil n'a pas de fabricant identifiable et pourquoi
    son adresse change. Ce sont exactement les deux adresses vues sur le boîtier."""
    assert si.is_private_mac(mac) is privee


def test_les_reperes_suivent_jusqu_au_profil():
    """Le tableau de bord lit les appareils par profil : les repères doivent y arriver,
    sans quoi le parent ne voit qu'une adresse."""
    baux = {"02:aa:bb:cc:dd:01": {"ip": "192.168.50.52", "hostname": "tablette",
                                  "expiry": 0}}
    identite = {"stations": si.merge(si.parse_all_sta(RICHE), baux,
                                    known_profiles=["alice"])}
    appareil = si.devices_by_profile(identite)["alice"][0]
    assert appareil["hostname"] == "tablette"
    assert appareil["connected_time"] == 9412
    assert appareil["rx_bytes"] == 1288490188
    assert appareil["private_mac"] is True


def test_les_compteurs_ne_declenchent_pas_de_republication(config, tmp_path, monkeypatch):
    """Les compteurs bougent en permanence. S'ils entraient dans la signature, le fichier
    serait republié à chaque interrogation et le cycle de l'agent repartirait toutes les
    4 secondes, ce qui est le défaut que la signature corrige."""
    import action_runner as ar

    monkeypatch.setattr(ar, "_enforcement_mode", lambda: "gateway")
    monkeypatch.setattr(ar, "_gateway_active", False)
    monkeypatch.setattr(ar, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(ar, "_identity_signature", None)
    monkeypatch.setattr(ar, "_last_identity_write", 0.0)
    monkeypatch.setattr(si, "read_leases", lambda path="": {})
    monkeypatch.setattr(ar, "_hostapd_all_sta", lambda: RICHE)

    chemin = tmp_path / si.IDENTITY_NAME
    ar._refresh_identity()
    premier = chemin.read_text()

    # Mêmes station et adresse, compteurs qui ont bougé : aucune republication.
    monkeypatch.setattr(ar, "_hostapd_all_sta",
                        lambda: RICHE.replace("rx_bytes=1288490188",
                                              "rx_bytes=1999999999"))
    ar._refresh_identity()
    assert chemin.read_text() == premier


# ------------------------------------------------------------------ #
#  Le même appareil porte le même nom partout                         #
# ------------------------------------------------------------------ #

def test_ordre_de_preference_du_nom_d_appareil():
    """Le nom donné par le parent l'emporte, puis celui que l'appareil annonce, puis
    l'adresse. Désigner un appareil par son nom à un endroit et par son adresse à un
    autre force le parent à faire le rapprochement lui-même."""
    import dashboard

    parent = {"192.168.50.52": "Tablette du salon"}
    annonces = {"192.168.50.52": "iPad", "192.168.50.53": "A16-de-quelquun"}

    assert dashboard._device_label("192.168.50.52", parent, annonces) == "Tablette du salon"
    assert dashboard._device_label("192.168.50.53", parent, annonces) == "A16-de-quelquun"
    assert dashboard._device_label("192.168.50.99", parent, annonces) == "192.168.50.99"
    assert dashboard._device_label("192.168.50.52", {}, {}) == "192.168.50.52"


def test_le_statut_publie_les_noms_par_adresse(db, config, tmp_path, monkeypatch):
    """Les écrans qui partent d'une adresse (journal, mode adulte) ont besoin de la
    correspondance : elle est calculée une fois, côté serveur."""
    import dashboard

    config.data["profiles"]["alice"]["devices"] = [
        {"ip": "192.168.50.52", "mac": "02:aa:bb:cc:dd:01", "hostname": "iPad"},
        {"ip": "192.168.50.53", "mac": "02:aa:bb:cc:dd:02", "hostname": ""},
    ]
    config.data["device_names"] = {"192.168.50.53": "Console du salon"}

    profil = config.data["profiles"]["alice"]
    noms = {d["ip"]: d["hostname"] for d in profil["devices"] if d.get("hostname")}
    etiquettes = {ip: dashboard._device_label(ip, config.data["device_names"], noms)
                  for ip in (d["ip"] for d in profil["devices"])}
    assert etiquettes == {"192.168.50.52": "iPad",
                          "192.168.50.53": "Console du salon"}


def test_le_nom_annonce_sert_a_la_page_appareils(db, config):
    """En posture passerelle, c'est la SEULE source : dnsmasq sert le DHCP du réseau
    enfants avec port=0, donc Pi-hole FTL ne voit passer aucun de ces noms."""
    import station_identity as si

    baux = {"02:aa:bb:cc:dd:01": {"ip": "192.168.50.52", "hostname": "iPad", "expiry": 0}}
    identite = {"stations": si.merge(si.parse_all_sta(AUTORISEE_CLE_CONNUE), baux,
                                    known_profiles=["alice"])}
    si.apply_to_config(config.data, {**identite, "updated_at": datetime.now().isoformat()})
    noms = {d["ip"]: d.get("hostname")
            for d in config.data["profiles"]["alice"]["devices"]}
    assert noms == {"192.168.50.52": "iPad"}


def test_une_adresse_deja_connue_recoit_quand_meme_son_nom(db, config):
    """Défaut trouvé en écrivant le test précédent : la liste d'appareils n'était
    remplacée que si l'ENSEMBLE DES ADRESSES changeait.

    Une adresse déjà présente dans la configuration gardait donc son entrée figée, sans
    nom annoncé, sans compteur, indéfiniment. C'est le cas d'un appareil rattaché à la
    main avant l'identité par clé, et celui d'un appareil dont le premier bail n'annonçait
    pas encore de nom.
    """
    import station_identity as si

    profil = config.data["profiles"]["alice"]
    profil["devices"] = [{"ip": "192.168.50.52", "mac": "02:aa:bb:cc:dd:01"}]

    baux = {"02:aa:bb:cc:dd:01": {"ip": "192.168.50.52", "hostname": "iPad", "expiry": 0}}
    identite = si.render(si.merge(si.parse_all_sta(RICHE), baux, known_profiles=["alice"]))
    si.apply_to_config(config.data, identite)

    appareil = profil["devices"][0]
    assert appareil["hostname"] == "iPad"
    assert appareil["connected_time"] == 9412


def test_les_compteurs_se_rafraichissent_sans_signaler_de_changement(db, config):
    """Les compteurs doivent suivre pour l'affichage, mais sans produire de trace : une
    ligne de journal à chaque rafraîchissement serait du bruit."""
    import station_identity as si

    baux = {"02:aa:bb:cc:dd:01": {"ip": "192.168.50.52", "hostname": "iPad", "expiry": 0}}
    profil = config.data["profiles"]["alice"]
    profil["devices"] = []

    premier = si.render(si.merge(si.parse_all_sta(RICHE), baux, known_profiles=["alice"]))
    assert si.apply_to_config(config.data, premier) is True     # l'appareil apparaît

    plus_tard = si.render(si.merge(
        si.parse_all_sta(RICHE.replace("rx_bytes=1288490188", "rx_bytes=1999999999")),
        baux, known_profiles=["alice"]))
    assert si.apply_to_config(config.data, plus_tard) is False  # rien à signaler
    assert profil["devices"][0]["rx_bytes"] == 1999999999       # mais la valeur a suivi


# ------------------------------------------------------------------ #
#  Les appareils déjà vus d'un enfant                                 #
# ------------------------------------------------------------------ #
#  La liste d'appareils d'un profil n'est que l'état du moment : sans mémoire, l'écran ne
#  distinguait pas « jamais connecté » de « connecté hier ».

def _publier_avec_bail(tmp_path, brut, ip, nom):
    stations = si.merge(si.parse_all_sta(brut),
                        {"02:aa:bb:cc:dd:01": {"ip": ip, "hostname": nom, "expiry": 0}},
                        known_profiles=["alice", "bruno"])
    si.write(stations, str(tmp_path / si.IDENTITY_NAME))


def test_un_appareil_connecte_est_retenu_avec_son_dernier_nom(db, tmp_path, moniteur):
    _publier_avec_bail(tmp_path, AUTORISEE_CLE_CONNUE, "192.168.50.52", "tablette")
    moniteur._sync_identity()
    _publier_avec_bail(tmp_path, AUTORISEE_CLE_CONNUE, "192.168.50.52", "")   # nom perdu
    moniteur._sync_identity()
    (vu,) = db.get_seen_devices("alice")
    assert (vu["mac"], vu["name"], vu["last_ip"]) == ("02:aa:bb:cc:dd:01", "tablette",
                                                      "192.168.50.52")
    assert db.get_seen_devices("bruno") == []


def test_les_appareils_vus_sont_tries_et_bornes(db):
    for i in range(8):
        db.record_seen_devices("alice", [{"mac": f"02:00:00:00:00:0{i}", "hostname": f"a{i}",
                                          "ip": "192.168.50.60"}],
                               quand=f"2026-09-2{i}T10:00:00")
    vus = db.get_seen_devices("alice", limit=3)
    assert [v["name"] for v in vus] == ["a7", "a6", "a5"]


# ------------------------------------------------------------------ #
#  Débit en direct, depuis les compteurs de hostapd                   #
# ------------------------------------------------------------------ #
#  hostapd compte du point de vue du POINT D'ACCÈS : tx_bytes est ce qu'il a envoyé à
#  l'appareil (ce que l'enfant télécharge), rx_bytes ce qu'il en a reçu. Mesuré sur le
#  boîtier, iPhone sur YouTube : tx_bytes +4,2 Mo en 30 s, rx_bytes +0,2 Mo.

def test_le_debit_se_calcule_dans_le_bon_sens():
    avant = [{"mac": "5a:f8:08:73:c7:cf", "rx_bytes": 14_566_046, "tx_bytes": 246_900_879}]
    memo = si.add_rates(avant, {}, now=1000.0)
    assert avant[0]["down_bps"] is None            # premier relevé : rien à comparer
    apres = [{"mac": "5a:f8:08:73:c7:cf", "rx_bytes": 14_790_602, "tx_bytes": 251_107_239}]
    si.add_rates(apres, memo, now=1030.0)
    assert round(apres[0]["down_bps"]) == 140_212 and round(apres[0]["up_bps"]) == 7_485


def test_un_compteur_qui_repart_de_zero_ne_donne_pas_de_debit_negatif():
    """Les compteurs repartent de zéro à chaque association (rotation de MAC comprise)."""
    memo = si.add_rates([{"mac": "aa", "rx_bytes": 5_000_000, "tx_bytes": 9_000_000}], {}, now=0)
    st = [{"mac": "aa", "rx_bytes": 1_000, "tx_bytes": 2_000}]
    si.add_rates(st, memo, now=60)
    assert st[0]["down_bps"] is None and st[0]["up_bps"] is None


def test_le_debit_suit_l_appareil_jusqu_au_profil():
    identite = {"stations": [{"mac": "aa", "profile": "alice", "ip": "192.168.50.85",
                              "down_bps": 140_212.0, "up_bps": 7_485.2}]}
    appareil = si.devices_by_profile(identite)["alice"][0]
    assert (appareil["down_bps"], appareil["up_bps"]) == (140_212.0, 7_485.2)
