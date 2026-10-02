# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Arnaud Ortais
# Dual-licensed: AGPL-3.0 (open source) or Commercial License — see LICENSE and LICENSE-COMMERCIAL.
"""
test_services.py — les services que le parent nomme, et ce que leur regroupement corrige.

Le regroupement existait en COMMENTAIRE dans le catalogue des catégories. Le rendre
explicite n'est pas cosmétique : il corrige un calcul de durée qui comptait la même
soirée plusieurs fois, et il évite qu'un pic se dilue sur quatre domaines dont aucun ne
paraît anormal.
"""

import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

import services as sv


# ------------------------------------------------------------------ #
#  Le catalogue des services                                          #
# ------------------------------------------------------------------ #

def test_chaque_service_a_un_libelle_et_une_categorie():
    import domain_classifier as classifier

    for cle, svc in sv.SERVICES.items():
        assert svc["label"].strip(), f"{cle} sans libellé"
        assert svc["category"] in classifier.VALID_CATEGORIES, f"{cle} : catégorie inconnue"
        assert svc["domains"], f"{cle} sans domaine"


@pytest.mark.parametrize("domaine,service", [
    ("youtube.com", "youtube"), ("googlevideo.com", "youtube"), ("ytimg.com", "youtube"),
    ("netflix.com", "netflix"), ("nflxvideo.net", "netflix"),
    ("scdn.co", "spotify"), ("fbcdn.net", "facebook"), ("cdninstagram.com", "instagram"),
    ("ttvnw.net", "twitch"),
])
def test_les_domaines_de_livraison_rejoignent_leur_service(domaine, service):
    """Ce sont eux qui portent le volume : les laisser à part revenait à ignorer l'usage
    réel au profit du domaine de façade, le moins bavard."""
    assert sv.service_of(domaine) == service


def test_un_domaine_inconnu_n_a_pas_de_service():
    assert sv.service_of("site-inconnu.fr") == ""
    assert sv.label_for_domain("site-inconnu.fr") == "site-inconnu.fr"


def test_la_correspondance_est_exacte_et_non_par_suffixe():
    """Un test par suffixe attraperait « faux-youtube.com », qui n'est pas YouTube."""
    assert sv.service_of("faux-youtube.com") == ""
    assert sv.service_of("youtube.com.attaquant.fr") == ""


def test_le_libelle_est_celui_que_le_parent_emploie():
    assert sv.label_for_domain("googlevideo.com") == "YouTube"
    assert sv.label_for_domain("scdn.co") == "Spotify"
    assert sv.label_for_domain("nflxvideo.net") == "Netflix"


# ------------------------------------------------------------------ #
#  Le catalogue de catégories en est DÉRIVÉ                           #
# ------------------------------------------------------------------ #

def test_le_catalogue_de_categories_est_derive_des_services():
    """Deux listes des mêmes domaines finissent par diverger : c'est ainsi que l'entrée
    de fbcdn.net avait cessé d'avoir un effet sans que personne ne le voie."""
    import domain_classifier as classifier

    assert classifier.OBVIOUS_CATEGORIES == sv.categories_by_domain()


def test_tout_domaine_de_service_est_categorise_sans_appel_sortant():
    import domain_classifier as classifier

    for cle, svc in sv.SERVICES.items():
        for domaine in svc["domains"]:
            assert classifier.OBVIOUS_CATEGORIES.get(domaine) == svc["category"]
            assert not classifier.is_cdn(domaine), f"{domaine} serait jeté par record_domain"


# ------------------------------------------------------------------ #
#  Le regroupement des compteurs                                      #
# ------------------------------------------------------------------ #

def test_les_compteurs_s_additionnent_par_service():
    """Des NOMBRES s'additionnent sans risque : ce sont des compteurs, pas des durées."""
    groupe = sv.group_by_service({"youtube.com": 10, "googlevideo.com": 300,
                                  "ytimg.com": 40, "site-inconnu.fr": 5})
    assert groupe == {"YouTube": 350, "site-inconnu.fr": 5}


# ------------------------------------------------------------------ #
#  LA correction : une durée ne s'additionne pas                      #
# ------------------------------------------------------------------ #

