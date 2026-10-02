# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Arnaud Ortais
# Dual-licensed: AGPL-3.0 (open source) or Commercial License — see LICENSE and LICENSE-COMMERCIAL.
"""
test_classification.py — la reprise du pipeline de classification.

Le pipeline a trois niveaux : catalogue local à l'insertion, Cloudflare toutes les dix
minutes, modèle une fois par jour. Ce fichier couvre les deux endroits où un domaine en
sortait sans avoir été classé, et où plus rien ne le reprenait jamais : la marque
Cloudflare définitive, et l'aveu d'ignorance enregistré comme un verdict.
"""

import threading
from datetime import datetime, timedelta

import pytest


def _marquer(db, domain, *, category="unknown", by="cloudflare", il_y_a_jours=0,
             hits=1):
    """Pose un domaine en base dans un état donné du pipeline."""
    quand = (datetime.now() - timedelta(days=il_y_a_jours)).isoformat()
    with db.get_db() as conn:
        conn.execute("""
            INSERT INTO domains (domain, category, hits_total, first_seen, last_seen,
                                 last_mode, categorized_by, categorized_at)
            VALUES (?,?,?,?,?,?,?,?)
        """, (domain, category, hits, quand, quand, "permissive", by, quand))


def _file_cloudflare(limit=50):
    import domain_classifier as classifier
    return [d["domain"] for d in classifier.get_uncategorized(limit=limit,
                                                              cloudflare_only=True)]


# ------------------------------------------------------------------ #
#  2.1 — reprise des échecs Cloudflare                                #
# ------------------------------------------------------------------ #

def test_un_domaine_jamais_essaye_est_dans_la_file(db):
    _marquer(db, "nouveau.fr", by=None)
    assert _file_cloudflare() == ["nouveau.fr"]


def test_une_marque_recente_n_est_pas_reprise(db):
    """Sans ce filtre, la passe des dix minutes réinterrogerait sans cesse les mêmes."""
    _marquer(db, "essaye-hier.fr", il_y_a_jours=1)
    assert _file_cloudflare() == []


def test_une_marque_ancienne_repasse_dans_la_file(db):
    """Le défaut d'origine : la marque était définitive, donc un domaine inconnu de
    Cloudflare un jour ne l'était plus jamais réinterrogé, même devenu connu depuis."""
    import domain_classifier as classifier

    _marquer(db, "vieux.fr", il_y_a_jours=classifier.CLOUDFLARE_RETRY_DAYS + 1)
    assert _file_cloudflare() == ["vieux.fr"]


def test_un_domaine_tres_visite_repasse_plus_vite(db):
    """Un domaine qui compte mérite sa seconde chance plus tôt qu'un domaine marginal."""
    import domain_classifier as classifier

    _marquer(db, "marginal.fr", il_y_a_jours=2, hits=3)
    _marquer(db, "tres-visite.fr", il_y_a_jours=2,
             hits=classifier.CLOUDFLARE_RETRY_BUSY_HITS + 50)
    assert _file_cloudflare() == ["tres-visite.fr"]


def test_un_domaine_deja_categorise_ne_repasse_jamais(db):
    """La reprise ne concerne que les domaines restés sans catégorie."""
    _marquer(db, "classe.fr", category="social", il_y_a_jours=365)
    assert _file_cloudflare() == []


def test_la_reprise_rafraichit_la_date_et_ne_boucle_pas(db, monkeypatch):
    """Le piège de la reprise : si la tentative ne met pas la date à jour, le domaine
    ressort de la file à CHAQUE passe pour l'éternité.

    Le résolveur est simulé et ne conclut pas, ce qui est justement le cas qui laisse la
    catégorie à « unknown ».
    """
    import domain_classifier as classifier

    _marquer(db, "vieux.fr", il_y_a_jours=classifier.CLOUDFLARE_RETRY_DAYS + 1)
    monkeypatch.setattr(classifier, "cloudflare_lookup", lambda domain: None)

    assert _file_cloudflare() == ["vieux.fr"]
    classifier.classify_with_cloudflare(limit=10)
    assert _file_cloudflare() == [], "la tentative doit avoir rafraîchi categorized_at"


def test_une_reprise_qui_conclut_ecrit_la_categorie(db, monkeypatch):
    import domain_classifier as classifier

    _marquer(db, "vieux.fr", il_y_a_jours=classifier.CLOUDFLARE_RETRY_DAYS + 1)
    monkeypatch.setattr(classifier, "cloudflare_lookup", lambda domain: "adult")
    assert classifier.classify_with_cloudflare(limit=10) == 1
    connus = {d["domain"]: d["category"] for d in classifier.get_all_domains()}
    assert connus["vieux.fr"] == "adult"


