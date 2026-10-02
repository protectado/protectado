# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Arnaud Ortais
# Dual-licensed: AGPL-3.0 (open source) or Commercial License — see LICENSE and LICENSE-COMMERCIAL.
"""
test_filtering.py — ce que le boîtier bloque, pour CET enfant, dans CE mode.

Sources, dans l'ordre : la grille de l'enfant (par tranche d'âge, personnalisable), les
exceptions du parent, les déblocages temporaires, puis ce que rien ne rouvre : les deux
catégories bloquées à tout âge et, en passerelle, les résolveurs DNS-over-HTTPS connus.
Les tests les plus importants vérifient ce qu'aucun réglage ne peut faire.
"""

import json
from datetime import datetime, timedelta

import pytest

import access_grid as ag
import modes

ANNEE = datetime.now().year
PLUS_TARD = lambda: (datetime.now() + timedelta(hours=1)).isoformat()   # noqa: E731
AVANT = lambda: (datetime.now() - timedelta(minutes=1)).isoformat()     # noqa: E731


def _enfant(age, access=None):
    profil = {"name": "Enfant", "birth_year": ANNEE - age}
    if access is not None:
        profil["access"] = access
    return profil


def _catalogue(**domaines):
    """Pose des domaines avec leur catégorie (« jeu_fr » désigne jeu.fr)."""
    import domain_classifier as classifier
    for domaine, categorie in domaines.items():
        nom = domaine.replace("_", ".")
        classifier.record_domain(nom, modes.FREE)
        classifier.update_domain(nom, category=categorie)


def _liste(mode, profil, cle="alice"):
    import domain_classifier as classifier
    return classifier.get_active_blacklist(mode, profil, cle)


# ------------------------------------------------------------------ #
#  La grille par tranche d'âge                                        #
# ------------------------------------------------------------------ #

@pytest.mark.parametrize("age,devoirs,libre", [
    (7,  {"education", "work"},          {"education", "work", "other"}),
    (11, {"education", "work"},          {"education", "work", "other", "entertainment"}),
    (14, {"education", "work", "other"}, {"education", "work", "other", "entertainment", "social"}),
    (18, {"education", "work", "other"}, {"education", "work", "other", "entertainment", "social"}),
])
def test_les_defauts_par_tranche(age, devoirs, libre):
    grille = ag.grid_of(_enfant(age))
    assert (grille[modes.HOMEWORK], grille[modes.FREE]) == (devoirs, libre)


def test_age_inconnu_prend_la_tranche_la_plus_protectrice():
    assert ag.grid_of({}) == ag.grid_of(_enfant(7))


def test_aucune_grille_ne_peut_autoriser_l_adulte_ni_l_extremisme():
    triche = _enfant(17, {"free": ["adult", "extremism", "social"], "homework": ["adult"]})
    for mode in (modes.HOMEWORK, modes.FREE):
        assert not {"adult", "extremism"} & ag.allowed_categories(triche, mode)
        assert {"adult", "extremism"} <= ag.blocked_categories(triche, mode)


def test_sanitize_ne_garde_que_les_categories_configurables():
    """`homework` absent veut dire « défaut de la tranche », différent d'une liste vide
    (« rien d'autorisé ») : les confondre couperait un enfant."""
    propre = ag.sanitize({"free": ["adult", "social", "cdn", "inconnue", "EDUCATION"],
                          "off": ["education"], "nimportequoi": []})
    assert propre == {"free": ["education", "social"]}


def test_une_liste_vide_n_autorise_que_l_indispensable():
    strict = _enfant(14, {"homework": []})
    assert ag.allowed_categories(strict, modes.HOMEWORK) == set(ag.ALWAYS_ALLOWED)
    assert ag.grid_of(strict)[modes.FREE] == ag.grid_of(_enfant(14))[modes.FREE]


def test_l_infrastructure_et_l_inconnu_ne_sont_jamais_bloques():
    """Bloquer l'inconnu couperait l'internet à chaque site neuf ; l'infrastructure casse
    les sites autorisés sans rien protéger."""
    for age in (7, 18):
        for mode in (modes.HOMEWORK, modes.FREE):
            assert {"cdn", "unknown"} <= ag.allowed_categories(_enfant(age), mode)