def _seance(db, profile, domaines, debut_heure=20, duree_min=40, pas=4):
    """Pose une séance où les domaines d'un service sont sollicités EN PARALLÈLE."""
    base = datetime.now().replace(hour=debut_heure, minute=0, second=0, microsecond=0)
    with db.get_db() as conn:
        for minute in range(0, duree_min + 1, pas):
            for domaine in domaines:
                conn.execute(
                    "INSERT INTO dns_timeline (timestamp, profile, domain) VALUES (?,?,?)",
                    ((base + timedelta(minutes=minute)).isoformat(), profile, domaine))


def test_une_seance_de_service_n_est_plus_comptee_plusieurs_fois(db):
    """Le défaut : les domaines d'un service sont sollicités en parallèle, donc
    additionner leurs durées comptait la même soirée autant de fois qu'il a de domaines.
    Le total affiché au parent pouvait dépasser la journée.
    """
    domaines = ["youtube.com", "googlevideo.com", "ytimg.com"]
    _seance(db, "alice", domaines, duree_min=40)

    # Chacun, pris seul, voit la séance entière.
    seuls = [db.estimate_session_minutes("alice", d) for d in domaines]
    assert all(m == seuls[0] for m in seuls)
    somme_naive = sum(seuls)

    # Le service, lui, ne la compte qu'une fois.
    par_service = db.get_time_spent_today("alice")
    assert list(par_service) == ["YouTube"]
    assert par_service["YouTube"] == seuls[0]
    assert somme_naive == 3 * par_service["YouTube"], "le défaut doit être reproductible"


def test_deux_services_distincts_s_additionnent_bien(db):
    """Le regroupement ne doit pas fusionner ce qui n'a rien à voir."""
    _seance(db, "alice", ["youtube.com", "ytimg.com"], debut_heure=18, duree_min=20)
    _seance(db, "alice", ["spotify.com", "scdn.co"], debut_heure=21, duree_min=20)
    par_service = db.get_time_spent_today("alice")
    assert set(par_service) == {"YouTube", "Spotify"}
    assert par_service["YouTube"] > 0 and par_service["Spotify"] > 0


def test_un_domaine_sans_service_garde_son_nom(db):
    _seance(db, "alice", ["site-inconnu.fr"], duree_min=10)
    assert "site-inconnu.fr" in db.get_time_spent_today("alice")


def test_estimate_accepte_un_domaine_ou_une_liste(db):
    _seance(db, "alice", ["youtube.com"], duree_min=20)
    seul = db.estimate_session_minutes("alice", "youtube.com")
    liste = db.estimate_session_minutes("alice", ["youtube.com"])
    assert seul == liste
    assert db.estimate_session_minutes("alice", []) == 0


# ------------------------------------------------------------------ #
#  L'anomalie se juge par service                                     #
# ------------------------------------------------------------------ #

def _historique(db, profile, domain, valeurs):
    with db.get_db() as conn:
        for i, n in enumerate(valeurs, start=1):
            jour = (date.today() - timedelta(days=i)).isoformat()
            conn.execute(
                "INSERT INTO daily_usage (date, profile, domain, queries) VALUES (?,?,?,?)",
                (jour, profile, domain, n))


def test_un_pic_dilue_sur_plusieurs_domaines_est_vu(db, config, moniteur):
    """LE cas de dilution, et il est banal : l'enfant change sa FAÇON d'utiliser un
    service.

    Jusqu'ici il ouvrait YouTube sans regarder longtemps, donc du trafic sur youtube.com
    et rien sur googlevideo.com. Ce soir il enchaîne les vidéos : youtube.com bouge à
    peine, et tout le volume part sur le domaine de diffusion, qui n'a AUCUNE habitude.

    Aucun des deux domaines ne peut se signaler seul : le premier n'a pas augmenté assez,
    le second n'a pas d'historique auquel se comparer. Le SERVICE, lui, a quadruplé.
    """
    import database as db_mod
    import domain_classifier as classifier

    # Habitude : une centaine de requêtes par jour, uniquement sur le domaine de façade.
    _historique(db, "alice", "youtube.com", [100, 100, 100])

    m = moniteur
    ip = config.data["profiles"]["alice"]["devices"][0]["ip"]
    par_ip = {ip: []}
    for domaine, n in (("youtube.com", 120), ("googlevideo.com", 300)):
        db_mod.increment_usage("alice", domaine, count=n)
        classifier.record_domain(domaine, "free")
        par_ip[ip].append(domaine)

    m._check_unusual_patterns("alice", config.data["profiles"]["alice"], par_ip,
                              classifier.categories_of(par_ip[ip]),
                              db_mod.get_usage_history())

    assert len(m._unusual_events) == 1, "un seul signalement, pour le service"
    e = m._unusual_events[0]
    assert e["domain"] == "YouTube"
    assert e["reason"] == "volume", "pas « première fois » : le service est connu de lui"
    assert e["count"] == 420, "le total du service, pas celui d'un de ses domaines"
    assert e["baseline"] == pytest.approx(100.0, abs=0.1)