# ------------------------------------------------------------------ #
#  2.2 — « rien à signaler » n'est pas « je ne sais pas »              #
# ------------------------------------------------------------------ #

class _FauxModele:
    """Client OpenAI simulé : renvoie le JSON qu'on lui dicte, sans réseau."""

    def __init__(self, reponses):
        self.reponses = list(reponses)
        self.appels = 0
        self.chat = type("Chat", (), {"completions": self})()

    def create(self, **kwargs):
        self.appels += 1
        contenu = self.reponses.pop(0) if self.reponses else "{}"
        message = type("M", (), {"content": contenu})()
        return type("R", (), {"choices": [type("C", (), {"message": message})()]})()


def _brancher_modele(monkeypatch, classifier, reponses):
    faux = _FauxModele(reponses)
    monkeypatch.setattr(classifier, "classify_with_cloudflare", lambda limit=500: 0)
    monkeypatch.setattr(classifier, "OpenAI", lambda **kwargs: faux)
    return faux


def test_l_aveu_d_ignorance_n_est_pas_enregistre(db, config, monkeypatch):
    """« unknown » laisse la ligne intacte, donc reprise plus tard et remontable au parent.

    L'enregistrer comme un verdict la sortait définitivement de la file : c'est ce que
    faisait le prompt en demandant « other » quand le modèle était incertain.
    """
    import domain_classifier as classifier

    cfg = config.data
    cfg["privacy"] = {"share_with_ai": True}
    cfg["openrouter"] = {"api_key": "cle-de-test", "model": "modele/de-test"}
    _marquer(db, "mystere.fr", by=None)

    faux = _brancher_modele(monkeypatch, classifier, ['{"mystere.fr": "unknown"}'])
    resultat = classifier.classify_with_claude(cfg, batch_size=10)

    assert resultat == {}
    connus = {d["domain"]: d for d in classifier.get_all_domains()}
    assert connus["mystere.fr"]["category"] == "unknown"
    assert not connus["mystere.fr"]["categorized_by"], "la ligne doit rester vierge"
    # Une passe sans progrès arrête la boucle : sans ça, les dix passes reposeraient les
    # mêmes domaines au modèle.
    assert faux.appels == 1


def test_un_site_banal_est_bien_enregistre(db, config, monkeypatch):
    """`other` reste une catégorie légitime : le distinguer de l'ignorance ne doit pas
    l'empêcher de s'écrire."""
    import domain_classifier as classifier

    cfg = config.data
    cfg["privacy"] = {"share_with_ai": True}
    cfg["openrouter"] = {"api_key": "cle-de-test", "model": "modele/de-test"}
    _marquer(db, "meteo.fr", by=None)

    _brancher_modele(monkeypatch, classifier, ['{"meteo.fr": "other"}'])
    assert classifier.classify_with_claude(cfg, batch_size=10) == {"meteo.fr": "other"}
    connus = {d["domain"]: d for d in classifier.get_all_domains()}
    assert connus["meteo.fr"]["category"] == "other"
    assert connus["meteo.fr"]["categorized_by"] == "claude"


def test_une_categorie_inventee_est_refusee(db, config, monkeypatch):
    """Une réponse hors des catégories du produit était écrite telle quelle : le domaine
    devenait catégorisé mais jamais bloqué, et invisible dans les compteurs."""
    import domain_classifier as classifier

    cfg = config.data
    cfg["privacy"] = {"share_with_ai": True}
    cfg["openrouter"] = {"api_key": "cle-de-test", "model": "modele/de-test"}
    _marquer(db, "streaming.fr", by=None)

    _brancher_modele(monkeypatch, classifier, ['{"streaming.fr": "Video Streaming"}'])
    assert classifier.classify_with_claude(cfg, batch_size=10) == {}
    connus = {d["domain"]: d["category"] for d in classifier.get_all_domains()}
    assert connus["streaming.fr"] == "unknown"


# ------------------------------------------------------------------ #
#  2.3 — interrogation immédiate d'un domaine qui bloque              #
# ------------------------------------------------------------------ #

