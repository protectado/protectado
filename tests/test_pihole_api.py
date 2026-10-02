# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Arnaud Ortais
# Dual-licensed: AGPL-3.0 (open source) or Commercial License — see LICENSE and LICENSE-COMMERCIAL.
"""
test_pihole_api.py — ce que le client Pi-hole demande à l'API FTL v6.

Vrai PiHoleAPI, appels HTTP remplacés (fixture `pihole_api`) : on vérifie ce qui est
émis, sans jamais joindre un Pi-hole.
"""

import pytest


# ------------------------------------------------------------------ #
#  Requêtes DNS : toute la fenêtre, page par page                     #
# ------------------------------------------------------------------ #

def _fenetre(api, total):
    """Pi-hole qui rend `total` requêtes, la plus récente d'abord, par pages."""
    requetes = [{"id": 10_000 - i, "domain": f"d{i}.fr"} for i in range(total)]

    def get(endpoint, params=None):
        api.appels.append(("GET", endpoint, dict(params)))
        debut = params.get("start", 0)
        return {"queries": requetes[debut:debut + params["length"]], "cursor": 10_000}
    api._get = get


def test_toutes_les_pages_sont_lues_avec_le_meme_curseur(pihole_api):
    """Défaut reproduit (P1-9) : 1017 requêtes dans la fenêtre, 500 rendues."""
    import pihole_api as module
    n = module.QUERY_PAGE * 2 + 17
    _fenetre(pihole_api, n)
    assert len(pihole_api.get_recent_queries()) == n
    pages = [p for _, _, p in pihole_api.appels]
    assert "cursor" not in pages[0]
    assert [(p["start"], p["cursor"]) for p in pages[1:]] == [
        (module.QUERY_PAGE, 10_000), (module.QUERY_PAGE * 2, 10_000)]
    assert len({p["from"] for p in pages}) == 1


def test_le_plafond_arrete_la_lecture_et_le_dit(pihole_api, capsys):
    import pihole_api as module
    _fenetre(pihole_api, module.QUERY_MAX * 3)
    assert len(pihole_api.get_recent_queries()) == module.QUERY_MAX
    assert "plafond" in capsys.readouterr().out


def test_une_page_en_erreur_rend_ce_qui_a_ete_lu(pihole_api):
    import pihole_api as module
    _fenetre(pihole_api, module.QUERY_PAGE * 3)
    lire = pihole_api._get
    pihole_api._get = lambda ep, params=None: {} if params.get("start") else lire(ep, params)
    assert len(pihole_api.get_recent_queries()) == module.QUERY_PAGE


# ------------------------------------------------------------------ #
#  Domaines de contournement (Firefox, iCloud Private Relay)          #
# ------------------------------------------------------------------ #

TOUS = {"mozillaCanary": True, "iCloudPrivateRelay": True, "designatedResolver": True}


@pytest.mark.parametrize("etat,reactives", [
    (TOUS, []),
    (dict(TOUS, iCloudPrivateRelay=False), ["iCloudPrivateRelay"]),
])
def test_les_domaines_de_contournement_restent_actifs(pihole_api, etat, reactives):
    pihole_api.reponses["/config/dns/specialDomains"] = {
        "config": {"dns": {"specialDomains": etat}}}
    assert pihole_api.ensure_special_domains() == reactives
    patchs = [p for m, _, p in pihole_api.appels if m == "PATCH"]
    assert patchs == ([{"config": {"dns": {"specialDomains": {k: True for k in reactives}}}}]
                      if reactives else [])


def test_des_reglages_illisibles_ne_sont_pas_ecrases(pihole_api):
    assert pihole_api.ensure_special_domains() is None
    assert not [a for a in pihole_api.appels if a[0] == "PATCH"]


def test_setup_profiles_verifie_les_domaines_de_contournement(pihole_api):
    pihole_api.reponses["/config/dns/specialDomains"] = {
        "config": {"dns": {"specialDomains": dict(TOUS, mozillaCanary=False)}}}
    pihole_api.setup_profiles({})
    assert [p for m, _, p in pihole_api.appels if m == "PATCH"]


# ------------------------------------------------------------------ #
#  Suppression des groupes d'un profil                                #
# ------------------------------------------------------------------ #

def test_supprimer_les_groupes_d_un_profil_preserve_les_entrees_partagees(pihole_api):
    pihole_api.reponses["/groups"] = {"groups": [
        {"name": "alice-off", "id": 11}, {"name": "alice-homework", "id": 12},
        {"name": "alice-free", "id": 13}, {"name": "bruno-free", "id": 23}]}
    pihole_api.reponses["/domains/deny/regex"] = {"domains": [
        {"domain": ".*", "groups": [11], "comment": "protectado:blocked:alice"},
        {"domain": "(.*\\.)?jeu\\.fr$", "groups": [12, 23], "comment": "protectado:free"}]}

    assert pihole_api.delete_profile_groups("alice") is True
    supprimes = [e for m, e, _ in pihole_api.appels if m == "DELETE"]
    assert {"/groups/alice-off", "/groups/alice-homework", "/groups/alice-free"} <= set(supprimes)
    assert "/groups/bruno-free" not in supprimes
    assert [(e, p) for m, e, p in pihole_api.appels if m == "PUT"] == [
        ("/domains/deny/regex/%28.%2A%5C.%29%3Fjeu%5C.fr%24",
         {"comment": "protectado:free", "enabled": True, "groups": [23]})]
    assert not [e for m, e, _ in pihole_api.appels if m == "POST"]