def test_un_domaine_de_diffusion_neuf_n_est_pas_une_decouverte(db, config, moniteur):
    """Corollaire : basculer sur le domaine de diffusion d'un service déjà utilisé n'est
    pas « il a découvert quelque chose ». Raisonné par domaine, googlevideo.com sans
    historique aurait déclenché le signal « première fois » sur une catégorie sensible.
    """
    import database as db_mod
    import domain_classifier as classifier

    _historique(db, "alice", "youtube.com", [100, 100, 100])
    m = moniteur
    ip = config.data["profiles"]["alice"]["devices"][0]["ip"]
    db_mod.increment_usage("alice", "googlevideo.com", count=40)
    classifier.record_domain("googlevideo.com", "free")
    m._check_unusual_patterns("alice", config.data["profiles"]["alice"],
                              {ip: ["googlevideo.com"]},
                              classifier.categories_of(["googlevideo.com"]),
                              db_mod.get_usage_history())
    assert m._unusual_events == []


def test_un_signalement_par_service_et_non_par_domaine(db, config, moniteur):
    """Un pic sur un service écrivait autant de lignes au parent que le service a de
    domaines, et déclenchait donc une escalade vers le modèle pour une seule soirée."""
    import database as db_mod
    import domain_classifier as classifier

    for domaine in ("youtube.com", "googlevideo.com", "ytimg.com"):
        _historique(db, "alice", domaine, [30, 25, 35])
        db_mod.increment_usage("alice", domaine, count=400)
        classifier.record_domain(domaine, "free")

    m = moniteur
    ip = config.data["profiles"]["alice"]["devices"][0]["ip"]
    m._check_unusual_patterns(
        "alice", config.data["profiles"]["alice"],
        {ip: ["youtube.com", "googlevideo.com", "ytimg.com"]},
        classifier.categories_of(["youtube.com", "googlevideo.com", "ytimg.com"]),
        db_mod.get_usage_history())
    assert [e["domain"] for e in m._unusual_events] == ["YouTube"]


# ------------------------------------------------------------------ #
#  Le catalogue est de la DONNÉE, plus du code                        #
# ------------------------------------------------------------------ #
#
#  Ces tests portent sur le chargeur, et l'un d'eux porte sur le fichier livré
#  lui-même : c'est lui qui rend le rollback théorique. Un catalogue invalide
#  empêcherait l'agent de démarrer sur tous les boîtiers de la flotte, et ne serait
#  rattrapé que par l'updater. Autant ne jamais le publier.


@pytest.fixture
def catalogue(monkeypatch, tmp_path):
    """Permet de poser des catalogues d'appoint, et REMET l'état du module ensuite.

    `services.SERVICES` est un état de module construit à l'import : un test qui le
    laisserait modifié contaminerait tous les suivants, et le regroupement des durées
    ou la détection d'anomalie échoueraient à des endroits sans rapport.
    """
    d_origine, v_origine = sv.SERVICES, sv.CATALOG_VERSIONS

    class _Poseur:
        def local(self, contenu):
            return self._ecrire("services.local.json", "LOCAL_PATH", contenu)

        def _ecrire(self, nom, attribut, contenu):
            chemin = tmp_path / nom
            chemin.write_text(contenu if isinstance(contenu, str)
                              else json.dumps(contenu, ensure_ascii=False))
            monkeypatch.setattr(sv, attribut, str(chemin))
            return sv.reload_catalog()

    import domain_classifier as classifier
    c_origine = classifier.OBVIOUS_CATEGORIES

    yield _Poseur()

    sv.SERVICES, sv.CATALOG_VERSIONS = d_origine, v_origine
    sv._BY_DOMAIN = {dom: cle for cle, svc in d_origine.items() for dom in svc["domains"]}
    classifier.OBVIOUS_CATEGORIES = c_origine


