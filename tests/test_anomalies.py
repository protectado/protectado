# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Arnaud Ortais
# Dual-licensed: AGPL-3.0 (open source) or Commercial License — see LICENSE and LICENSE-COMMERCIAL.
"""
test_anomalies.py — la détection d'anomalies, et ce qu'elle ne doit PAS signaler.

Le défaut corrigé ici coûtait de l'argent et de la crédibilité : « inhabituel » voulait
dire « absent du catalogue », alors que le cycle vient d'y inscrire chaque domaine. Seule
l'infrastructure partagée restait absente, et c'est elle qui franchit le plus vite
n'importe quel seuil de volume. Le produit faisait donc commenter du trafic de CDN par un
modèle payant, cinq fois de suite pour une seule rafale.
"""

from datetime import date, datetime, timedelta

import pytest


# ------------------------------------------------------------------ #
#  Outils                                                             #
# ------------------------------------------------------------------ #

def _cycle(m, profile_key, domain, hits, known=None, history=None):
    """Joue un cycle de détection pour un profil : `hits` requêtes vers `domain`.

    Reproduit ce que fait run_cycle : comptabilisation d'abord (c'est elle qui alimente
    daily_usage, donc le total du jour), détection ensuite.
    """
    import database as db
    import domain_classifier as classifier

    ip = m.config["profiles"][profile_key]["devices"][0]["ip"]
    # increment_usage prend un `count` : l'appeler `hits` fois ouvrirait autant de
    # connexions sqlite, et la suite passait de deux secondes à deux minutes. Le critère
    # ne lit que le TOTAL du jour, donc un incrément groupé est strictement équivalent.
    db.increment_usage(profile_key, domain, count=hits)
    classifier.record_domain(domain, "permissive")
    by_ip_new = {ip: [domain] * hits}
    m._check_unusual_patterns(
        profile_key, m.config["profiles"][profile_key], by_ip_new,
        known if known is not None else classifier.categories_of([domain]),
        history if history is not None else db.get_usage_history(),
    )
    return m._unusual_events


def _historique(db, profile, domain, valeurs):
    """Pose un historique des jours PRÉCÉDENTS pour (profil, domaine)."""
    with db.get_db() as conn:
        for i, n in enumerate(valeurs, start=1):
            jour = (date.today() - timedelta(days=i)).isoformat()
            conn.execute(
                "INSERT INTO daily_usage (date, profile, domain, queries) VALUES (?,?,?,?)",
                (jour, profile, domain, n))


# ------------------------------------------------------------------ #
#  1.1 — le critère                                                   #
# ------------------------------------------------------------------ #

def test_infrastructure_partagee_ne_produit_aucun_evenement(db, moniteur):
    """Le défaut d'origine : un flux massif vers un CDN ne doit rien déclencher.

    gstatic.com est de l'infrastructure : son nom ne dit rien de ce que l'enfant faisait.
    """
    m = moniteur
    _historique(db, "alice", "gstatic.com", [100, 120, 90])
    assert _cycle(m, "alice", "gstatic.com", 900) == []


def test_pic_reel_sur_un_domaine_ordinaire_est_signale(db, moniteur):
    """Quatre fois sa propre moyenne, au-dessus du plancher : c'est le signal cherché."""
    m = moniteur
    _historique(db, "alice", "roblox.com", [30, 25, 35])
    evenements = _cycle(m, "alice", "roblox.com", 400)
    assert len(evenements) == 1
    assert evenements[0]["domain"] == "roblox.com"
    assert evenements[0]["reason"] == "volume"
    assert evenements[0]["profile"] == "alice"


def test_usage_habituel_ne_declenche_rien(db, moniteur):
    """Un volume élevé mais CONFORME à l'habitude de cet enfant n'est pas une anomalie."""
    m = moniteur
    _historique(db, "alice", "youtube.com", [400, 380, 420])
    assert _cycle(m, "alice", "youtube.com", 410) == []


def test_pas_de_reference_pas_de_verdict(db, moniteur):
    """Sans historique suffisant, le volume ne dit rien : on ne juge pas.

    Le domaine choisi est de catégorie `work`, donc le signal « première fois » ne
    s'applique pas non plus. Refuser de conclure vaut mieux qu'alerter sur le premier
    jour d'usage d'un boîtier neuf, où tout est un pic par construction.
    """
    m = moniteur
    assert _cycle(m, "alice", "office.com", 900) == []