def _provoquer_blocage(m, monkeypatch, domaine="jeu-bloque.fr", hits=30):
    """Fait passer `domaine` par le chemin de blocage réel, avec les seuils du produit.

    KEEPALIVE_MIN_CYCLES impose trois cycles d'observation avant de juger : on envoie donc
    un volume franc, qui saute cette attente comme le ferait un usage réel.
    """
    import domain_classifier as classifier

    monkeypatch.setattr(classifier, "get_active_blacklist",
                        lambda mode, profile=None, profile_key="": [domaine])
    ip = m.config["profiles"]["alice"]["devices"][0]["ip"]
    m._check_blocked_domains("alice", m.config["profiles"]["alice"], {ip: [domaine] * hits})


def test_un_blocage_declenche_une_interrogation_immediate(db, monkeypatch, moniteur):
    """Le domaine dont le parent va demander ce qu'il est doit être identifié tout de
    suite, et non à la passe des dix minutes."""
    import domain_classifier as classifier

    appels = []
    fini = threading.Event()

    def faux_classify_now(domain):
        appels.append((domain, threading.current_thread().name))
        fini.set()
        return "entertainment"

    monkeypatch.setattr(classifier, "classify_now", faux_classify_now)
    m = moniteur
    _provoquer_blocage(m, monkeypatch)

    assert fini.wait(timeout=5), "aucune interrogation déclenchée"
    assert [d for d, _ in appels] == ["jeu-bloque.fr"]


def test_l_interrogation_ne_tourne_pas_dans_le_fil_du_cycle(db, monkeypatch, moniteur):
    """Garde-fou principal : une résolution lente ne doit pas retarder le cycle, qui
    porte le blocage de tous les enfants."""
    import domain_classifier as classifier

    fils = []
    fini = threading.Event()
    fil_du_cycle = threading.current_thread()

    def faux_classify_now(domain):
        fils.append(threading.current_thread())
        fini.set()
        return None

    monkeypatch.setattr(classifier, "classify_now", faux_classify_now)
    m = moniteur
    _provoquer_blocage(m, monkeypatch)

    assert fini.wait(timeout=5)
    assert fils[0] is not fil_du_cycle


def test_une_seule_interrogation_par_domaine_et_par_fenetre(db, monkeypatch, moniteur):
    """Une application qui réessaie en boucle ne doit pas produire une résolution par
    cycle. La fenêtre est d'une heure."""
    import domain_classifier as classifier

    appels = []
    monkeypatch.setattr(classifier, "classify_now",
                        lambda domain: appels.append(domain))
    m = moniteur

    for _ in range(4):
        _provoquer_blocage(m, monkeypatch)
    # Laisser les fils se terminer avant de compter.
    for fil in threading.enumerate():
        if fil.name.startswith("cf-"):
            fil.join(timeout=5)
    assert appels == ["jeu-bloque.fr"]


def test_la_depense_en_appels_au_modele_ne_bouge_pas(db, monkeypatch, moniteur):
    """Troisième garde-fou : l'identification à chaud passe par Cloudflare et JAMAIS par
    le modèle, dont la facture doit rester celle d'un appel groupé par jour."""
    import domain_classifier as classifier

    interdit = lambda *a, **k: pytest.fail("le modèle a été appelé sur un blocage")
    monkeypatch.setattr(classifier, "classify_with_claude", interdit)
    monkeypatch.setattr(classifier, "cloudflare_lookup", lambda domain: "adult")

    m = moniteur
    _provoquer_blocage(m, monkeypatch)
    for fil in threading.enumerate():
        if fil.name.startswith("cf-"):
            fil.join(timeout=5)


def test_classify_now_marque_meme_sans_conclusion(db, monkeypatch):
    """Même à chaud, une tentative qui ne conclut pas se marque : le domaine reste
    réessayable, mais pas à chaque cycle."""
    import domain_classifier as classifier

    _marquer(db, "mystere.fr", by=None)
    monkeypatch.setattr(classifier, "cloudflare_lookup", lambda domain: None)

    assert classifier.classify_now("mystere.fr") is None
    assert _file_cloudflare() == [], "la tentative doit être datée"


def test_classify_now_ignore_l_infrastructure(db, monkeypatch):
    """Rien à identifier sur un CDN : son nom ne dit rien de ce que l'enfant faisait."""
    import domain_classifier as classifier

    appels = []
    monkeypatch.setattr(classifier, "cloudflare_lookup",
                        lambda domain: appels.append(domain))
    assert classifier.classify_now("gstatic.com") is None
    assert appels == []