def test_le_catalogue_livre_est_valide_et_donc_publiable():
    """LE test qui compte : ce fichier part sur tous les boîtiers, et s'il est invalide
    l'agent ne démarre pas. Ce test est la barrière avant publication, pas le rollback.
    """
    raw = json.loads(Path(sv.SHIPPED_PATH).read_text())
    propre = sv.validate(raw)
    assert propre == sv.SERVICES, "le fichier livré doit charger tel quel, sans appoint"
    assert raw.get("version"), "un catalogue sans version ne peut pas se comparer"


def test_les_categories_admises_sont_celles_du_produit():
    """Elles ne sont pas recopiées ici : une liste en dur dans le chargeur recréerait
    le doublon que ce chantier supprime, et accepterait une catégorie que la grille
    d'accès ne sait pas afficher."""
    import access_grid
    import domain_classifier as classifier

    assert sv.CATEGORIES == set(classifier.VALID_CATEGORIES) | {classifier.UNKNOWN_CATEGORY}
    assert set(access_grid.GRID_CATEGORIES) <= sv.CATEGORIES


@pytest.mark.parametrize("motif,attendu", [
    ({}, "services"),
    ({"services": {}}, "services"),
    ({"services": {"x": {"label": "X", "category": "education"}}}, "aucun domaine"),
    ({"services": {"x": {"label": "X", "category": "poney",
                         "domains": ["x.fr"]}}}, "catégorie inconnue"),
    ({"services": {"x": {"label": "", "category": "education",
                         "domains": ["x.fr"]}}}, "libellé"),
    ({"services": {"x": {"label": "X", "category": "education",
                         "domains": ["pas un domaine"]}}}, "domaine invalide"),
    ({"services": {"x": {"label": "X", "category": "education",
                         "domains": ["http://x.fr"]}}}, "domaine invalide"),
    ({"services": {"Majuscule": {"label": "X", "category": "education",
                                 "domains": ["x.fr"]}}}, "clé de service"),
    ({"services": {"a": {"label": "A", "category": "education", "domains": ["x.fr"]},
                   "b": {"label": "B", "category": "social",
                         "domains": ["x.fr"]}}}, "à la fois"),
])
def test_un_catalogue_malforme_dit_ou_est_l_erreur(motif, attendu):
    """Le message est le produit ici : il sera lu par quelqu'un qui édite du JSON à la
    main, souvent sur un boîtier, sans traceback utile."""
    with pytest.raises(sv.CatalogError) as e:
        sv.validate(motif)
    assert attendu in str(e.value)


def test_le_catalogue_livre_malforme_empeche_le_chargement(monkeypatch, tmp_path):
    """Volontaire, et c'est le point délicat : refuser de charger fait échouer le
    démarrage de l'agent, donc l'updater retombe sur le commit précédent. Charger à
    moitié ferait pire : les domaines perdus redeviendraient « non classés », donc
    AUTORISÉS, et le filtrage se desserrerait sans que personne ne le voie.
    """
    casse = tmp_path / "casse.json"
    casse.write_text("{ceci n'est pas du json")
    monkeypatch.setattr(sv, "SHIPPED_PATH", str(casse))
    with pytest.raises(sv.CatalogError):
        sv.load()

    monkeypatch.setattr(sv, "SHIPPED_PATH", str(tmp_path / "absent.json"))
    with pytest.raises(sv.CatalogError):
        sv.load()


def test_un_appoint_malforme_est_ignore_et_le_boitier_demarre(catalogue, caplog):
    """L'inverse du cas précédent, et pour une raison : l'appoint est édité à la main sur
    un boîtier vivant. Une virgule en trop ne doit pas couper la maison."""
    with caplog.at_level("WARNING"):
        charge = catalogue.local("{pas du json")
    assert charge == sv.SERVICES
    assert sv.service_of("youtube.com") == "youtube", "le livré reste en place"
    assert "ignoré" in caplog.text, "ignoré, mais jamais en silence"


def test_un_appoint_ajoute_un_service_sans_toucher_au_code(catalogue):
    """C'est l'intérêt du fichier : reconnaître un service ce soir, sans publication."""
    catalogue.local({"version": "essai", "services": {
        "arte": {"label": "Arte", "category": "education",
                 "domains": ["arte.tv", "artecdn.net"]}}})
    assert sv.service_of("artecdn.net") == "arte"
    assert sv.label_for_domain("arte.tv") == "Arte"
    assert sv.category_of("arte") == "education"
    assert sv.CATALOG_VERSIONS["local"] == "essai"
    assert sv.service_of("youtube.com") == "youtube", "le livré n'est pas perdu"