def test_premiere_fois_sur_categorie_sensible(db, moniteur):
    """Domaine déjà classé, jamais vu chez cet enfant : c'est le second signal.

    Le catalogue est prérempli par un AUTRE enfant, ce qui est le cas réel : Bruno
    connaît tiktok.com, Alice le découvre.

    L'enfant doit déjà être observé depuis quelques jours, sinon « première fois » ne
    veut rien dire : on lui pose donc un historique sur un autre domaine.
    """
    import domain_classifier as classifier

    m = moniteur
    _historique(db, "alice", "wikipedia.org", [40, 35, 30])
    classifier.record_domain("tiktok.com", "permissive")   # connu du catalogue
    evenements = _cycle(m, "alice", "tiktok.com", 40)
    assert len(evenements) == 1
    assert evenements[0]["reason"] == "premiere_fois"
    assert evenements[0]["category"] == "social"


def test_premiere_fois_muette_sur_un_boitier_neuf(db, moniteur):
    """LE défaut constaté : le premier soir, tout est une première fois.

    Chaque site social ou de divertissement déclenchait le signal, donc une escalade vers
    le modèle tous les trois domaines, et autant de paragraphes dans le journal du parent.
    Sans historique, on ne prétend rien savoir de cet enfant.
    """
    import domain_classifier as classifier

    m = moniteur
    for domaine in ("tiktok.com", "spotify.com", "whatsapp.com", "signal.org"):
        classifier.record_domain(domaine, "permissive")
        _cycle(m, "alice", domaine, 40)
    assert m._unusual_events == []


def test_premiere_fois_muette_avec_un_historique_trop_court(db, moniteur):
    """Deux jours d'observation ne suffisent pas à savoir ce qui est habituel."""
    import domain_classifier as classifier

    m = moniteur
    _historique(db, "alice", "wikipedia.org", [40, 35])     # deux jours seulement
    classifier.record_domain("tiktok.com", "permissive")
    assert _cycle(m, "alice", "tiktok.com", 40) == []


def test_premiere_fois_ignoree_sur_categorie_anodine(db, moniteur):
    """Un premier accès à un site de travail n'est pas un événement à faire commenter."""
    import domain_classifier as classifier

    m = moniteur
    _historique(db, "alice", "wikipedia.org", [40, 35, 30])
    classifier.record_domain("office.com", "work")
    assert _cycle(m, "alice", "office.com", 40) == []


def test_premiere_fois_sous_le_plancher(db, moniteur):
    """Un clic isolé sur un site social n'est pas une découverte à signaler."""
    import domain_classifier as classifier

    m = moniteur
    _historique(db, "alice", "wikipedia.org", [40, 35, 30])
    classifier.record_domain("tiktok.com", "permissive")
    assert _cycle(m, "alice", "tiktok.com", 3) == []


# ------------------------------------------------------------------ #
#  1.2 — la rafale ne compte qu'une fois                              #
# ------------------------------------------------------------------ #

def test_une_rafale_relue_sur_cinq_cycles_ne_donne_qu_un_evenement(db, moniteur):
    """La fenêtre Pi-hole fait 5 min, le cycle 60 s : la même rafale revient cinq fois.

    Un événement, un seul, et donc un seul appel au modèle au bout de la chaîne.
    """
    m = moniteur
    _historique(db, "alice", "roblox.com", [30, 25, 35])
    for _ in range(5):
        _cycle(m, "alice", "roblox.com", 400)
    assert len(m._unusual_events) == 1


def test_un_domaine_absent_du_cycle_ne_se_resignale_pas(db, moniteur):
    """Le total du jour reste élevé toute la soirée : ce n'est pas une raison pour
    resignaler le pic à chaque cycle où l'enfant ne touche plus au domaine."""
    m = moniteur
    _historique(db, "alice", "roblox.com", [30, 25, 35])
    _cycle(m, "alice", "roblox.com", 400)
    m._unusual_events.clear()
    m._unusual_seen.clear()          # on oublie la déduplication pour isoler la cause
    _cycle(m, "alice", "wikipedia.org", 2)
    assert m._unusual_events == []


# ------------------------------------------------------------------ #
#  Pas de confusion entre enfants                                     #
# ------------------------------------------------------------------ #