def test_un_mode_ferme_n_autorise_rien():
    assert ag.allowed_categories(_enfant(11), modes.OFF) == set()
    assert ag.allowed_categories(_enfant(11), "nimportequoi") == set()


def test_une_categorie_nouvelle_serait_bloquee_par_defaut():
    connues = set(ag.GRID_CATEGORIES) | ag.ALWAYS_BLOCKED | ag.ALWAYS_ALLOWED
    assert ag.allowed_categories(_enfant(18), modes.FREE) <= connues


def test_le_parent_personnalise_un_mode_sans_toucher_l_autre():
    serre = _enfant(17, {"free": ["education", "work"]})
    assert "social" in ag.blocked_categories(serre, modes.FREE)
    large = _enfant(7, {"free": ["education", "work", "other", "entertainment", "social"]})
    assert "entertainment" in ag.allowed_categories(large, modes.FREE)
    partiel = _enfant(11, {"homework": ["education"]})
    assert ag.grid_of(partiel)[modes.FREE] == ag.grid_of(_enfant(11))[modes.FREE]


@pytest.mark.parametrize("access", ["pas un dict", [], {"free": "pas une liste"}, {"free": None}])
def test_une_grille_illisible_retombe_sur_le_defaut(access):
    assert ag.grid_of(_enfant(11, access))[modes.FREE] == ag.grid_of(_enfant(11))[modes.FREE]


def test_describe_dit_d_ou_vient_chaque_ligne():
    vue = ag.describe(_enfant(11))
    assert vue["band"] == "10-12" and vue["modes"][modes.FREE]["custom"] is False
    assert vue["always_blocked"] == ["adult", "extremism"]
    perso = ag.describe(_enfant(11, {"free": ["education"]}))
    assert perso["modes"][modes.FREE]["custom"] is True
    assert perso["modes"][modes.HOMEWORK]["custom"] is False


def test_chaque_categorie_a_un_libelle_dans_les_quatre_langues():
    from pathlib import Path
    racine = Path(__file__).resolve().parent.parent
    for langue in ("fr", "en", "es", "pt"):
        t = json.loads((racine / "i18n" / f"{langue}.json").read_text())
        for categorie in set(ag.GRID_CATEGORIES) | ag.ALWAYS_BLOCKED | ag.ALWAYS_ALLOWED:
            assert t.get(f"category.{categorie}", "").strip(), f"{categorie} en {langue}"


# ------------------------------------------------------------------ #
#  La liste de blocage effective                                      #
# ------------------------------------------------------------------ #

def test_le_meme_mode_ne_bloque_pas_la_meme_chose_selon_l_age(db):
    _catalogue(youtube_com="entertainment", tiktok_com="social", wikipedia_org="education")
    petit, grand = _liste(modes.FREE, _enfant(7)), _liste(modes.FREE, _enfant(17))
    assert {"youtube.com", "tiktok.com"} <= set(petit)
    assert not {"youtube.com", "tiktok.com", "wikipedia.org"} & set(grand)


def test_les_devoirs_coupent_le_divertissement_a_tout_age(db):
    _catalogue(youtube_com="entertainment", wikipedia_org="education")
    for age in (7, 18):
        liste = _liste(modes.HOMEWORK, _enfant(age))
        assert "youtube.com" in liste and "wikipedia.org" not in liste


def test_un_mode_ferme_ne_renvoie_aucune_liste(db):
    """Le blocage total est un joker DNS sur le groupe Pi-hole, pas une énumération."""
    _catalogue(youtube_com="entertainment")
    assert _liste(modes.OFF, _enfant(11)) == []