# ------------------------------------------------------------------ #
#  Changement de groupes d'une entrée : sans fenêtre ouverte (P3-2)   #
# ------------------------------------------------------------------ #
#  DELETE puis POST laissait l'entrée absente entre les deux appels, donc le domaine
#  débloqué pour TOUS les profils qui la partageaient ; et un POST en échec la perdait.

def test_la_synchro_modifie_les_groupes_sur_place(pihole_api):
    pihole_api.reponses["/domains/deny/regex"] = {"domains": [
        {"domain": "(.*\\.)?jeu\\.fr$", "groups": [23], "comment": "protectado:free"},
        {"domain": "(.*\\.)?vieux\\.fr$", "groups": [12, 23], "comment": "protectado:free"},
        {"domain": "(.*\\.)?seul\\.fr$", "groups": [12], "comment": "protectado:free"}]}
    pihole_api._sync_blacklist(12, "free", ["jeu.fr", "neuf.fr"])
    emis = [(m, e.rsplit("/", 1)[-1], p) for m, e, p in pihole_api.appels if m != "GET"]
    assert ("PUT", "%28.%2A%5C.%29%3Fjeu%5C.fr%24",
            {"comment": "protectado:free", "enabled": True, "groups": [12, 23]}) in emis
    assert ("PUT", "%28.%2A%5C.%29%3Fvieux%5C.fr%24",
            {"comment": "protectado:free", "enabled": True, "groups": [23]}) in emis
    assert ("DELETE", "%28.%2A%5C.%29%3Fseul%5C.fr%24", None) in emis
    assert [p["domain"] for m, _, p in emis if m == "POST"] == ["(.*\\.)?neuf\\.fr$"]
    # Aucune entrée encore utile n'est supprimée.
    assert [e for m, e, _ in emis if m == "DELETE"] == ["%28.%2A%5C.%29%3Fseul%5C.fr%24"]


@pytest.mark.parametrize("methode", ["post", "put", "patch", "delete"])
def test_une_session_expiree_est_rouverte_pour_toute_ecriture(monkeypatch, methode):
    """Seul GET rouvrait la session sur un 401 : une écriture après expiration échouait
    sans un mot, et le changement de plage n'était pas appliqué."""
    import pihole_api as module
    api = module.PiHoleAPI("http://pihole.test", "x")
    api._sid, api._sid_expires = "ancienne", module.datetime.max
    monkeypatch.setattr(api, "_authenticate", lambda: setattr(api, "_sid", "neuve") or True)
    vus = []

    def requete(url, headers=None, **_):
        vus.append(headers.get("X-FTL-SID"))
        return type("R", (), {"status_code": 401 if len(vus) == 1 else 200,
                              "content": b"{}", "json": lambda self: {}})()
    monkeypatch.setattr(module.requests, methode, requete)
    appel = getattr(api, f"_{methode}")
    resultat = appel("/x") if methode == "delete" else appel("/x", {})
    assert vus == ["ancienne", "neuve"]
    assert resultat == (True if methode == "delete" else {})


def test_un_groupe_recree_n_est_pas_cherche_sous_son_ancien_identifiant(pihole_api):
    pihole_api._group_cache["alice-free"] = 13          # groupe supprimé puis recréé
    pihole_api.reponses["/groups"] = {"groups": [{"name": "alice-free", "id": 41}]}
    pihole_api.get_clients = lambda: [{"client": "192.168.50.52"}]
    pihole_api._put = lambda e, p=None: {}              # le client n'est pas mis à jour
    assert pihole_api.assign_client_to_group("192.168.50.52", "alice-free") is False
    assert pihole_api.get_group_id("alice-free") == 41


@pytest.mark.parametrize("adresse,montree", [
    ("192.168.50.72", True), ("2a01:e0a:1:2::5", True), ("alice-tablette", True),
    ("127.0.0.1", False), ("ff02::16", False), ("0.0.0.0", False),
    # Constaté sur le boîtier : l'adresse de lien local d'un appareil déjà listé en IPv4
    # (même MAC) apparaissait comme un second appareil, rattaché au parent par défaut.
    ("fe80::1018:8ce0:8b57:5bcb", False), ("169.254.12.3", False),
])
def test_seules_les_adresses_d_appareils_sont_montrees(adresse, montree):
    from pihole_api import is_real_device_ip
    assert is_real_device_ip(adresse) is montree