def test_une_seule_requete_globale_ne_melange_pas_les_enfants(db, moniteur):
    """L'historique est lu en UNE requête pour tous les profils. La clé porte le profil,
    donc l'habitude de l'un ne sert jamais de référence à l'autre.

    Bruno regarde beaucoup Twitch depuis des jours, Alice jamais. Les mêmes 400 requêtes
    sont banales chez Bruno et anormales chez Alice, et c'est bien ce qui doit sortir.
    """
    import database as db_mod

    m = moniteur
    _historique(db, "bruno", "ttvnw.net", [400, 380, 420])
    _historique(db, "alice", "ttvnw.net", [20, 25, 22])

    histoire = db_mod.get_usage_history()
    assert histoire[("bruno", "ttvnw.net")] != histoire[("alice", "ttvnw.net")]

    _cycle(m, "bruno", "ttvnw.net", 410)
    assert m._unusual_events == [], "l'usage habituel de Bruno ne doit rien déclencher"

    _cycle(m, "alice", "ttvnw.net", 410)
    assert len(m._unusual_events) == 1
    assert m._unusual_events[0]["profile"] == "alice"
    assert m._unusual_events[0]["baseline"] == pytest.approx(22.3, abs=0.1)


def test_le_meme_domaine_se_signale_pour_chaque_enfant_concerne(db, moniteur):
    """Deux enfants, deux anomalies : la déduplication est par enfant, pas par domaine."""
    m = moniteur
    for enfant in ("alice", "bruno"):
        _historique(db, enfant, "roblox.com", [30, 25, 35])
    _cycle(m, "alice", "roblox.com", 400)
    _cycle(m, "bruno", "roblox.com", 400)
    assert {e["profile"] for e in m._unusual_events} == {"alice", "bruno"}


# ------------------------------------------------------------------ #
#  1.3 — coût de lecture                                              #
# ------------------------------------------------------------------ #

def test_lecture_du_catalogue_ciblee(db):
    """categories_of ne renvoie que ce qu'on demande, et distingue « jamais vu » de
    « vu et non classé »."""
    import domain_classifier as classifier

    classifier.record_domain("youtube.com", "permissive")
    classifier.record_domain("domaine-inconnu.fr", "permissive")

    trouve = classifier.categories_of(["youtube.com", "domaine-inconnu.fr", "jamais-vu.fr"])
    assert trouve == {"youtube.com": "entertainment", "domaine-inconnu.fr": "unknown"}
    assert classifier.categories_of([]) == {}
    assert classifier.categories_of(["jamais-vu.fr"]) == {}


def test_lecture_du_catalogue_supporte_beaucoup_de_domaines(db):
    """Découpage en lots : SQLite plafonne le nombre de paramètres d'une requête."""
    import domain_classifier as classifier

    noms = [f"site{i}.fr" for i in range(1200)]
    for nom in noms[:3]:
        classifier.record_domain(nom, "work")
    trouve = classifier.categories_of(noms)
    assert set(trouve) == set(noms[:3])


def test_historique_exclut_le_jour_en_cours(db):
    """La référence du comportement normal ne doit pas contenir le pic qu'on juge."""
    import database as db_mod
    from datetime import date as _date

    db_mod.increment_usage("alice", "roblox.com")
    _historique(db, "alice", "roblox.com", [30])
    par_date = db_mod.get_usage_history()[("alice", "roblox.com")]
    assert list(par_date.values()) == [30]
    assert _date.today().isoformat() not in par_date


def test_l_historique_porte_les_dates(db):
    """Indexé par date, et pas en liste anonyme : pour comparer l'habitude d'un SERVICE,
    il faut additionner ses domaines jour par jour. Deux listes non alignées donneraient
    une moyenne qui ne correspond à aucune journée réelle."""
    import database as db_mod
    from datetime import date as _date, timedelta as _td

    _historique(db, "alice", "youtube.com", [100, 90])
    _historique(db, "alice", "googlevideo.com", [300, 280])
    histoire = db_mod.get_usage_history()

    hier = (_date.today() - _td(days=1)).isoformat()
    avant = (_date.today() - _td(days=2)).isoformat()
    assert histoire[("alice", "youtube.com")] == {hier: 100, avant: 90}
    assert histoire[("alice", "googlevideo.com")] == {hier: 300, avant: 280}
    # Additionnés par jour, les deux domaines donnent l'habitude du SERVICE.
    par_jour = {j: histoire[("alice", "youtube.com")][j]
                   + histoire[("alice", "googlevideo.com")][j]
                for j in (hier, avant)}
    assert par_jour == {hier: 400, avant: 370}


# ------------------------------------------------------------------ #
#  1.4 — extremism dans le prompt                                     #
# ------------------------------------------------------------------ #