def test_une_exception_ouvre_ou_ferme_un_domaine_dans_son_seul_mode(db):
    _catalogue(youtube_com="entertainment", wikipedia_org="education")
    db.set_domain_rule("alice", "youtube.com", modes.FREE, allow=True)
    db.set_domain_rule("alice", "wikipedia.org", modes.FREE, allow=False)
    assert "youtube.com" not in _liste(modes.FREE, _enfant(7))
    assert "wikipedia.org" in _liste(modes.FREE, _enfant(7))
    assert "youtube.com" in _liste(modes.HOMEWORK, _enfant(7))
    db.clear_domain_rule("alice", "youtube.com", modes.FREE)
    assert "youtube.com" in _liste(modes.FREE, _enfant(7))


def test_nominative_ou_globale_une_exception_vise_juste(db):
    """Autoriser YouTube pour l'aîné ne l'ouvre pas au cadet ; une règle pour un enfant
    l'emporte sur une règle pour toute la maison."""
    _catalogue(youtube_com="entertainment", jeu_fr="education")
    db.set_domain_rule("", "jeu.fr", modes.FREE, allow=False)
    db.set_domain_rule("", "youtube.com", modes.FREE, allow=False)
    db.set_domain_rule("aine", "youtube.com", modes.FREE, allow=True)
    assert "jeu.fr" in _liste(modes.FREE, _enfant(17), "aine")
    assert "youtube.com" not in _liste(modes.FREE, _enfant(17), "aine")
    assert "youtube.com" in _liste(modes.FREE, _enfant(17), "cadet")


@pytest.mark.parametrize("categorie", ["adult", "extremism"])
@pytest.mark.parametrize("mode", [modes.HOMEWORK, modes.FREE])
def test_aucune_exception_ne_rouvre_l_adulte_ni_l_extremisme(db, categorie, mode):
    """Le seul endroit où le produit refuse un ordre du parent."""
    _catalogue(mauvais_fr=categorie)
    db.set_domain_rule("alice", "mauvais.fr", mode, allow=True)
    db.set_domain_rule("", "mauvais.fr", mode, allow=True)
    triche = _enfant(18, {"free": ["adult", "social"]})
    assert "mauvais.fr" in _liste(mode, triche)


def test_les_anciens_blocages_par_colonne_deviennent_des_exceptions(db, config):
    import dashboard
    _catalogue(jeu_fr="education", radio_fr="education")
    with db.get_db() as conn:
        conn.execute("UPDATE domains SET blocked_work=1 WHERE domain='jeu.fr'")
        conn.execute("UPDATE domains SET blocked_permissive=1 WHERE domain='radio.fr'")
    assert dashboard._migrate_domain_rules() == 2
    assert "jeu.fr" in _liste(modes.HOMEWORK, _enfant(17), "x")
    assert "radio.fr" in _liste(modes.FREE, _enfant(17), "x")
    assert dashboard._migrate_domain_rules() == 0


def test_le_catalogue_distingue_aucune_exception_et_autorise(db):
    import domain_classifier as classifier
    _catalogue(jeu_fr="education", radio_fr="education", libre_fr="education")
    db.set_domain_rule("", "jeu.fr", modes.HOMEWORK, allow=False)
    db.set_domain_rule("", "radio.fr", modes.FREE, allow=True)
    vue = {d["domain"]: d for d in classifier.get_all_domains()}
    assert vue["jeu.fr"]["blocked_homework"] is True
    assert vue["radio.fr"]["blocked_free"] is False
    assert (vue["libre.fr"]["blocked_homework"], vue["libre.fr"]["blocked_free"]) == (None, None)


# ------------------------------------------------------------------ #
#  Déblocages temporaires                                             #
# ------------------------------------------------------------------ #

def test_un_deblocage_en_cours_survit_a_une_resynchronisation(db, config):
    """Défaut reproduit (P1-3) : toute resynchronisation rebloquait avant l'échéance."""
    _catalogue(jeu_fr="education")
    db.set_domain_rule("", "jeu.fr", modes.FREE, allow=False)
    db.set_temp_domain_unblock("jeu.fr", "alice", PLUS_TARD())
    alice, bruno = (config.data["profiles"][p] for p in ("alice", "bruno"))
    assert "jeu.fr" not in _liste(modes.FREE, alice, "alice")
    assert "jeu.fr" in _liste(modes.FREE, bruno, "bruno")
    db.set_temp_domain_unblock("jeu.fr", "alice", AVANT())
    assert "jeu.fr" in _liste(modes.FREE, alice, "alice")