def test_un_appoint_remplace_le_service_entier_et_non_ses_domaines(catalogue):
    """Remplacer et non compléter est le seul comportement qui permette de RETIRER un
    domaine mal classé sans publier de code. C'est aussi ce qu'attend quelqu'un qui lit
    son propre fichier : ce qu'il y écrit est ce qui s'applique.
    """
    assert "ytimg.com" in sv.domains_of("youtube")
    catalogue.local({"services": {
        "youtube": {"label": "YouTube", "category": "entertainment",
                    "domains": ["youtube.com"]}}})
    assert sv.domains_of("youtube") == ("youtube.com",)
    assert sv.service_of("ytimg.com") == "", "le domaine retiré ne revient pas"


def test_un_appoint_qui_se_contredit_avec_le_livre_est_ecarte_en_bloc(catalogue, caplog):
    """Réclamer un domaine déjà pris par un autre service rendrait l'attribution du temps
    passé indéterminée. On écarte l'appoint ENTIER : l'appliquer à moitié donnerait un
    catalogue que personne n'a écrit, ni le livré ni le fichier local.
    """
    with caplog.at_level("WARNING"):
        catalogue.local({"services": {
            "monservice": {"label": "Mon service", "category": "other",
                           "domains": ["ytimg.com"]}}})
    assert sv.service_of("ytimg.com") == "youtube"
    assert "monservice" not in sv.SERVICES
    assert "écartée" in caplog.text


def test_le_boitier_ne_va_rien_chercher_sur_le_reseau(catalogue):
    """Deux sources, et pas une troisième : le catalogue voyage dans la publication que
    l'updater va déjà chercher. Une récupération périodique propre au catalogue serait un
    appel sortant de plus, donc un signal disant « ce boîtier existe et tourne », et le
    produit n'en émet aucun.
    """
    import inspect

    assert not hasattr(sv, "CACHE_PATH")
    source = inspect.getsource(sv)
    for interdit in ("urllib", "requests", "http"):
        assert interdit not in source, f"{interdit} : le catalogue ne se télécharge pas"


def test_un_catalogue_demesure_est_refuse():
    """Un fichier venu du réseau ou d'un éditeur maladroit ne doit pas pouvoir faire
    gonfler la mémoire du boîtier ni peser sur chaque requête DNS."""
    gros = {f"s{i}": {"label": f"S{i}", "category": "other", "domains": [f"d{i}.fr"]}
            for i in range(sv.MAX_SERVICES + 1)}
    with pytest.raises(sv.CatalogError):
        sv.validate({"services": gros})


def test_le_chargement_normalise_ce_qu_il_accepte(catalogue):
    """Un fichier écrit à la main contient des majuscules et des espaces. Les normaliser
    au chargement évite que la recherche échoue sur « YouTube.COM »."""
    propre = sv.validate({"services": {
        "x": {"label": "  X  ", "category": "Education",
              "domains": ["  EXEMPLE.FR  ", "exemple.fr"]}}})
    assert propre["x"]["label"] == "X"
    assert propre["x"]["category"] == "education"
    assert propre["x"]["domains"] == ("exemple.fr",), "et le doublon est fondu"


# ------------------------------------------------------------------ #
#  Le rechargement à chaud                                            #
# ------------------------------------------------------------------ #
#
#  C'est ce qui donne au catalogue son propre cycle de mise à jour : une publication
#  qui ne touche que catalog/ est appliquée sans réinstaller les dépendances ni
#  redémarrer les services, et un appoint local prend effet au cycle suivant.