def test_le_prompt_de_classification_porte_les_huit_categories():
    """extremism est la seule catégorie bloquée à tout âge sans recours parental, et elle
    était absente de la liste soumise au modèle : il ne pouvait structurellement pas
    l'attribuer, alors que le produit promet de la bloquer."""
    import re
    from pathlib import Path

    source = Path(__file__).resolve().parent.parent / "domain_classifier.py"
    texte = source.read_text()
    bloc = texte[texte.index("Catégories possibles"):texte.index("Domaines à catégoriser")]
    categories = re.findall(r"^- (\w+)", bloc, re.M)
    # Huit catégories exploitables, plus `unknown` qui n'est pas une catégorie mais un
    # aveu d'ignorance (cf. lot 2.2) : il ne s'écrit jamais en base.
    assert categories == ["education", "work", "entertainment", "social",
                          "adult", "extremism", "cdn", "other", "unknown"]
    import domain_classifier as classifier
    assert set(categories) - {"unknown"} == set(classifier.VALID_CATEGORIES)
    # L'asymétrie doit être dite au modèle : une fausse attribution est irréversible
    # côté parent.
    consigne = texte[texte.index('IMPORTANT sur "extremism"'):]
    assert "TOUT ÂGE" in consigne and "certitude" in consigne


# ------------------------------------------------------------------ #
#  La distinction infrastructure / livraison de service               #
# ------------------------------------------------------------------ #

DOMAINES_DE_LIVRAISON = [
    ("googlevideo.com", "entertainment"),   # vidéo YouTube
    ("ytimg.com", "entertainment"),
    ("nflxvideo.net", "entertainment"),     # flux Netflix
    ("ttvnw.net", "entertainment"),         # vidéo Twitch
    ("jtvnw.net", "entertainment"),
    ("sndcdn.com", "entertainment"),        # audio SoundCloud
    ("scdn.co", "entertainment"),           # audio Spotify
    ("fbcdn.net", "social"),                # médias Facebook et Instagram
    ("cdninstagram.com", "social"),
    ("sc-cdn.net", "social"),               # Snapchat
    ("tiktokcdn.com", "social"),
    ("twimg.com", "social"),
    ("discordcdn.com", "social"),
    ("redd.it", "social"),
    ("whatsapp.net", "social"),
    ("cdn-telegram.org", "social"),
]


@pytest.mark.parametrize("domaine,categorie", DOMAINES_DE_LIVRAISON)
def test_un_domaine_de_livraison_est_du_vrai_usage(db, domaine, categorie):
    """Ces noms crient « CDN » mais portent l'usage réel : les ranger en infrastructure
    les ferait disparaître des compteurs, du temps passé et de la détection.

    Deux d'entre eux étaient bel et bien perdus : « cdn.net » dans CDN_PATTERNS est testé
    en suffixe, donc il attrapait fbcdn.net et sc-cdn.net, dont l'entrée au catalogue
    était morte.
    """
    import domain_classifier as classifier

    assert not classifier.is_cdn(domaine)
    classifier.record_domain(domaine, "permissive")
    connus = {d["domain"]: d["category"] for d in classifier.get_all_domains()}
    assert connus.get(domaine) == categorie


def test_l_infrastructure_reste_ecartee(db):
    """L'inverse doit rester vrai, sinon le catalogue se remplit de bruit."""
    import domain_classifier as classifier

    for domaine in ("gstatic.com", "googleapis.com", "google-analytics.com",
                    "cloudfront.net", "akamaized.net", "inconnu-cdn.net"):
        assert classifier.is_cdn(domaine)
        classifier.record_domain(domaine, "permissive")
    assert classifier.get_all_domains() == []


def test_aucune_entree_du_catalogue_n_est_morte(db):
    """Garde-fou durable : si quelqu'un ajoute un motif générique à CDN_PATTERNS qui
    attrape un domaine nommément catalogué, ce test le dit tout de suite."""
    import domain_classifier as classifier

    mortes = [d for d in classifier.OBVIOUS_CATEGORIES
              if any(d.endswith(p) for p in classifier.CDN_PATTERNS)]
    # fbcdn.net et sc-cdn.net sont dans ce cas : is_cdn les sauve parce que le catalogue
    # explicite l'emporte. Le test vérifie ce sauvetage, pas l'absence de collision.
    for domaine in mortes:
        assert not classifier.is_cdn(domaine), f"{domaine} serait jeté par record_domain"