def test_un_deblocage_ne_rouvre_pas_une_categorie_interdite(db, config):
    _catalogue(jeu_fr="adult")
    db.set_temp_domain_unblock("jeu.fr", "alice", PLUS_TARD())
    assert "jeu.fr" in _liste(modes.FREE, config.data["profiles"]["alice"], "alice")


def test_deux_enfants_deux_deblocages_deux_echeances(db):
    db.set_temp_domain_unblock("jeu.fr", "alice", AVANT())
    db.set_temp_domain_unblock("jeu.fr", "bruno", PLUS_TARD())
    assert [(r["domain"], r["profile"]) for r in db.pop_expired_domain_unblocks()] == [
        ("jeu.fr", "alice")]
    assert [r["profile"] for r in db.get_temp_domain_unblocks()] == ["bruno"]


def test_migration_de_la_cle_des_deblocages(db):
    with db.get_db() as conn:
        conn.execute("DROP TABLE temp_domain_unblocks")
        conn.execute("CREATE TABLE temp_domain_unblocks (domain TEXT PRIMARY KEY, "
                     "profile TEXT NOT NULL DEFAULT '', expires_at TEXT NOT NULL, "
                     "created_at TEXT NOT NULL)")
        conn.execute("INSERT INTO temp_domain_unblocks VALUES ('jeu.fr', 'alice', ?, 'x')",
                     (PLUS_TARD(),))
    db.init_db()
    db.init_db()
    db.set_temp_domain_unblock("jeu.fr", "bruno", PLUS_TARD())
    assert sorted(r["profile"] for r in db.get_temp_domain_unblocks()) == ["alice", "bruno"]


# ------------------------------------------------------------------ #
#  Résolveurs DNS-over-HTTPS (passerelle)                             #
# ------------------------------------------------------------------ #

@pytest.fixture
def catalogue_doh(tmp_path, monkeypatch):
    import doh_catalog

    def poser(contenu):
        chemin = tmp_path / "doh.json"
        chemin.write_text(contenu if isinstance(contenu, str) else json.dumps(contenu))
        monkeypatch.setattr(doh_catalog, "PATH", str(chemin))
        return str(chemin)
    poser({"domains": ["dns.google"], "ipv4": ["8.8.8.8"]})
    return poser


def test_le_catalogue_doh_livre_est_valide():
    import doh_catalog
    cat = doh_catalog.load()
    assert "dns.google" in cat["domains"] and "8.8.8.8" in cat["ipv4"]


@pytest.mark.parametrize("contenu,attendu", [
    ("{pas du json", {"domains": frozenset(), "ipv4": frozenset()}),
    ('{"domains": "x"}', {"domains": frozenset(), "ipv4": frozenset()}),
    ({"domains": ["dns.google", "pas un domaine"], "ipv4": ["8.8.8.8", "999.1.1.1", "::1"]},
     {"domains": frozenset({"dns.google"}), "ipv4": frozenset({"8.8.8.8"})}),
])
def test_un_catalogue_doh_abime_ne_leve_jamais(catalogue_doh, contenu, attendu):
    import doh_catalog
    assert doh_catalog.load(catalogue_doh(contenu)) == attendu


def test_les_resolveurs_doh_sont_refuses_en_passerelle_quoi_qu_il_arrive(db, config,
                                                                          catalogue_doh):
    profil = config.data["profiles"]["alice"]
    db.set_domain_rule("alice", "dns.google", modes.FREE, allow=True)
    db.set_temp_domain_unblock("dns.google", "alice", PLUS_TARD())
    assert "dns.google" in _liste(modes.FREE, profil)
    assert _liste(modes.OFF, profil) == []
    config.data["network"]["enforcement"] = "dns_only"
    config.save()
    assert "dns.google" not in _liste(modes.FREE, profil)