def test_le_rechargement_refait_la_categorisation_derivee(catalogue):
    """LE risque de ce mécanisme, et la raison pour laquelle le rechargement a un point
    de passage unique.

    OBVIOUS_CATEGORIES est un instantané du catalogue construit à l'import. Recharger
    l'un sans l'autre donnerait le pire état possible : le nouveau domaine serait nommé
    correctement au parent dans les rapports, tout en restant « non classé » pour le
    filtrage, donc AUTORISÉ quel que soit le mode.
    """
    import domain_classifier as classifier

    assert "arte.tv" not in classifier.OBVIOUS_CATEGORIES

    # Le fichier est posé, mais SANS passer par le point de rechargement du produit :
    # c'est l'état que l'on veut éprouver, celui où seul le catalogue a bougé.
    catalogue.local({"services": {
        "arte": {"label": "Arte", "category": "education", "domains": ["arte.tv"]}}})
    assert sv.service_of("arte.tv") == "arte"
    assert "arte.tv" not in classifier.OBVIOUS_CATEGORIES, "l'instantané est bien périmé"

    combien = classifier.reload_services()

    assert combien == len(sv.SERVICES)
    assert classifier.OBVIOUS_CATEGORIES == sv.categories_by_domain()
    assert classifier.OBVIOUS_CATEGORIES["arte.tv"] == "education"
    assert not classifier.is_cdn("arte.tv"), "sinon record_domain le jetterait"


def test_le_cycle_recharge_sans_redemarrage(catalogue, config, pihole):
    """Un appoint écrit pendant que le boîtier tourne prend effet au cycle suivant, sans
    qu'un service soit redémarré ni qu'un parent soit coupé du réseau."""
    import domain_classifier as classifier
    import monitor

    m = monitor.ProtectadoMonitor.__new__(monitor.ProtectadoMonitor)

    m._maybe_reload_services()          # premier passage : relève l'empreinte
    empreinte = m._services_stamp
    assert empreinte is not None

    catalogue.local({"services": {
        "arte": {"label": "Arte", "category": "education", "domains": ["arte.tv"]}}})
    # Le poseur a déjà rechargé le module ; on remet l'état d'avant pour éprouver le
    # cycle lui-même, qui est seul censé s'en apercevoir.
    sv.reload_catalog()
    m._services_stamp = empreinte
    m._maybe_reload_services()

    assert m._services_stamp != empreinte
    assert sv.service_of("arte.tv") == "arte"
    assert classifier.OBVIOUS_CATEGORIES.get("arte.tv") == "education"


def test_le_premier_passage_du_cycle_ne_recharge_rien(catalogue):
    """Le catalogue vient d'être chargé à l'import : relire deux fichiers au démarrage
    n'apporterait rien, et masquerait le premier vrai changement."""
    import monitor

    m = monitor.ProtectadoMonitor.__new__(monitor.ProtectadoMonitor)
    appels = []
    import domain_classifier as classifier
    original = classifier.reload_services
    classifier.reload_services = lambda: appels.append(1) or 0
    try:
        m._maybe_reload_services()
    finally:
        classifier.reload_services = original
    assert appels == []


def test_un_catalogue_livre_casse_en_cours_de_route_ne_coupe_pas_le_boitier(catalogue,
                                                                           monkeypatch,
                                                                           tmp_path,
                                                                           capsys):
    """Asymétrie voulue avec le démarrage : à l'import, un catalogue cassé DOIT empêcher
    l'agent de démarrer, pour que l'updater fasse son rollback. En cours de service, il
    n'y a plus de rollback possible et couper la maison serait absurde : on garde ce
    qu'on a en mémoire.
    """
    import monitor

    avant = dict(sv.SERVICES)
    m = monitor.ProtectadoMonitor.__new__(monitor.ProtectadoMonitor)
    m._maybe_reload_services()

    casse = tmp_path / "casse.json"
    casse.write_text("{")
    monkeypatch.setattr(sv, "SHIPPED_PATH", str(casse))
    m._services_stamp = ("change",)
    m._maybe_reload_services()

    assert sv.SERVICES == avant, "le catalogue en mémoire survit"
    assert sv.service_of("youtube.com") == "youtube"
    assert "refusé" in capsys.readouterr().out


def test_l_empreinte_voit_apparaitre_l_appoint(catalogue, monkeypatch, tmp_path):
    """Un fichier absent compte comme 0 : son apparition est donc un changement, sinon un
    appoint créé après le démarrage ne serait jamais lu."""
    absent = tmp_path / "pas-encore.json"
    monkeypatch.setattr(sv, "LOCAL_PATH", str(absent))
    vide = sv.stamp()
    absent.write_text(json.dumps({"services": {
        "arte": {"label": "Arte", "category": "other", "domains": ["arte.tv"]}}}))
    assert sv.stamp() != vide