# ------------------------------------------------------------------ #
#  Le câblage : run_cycle passe bien la bonne vue et les prélectures  #
# ------------------------------------------------------------------ #

def test_run_cycle_signale_le_pic_une_seule_fois(db, action_queue, pihole, moniteur):
    """Cycle complet : la rafale est vue, signalée une fois, et pas cinq."""
    m = moniteur
    _historique(db, "alice", "roblox.com", [20, 20, 20])

    # Fenêtre Pi-hole : 90 requêtes d'Alice vers roblox.com, avec un identifiant par
    # requête pour que la progression de comptabilisation puisse les distinguer.
    fenetre = [{"client": "192.168.50.52", "domain": "roblox.com",
                "id": i, "time": 1790000000 + i} for i in range(90)]
    pihole.returns["get_recent_queries"] = fenetre

    m.run_cycle()
    assert len(m._unusual_events) == 1
    assert m._unusual_events[0]["reason"] == "volume"

    # La même fenêtre relue au cycle suivant : aucune requête neuve, aucun doublon.
    m.run_cycle()
    assert len(m._unusual_events) == 1


def test_run_cycle_ignore_l_infrastructure(db, action_queue, pihole, moniteur):
    """Même volume, mais sur de l'infrastructure : rien ne doit remonter."""
    m = moniteur
    _historique(db, "alice", "gstatic.com", [50, 60, 40])
    pihole.returns["get_recent_queries"] = [
        {"client": "192.168.50.52", "domain": "gstatic.com", "id": i, "time": 1790000000 + i}
        for i in range(90)]

    m.run_cycle()
    assert m._unusual_events == []


def test_l_escalade_reste_locale_quand_le_partage_est_coupe(db, moniteur):
    """Au troisième événement, l'escalade part. Avec le partage IA coupé, elle ne sort
    pas de la machine : c'est ce qui garantit qu'aucun test ne dépend du réseau.

    Le compteur est remis à zéro par l'escalade, ce qui est le comportement voulu : le
    tampon sert à grouper, pas à conserver.
    """
    m = moniteur
    for enfant, domaine in (("alice", "roblox.com"), ("bruno", "roblox.com"),
                            ("alice", "minecraft.net")):
        _historique(db, enfant, domaine, [30, 25, 35])
    _cycle(m, "alice", "roblox.com", 400)
    _cycle(m, "bruno", "roblox.com", 400)
    assert len(m._unusual_events) == 2
    _cycle(m, "alice", "minecraft.net", 400)
    # Trois événements atteints : _escalate_to_claude a vidé le tampon et confié le lot
    # à un fil, attendu ici pour que le test ne se termine pas avant lui.
    assert m._unusual_events == []
    m._escalation_thread.join(5)


# ------------------------------------------------------------------ #
#  Le compte rendu de l'escalade                                      #
# ------------------------------------------------------------------ #

def test_l_escalade_n_ecrit_qu_une_ligne_au_journal(db, config, monkeypatch):
    """Trois événements groupés donnaient trois fois la MÊME analyse au journal, chacune
    accrochée à un domaine et tronquée au milieu d'une phrase. Le tampon sert à grouper
    l'appel au modèle, pas à multiplier le compte rendu."""
    import claude_agent

    cfg = config.data
    cfg["privacy"] = {"share_with_ai": True}
    cfg["openrouter"] = {"api_key": "cle-de-test", "model": "modele/de-test"}
    config.save()

    analyse = ("## Analyse de l'activité réseau\n\n**Enfant 1 (13-15 ans)** a accédé à "
               "des messageries chiffrées. *Rien* d'anormal.")

    class _Faux:
        def __init__(self):
            self.chat = type("C", (), {"completions": self})()

        def create(self, **kwargs):
            message = type("M", (), {"content": analyse})()
            return type("R", (), {"choices": [type("Ch", (), {"message": message})()]})()

    monkeypatch.setattr(claude_agent, "_get_client", lambda config: (_Faux(), "modele"))
    claude_agent.analyze_unusual_patterns([
        {"profile": "alice", "domain": "whatsapp.com", "count": 40,
         "timestamp": datetime.now().isoformat()},
        {"profile": "alice", "domain": "whatsapp.net", "count": 30,
         "timestamp": datetime.now().isoformat()},
        {"profile": "alice", "domain": "signal.org", "count": 25,
         "timestamp": datetime.now().isoformat()},
    ])

    evenements = db.get_recent_events()
    assert len(evenements) == 1
    e = evenements[0]
    assert e["profile"] == "alice"
    # Les trois domaines sont nommés sur la seule ligne, plutôt qu'un par ligne.
    assert e["domain"] == "signal.org, whatsapp.com, whatsapp.net"
    # Markdown replié : pas de titre ni d'astérisque dans une ligne de journal.
    assert "##" not in e["message"] and "**" not in e["message"]
    assert e["message"].startswith("Analyse de l'activité réseau")


def test_l_escalade_de_deux_enfants_n_est_attribuee_a_personne(db, config, monkeypatch):
    """Si le tampon mélange deux enfants, la ligne est globale : l'attribuer à l'un des
    deux serait faux pour l'autre."""
    import claude_agent

    cfg = config.data
    cfg["privacy"] = {"share_with_ai": True}
    cfg["openrouter"] = {"api_key": "cle-de-test", "model": "modele/de-test"}
    config.save()

    class _Faux:
        def __init__(self):
            self.chat = type("C", (), {"completions": self})()

        def create(self, **kwargs):
            message = type("M", (), {"content": "Analyse."})()
            return type("R", (), {"choices": [type("Ch", (), {"message": message})()]})()

    monkeypatch.setattr(claude_agent, "_get_client", lambda config: (_Faux(), "modele"))
    claude_agent.analyze_unusual_patterns([
        {"profile": "alice", "domain": "tiktok.com", "count": 40,
         "timestamp": datetime.now().isoformat()},
        {"profile": "bruno", "domain": "roblox.com", "count": 40,
         "timestamp": datetime.now().isoformat()},
    ])
    evenements = db.get_recent_events()
    assert len(evenements) == 1
    assert evenements[0]["profile"] == "global"


# ------------------------------------------------------------------ #
#  Écritures groupées (P3-1)                                          #
# ------------------------------------------------------------------ #
#  Une connexion et deux transactions PAR REQUÊTE DNS : 2 000 requêtes coûtaient 32 s
#  mesurées sur poste de dev, davantage sur la carte SD du Pi. Le cycle écrit désormais
#  ses compteurs en une transaction ; le résultat doit rester le même.

def test_l_ecriture_groupee_donne_les_memes_compteurs(db):
    import domain_classifier as classifier
    db.increment_usage_bulk({("alice", "roblox.com"): 3, ("bruno", "roblox.com"): 1,
                             ("alice", "youtube.com"): 2})
    classifier.record_domains_bulk({"roblox.com": (4, "free"), "youtube.com": (2, "work")})
    with db.get_db() as conn:
        usage = {(r["profile"], r["domain"]): r["queries"]
                 for r in conn.execute("SELECT * FROM daily_usage")}
        lignes = conn.execute("SELECT COUNT(*) FROM dns_timeline").fetchone()[0]
        catalogue = {r["domain"]: (r["hits_total"], r["last_mode"])
                     for r in conn.execute("SELECT * FROM domains")}
    assert usage == {("alice", "roblox.com"): 3, ("bruno", "roblox.com"): 1,
                     ("alice", "youtube.com"): 2}
    assert lignes == 3                 # une marque par (profil, domaine) et par cycle
    assert catalogue == {"roblox.com": (4, "free"), "youtube.com": (2, "work")}
    # Ajouts successifs : les compteurs s'additionnent, comme en unitaire.
    db.increment_usage_bulk({("alice", "roblox.com"): 2})
    classifier.record_domains_bulk({"roblox.com": (1, "work")})
    with db.get_db() as conn:
        assert conn.execute("SELECT queries FROM daily_usage WHERE profile='alice' AND "
                            "domain='roblox.com'").fetchone()[0] == 5
        assert tuple(conn.execute("SELECT hits_total, last_mode FROM domains WHERE "
                                  "domain='roblox.com'").fetchone()) == (5, "work")


def test_le_cycle_regroupe_les_requetes_repetees(db, moniteur):
    ip = moniteur.config["profiles"]["alice"]["devices"][0]["ip"]
    moniteur._record_usage({ip: ["www.roblox.com"] * 25 + ["cdn.roblox.com"] * 5})
    with db.get_db() as conn:
        assert [tuple(r) for r in conn.execute("SELECT profile, domain, queries "
                                               "FROM daily_usage")] == [("alice", "roblox.com", 30)]
        assert conn.execute("SELECT COUNT(*) FROM dns_timeline").fetchone()[0] == 1
        assert conn.execute("SELECT hits_total FROM domains WHERE domain='roblox.com'"
                            ).fetchone()[0] == 30
