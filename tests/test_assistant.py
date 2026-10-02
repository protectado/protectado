# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Arnaud Ortais
# Dual-licensed: AGPL-3.0 (open source) or Commercial License — see LICENSE and LICENSE-COMMERCIAL.
"""
test_assistant.py — l'assistant IA : ses outils, son chat et le rapport du soir.

Aucun appel au fournisseur : le client est un double qui rejoue des réponses écrites
d'avance et enregistre ce qu'on lui envoie.
"""

import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace as NS

import pytest

import modes
import privacy


@pytest.fixture
def agent(db, config, monkeypatch):
    """claude_agent avec une file d'actions et un réveil du moniteur capturés."""
    import claude_agent
    claude_agent.envoyees = []
    monkeypatch.setattr(claude_agent, "_queue_action",
                        lambda action, args: claude_agent.envoyees.append((action, args)))
    monkeypatch.setattr(claude_agent, "notify_monitor", lambda: None)
    claude_agent.outil = lambda nom, **args: claude_agent._execute_parent_tool(
        nom, args, config.data)
    return claude_agent


class _FauxClient:
    def __init__(self, reponses):
        self.reponses, self.appels = list(reponses), []
        self.chat = NS(completions=NS(create=self._create))

    def _create(self, **kw):
        self.appels.append(kw)
        return self.reponses.pop(0)


def _reponse(contenu=None, outils=None):
    return NS(choices=[NS(message=NS(content=contenu, tool_calls=outils),
                          finish_reason="tool_calls" if outils else "stop")])


@pytest.fixture
def ia(agent, config, monkeypatch):
    """Partage IA ouvert, et un faux client à scénariser : ia(reponses) → client."""
    config.data["privacy"] = {"share_with_ai": True}
    config.save()
    monkeypatch.setattr(agent, "_chat_history", [])

    def brancher(reponses):
        client = _FauxClient(reponses)
        monkeypatch.setattr(agent, "_get_client", lambda cfg: (client, "modele"))
        return client
    return brancher


def test_le_client_ia_a_un_delai_et_une_seule_relance(agent, monkeypatch):
    recus: dict = {}
    monkeypatch.setattr(agent, "OpenAI", lambda **kw: recus.update(kw) or object())
    agent._get_client({"openrouter": {"api_key": "sk-test"}})
    assert (recus["timeout"], recus["max_retries"]) == (agent.AI_TIMEOUT_SEC, 1)


# ------------------------------------------------------------------ #
#  Outils                                                             #
# ------------------------------------------------------------------ #

def test_bloquer_maintenant_est_une_derogation_off_bornee(agent, db):
    """Défaut reproduit (P1-2) : une action sans état, défaite au cycle suivant."""
    import scheduler
    reponse = agent.outil("block_device_now", profile="alice", reason="au lit", minutes=45)
    slot = scheduler.get_slot_at("alice", datetime.now())
    assert (slot["mode"], slot["override"]) == (modes.OFF, True) and "45" in reponse
    assert "Durée invalide" in agent.outil("block_device_now", profile="bruno", reason="x",
                                           minutes=agent._MAX_MODE_OVERRIDE_MIN + 1)
    agent.outil("block_device_now", profile="bruno", reason="x")
    (ligne,) = [r for r in db.get_temp_overrides() if r["profile"] == "bruno"]
    restant = datetime.fromisoformat(ligne["expires_at"]) - datetime.now()
    assert abs(restant.total_seconds() / 60 - agent._DEFAULT_BLOCK_MIN) < 1


@pytest.fixture
def deblocage(agent, db, pihole, monkeypatch):
    """jeu.fr et youtube.com bloqués pour toute la maison ; le minuteur n'est pas lancé."""
    import pihole_api
    import threading
    for domaine in ("jeu.fr", "youtube.com"):
        db.set_domain_rule("", domaine, modes.FREE, allow=False)
    monkeypatch.setattr(pihole_api, "PiHoleAPI", lambda host, password: pihole)
    minuteurs: list = []

    class _Minuteur(threading.Timer):
        def start(self):
            minuteurs.append(self)
    monkeypatch.setattr(agent.threading, "Timer", _Minuteur)
    pihole.returns.update(get_group_id=7, remove_domain_from_group=True)
    return lambda domaine: (agent.outil("allow_domain_temporarily", domain=domaine,
                                        profile="alice", minutes=30), minuteurs)


def test_debloquer_un_sous_domaine_debloque_sa_racine(deblocage, db):
    reponse, minuteurs = deblocage("www.youtube.com")
    assert [r["domain"] for r in db.get_temp_domain_unblocks()] == ["youtube.com"]
    assert "débloqué" in reponse and all(t.daemon for t in minuteurs)


@pytest.mark.parametrize("domaine,categorie,attendu", [
    ("libre.fr", None, "pas bloqué"),
    ("jeu.fr", "adult", "ne peut pas être débloqué"),
])
def test_un_deblocage_sans_effet_ne_se_dit_pas_reussi(deblocage, db, domaine, categorie,
                                                      attendu):
    import domain_classifier
    if categorie:
        domain_classifier.update_domain(domaine, categorie)
    reponse, _ = deblocage(domaine)
    assert attendu in reponse and db.get_temp_domain_unblocks() == []


def test_sans_confirmation_de_pi_hole_la_reponse_le_dit(deblocage, pihole, db):
    pihole.returns.update(get_group_id=None)
    reponse, _ = deblocage("jeu.fr")
    assert "pas encore appliqué" in reponse
    assert [r["domain"] for r in db.get_temp_domain_unblocks()] == ["jeu.fr"]


@pytest.mark.parametrize("outil,args", [
    ("override_day", {"profile": "alice", "date": "31/12", "mode": modes.OFF}),
    ("query_history", {"profile": "alice", "date": "hier"}),
])
def test_une_date_invalide_est_refusee(agent, db, outil, args):
    assert "Date invalide" in agent.outil(outil, **args)
    assert db.get_schedule_overrides() == []


# ------------------------------------------------------------------ #
#  Le chat enchaîne ses outils                                        #
# ------------------------------------------------------------------ #

def _appel(id_, nom, arguments):
    return NS(id=id_, type="function", function=NS(name=nom, arguments=arguments))


def _derog(profil, jour):
    return _appel(f"c-{profil}-{jour}", "override_day",
                  json.dumps({"profile": profil, "date": f"2026-10-0{jour}", "mode": modes.OFF}))


def test_deux_tours_d_outils_aboutissent_et_portent_les_outils(agent, ia, db):
    """Défaut reproduit (P1-7) : le second appel partait sans outils."""
    client = ia([_reponse(outils=[_derog("alice", 1)]), _reponse(outils=[_derog("bruno", 2)]),
                 _reponse(contenu="C'est fait.")])
    assert agent.chat("question") == "C'est fait."
    assert sorted(r["profile"] for r in db.get_schedule_overrides()) == ["alice", "bruno"]
    assert all(a.get("tools") for a in client.appels)


def test_la_boucle_d_outils_est_bornee(agent, ia):
    n = agent._MAX_TOOL_ROUNDS
    client = ia([_reponse(outils=[_derog("alice", i + 1)]) for i in range(n)]
                + [_reponse(contenu="Terminé.")])
    assert agent.chat("question") == "Terminé."
    assert client.appels[-1].get("tool_choice") == "none"
    ia([_reponse(outils=[_derog("alice", i + 1)]) for i in range(n + 1)])
    assert "trop d'actions" in agent.chat("question")


def test_une_erreur_d_outil_revient_au_modele(agent, ia, monkeypatch):
    client = ia([_reponse(outils=[_appel("c1", "override_day", "{pas du json")]),
                 _reponse(contenu="Je n'ai pas pu.")])
    assert agent.chat("question") == "Je n'ai pas pu."
    resultat = [m for m in client.appels[-1]["messages"] if m.get("role") == "tool"]
    assert "Erreur" in resultat[0]["content"]


# ------------------------------------------------------------------ #
#  Le rapport du soir                                                 #
# ------------------------------------------------------------------ #

def _evenement(db, quand, type_, cle):
    with db.get_db() as conn:
        conn.execute("INSERT INTO events (timestamp, profile, type, domain, message, "
                     "message_key, params) VALUES (?,?,?,?,?,?,?)",
                     (quand.isoformat(), "alice", type_, "", "", cle, ""))


def test_le_rapport_compte_les_evenements_du_jour(agent, ia, db, config):
    """Défaut reproduit (P1-8) : les 50 derniers événements, hier compris, et les
    tentatives bloquées comptées comme alertes."""
    config.data["profiles"]["alice"]["privacy_level"] = privacy.DETAILED
    config.save()
    client = ia([_reponse(contenu="rapport"), _reponse(contenu="résumé")])
    maintenant = datetime.now()
    for quand in (maintenant - timedelta(days=1), maintenant):
        _evenement(db, quand, "warning", "event.dns_bypass_suspected")
        _evenement(db, quand, "block_device", "event.manual_block")
        _evenement(db, quand, "warning", "event.blocked_attempt")
    agent.daily_report()
    texte = client.appels[0]["messages"][-1]["content"]
    enfant = json.loads(texte[texte.index("{"):texte.rindex("}") + 1])["Enfant 1"]
    assert (enfant["nb_alertes"], enfant["nb_blocages"]) == (1, 2)


@pytest.mark.parametrize("niveau,horaires", [(privacy.DETAILED, True), (privacy.SUMMARY, False)])
def test_le_rapport_connait_le_volume_et_les_tunnels(agent, ia, db, config, niveau, horaires):
    """Constaté : une journée passée en VPN décrite comme « activité principalement
    système », faute d'autre mesure que les requêtes DNS. Les horaires du tunnel ne
    partent qu'au niveau détaillé."""
    config.data["profiles"]["alice"]["privacy_level"] = niveau
    config.save()
    client = ia([_reponse(contenu="rapport"), _reponse(contenu="résumé")])
    jour = datetime.now().date().isoformat()
    db.add_daily_traffic({(jour, "alice"): 1_900_000_000})
    sid = db.open_vpn_session("alice", "192.168.50.85", "165.85.212.43",
                              f"{jour}T14:09:00", f"{jour}T15:32:00", 1_300_000_000)
    db.close_vpn_session(sid, f"{jour}T15:32:00")
    agent.daily_report()
    texte = client.appels[0]["messages"][-1]["content"]
    enfant = json.loads(texte[texte.index("{"):texte.rindex("}") + 1])["Enfant 1"]
    assert enfant["volume_internet_jour"] == "1,8 Go"
    (tunnel,) = enfant["tunnels_probables"]
    assert tunnel["volume"] == "1,2 Go"
    assert ("debut" in tunnel) is horaires
    assert "LECTURE DU VOLUME" in texte


@pytest.mark.parametrize("rapport,jour,revues,code", [
    ("Erreur rapport : délai", date(2026, 6, 1), ["hebdo", "mensuel"], 1),
    (RuntimeError("réseau"), date(2026, 6, 1), ["hebdo", "mensuel"], 1),
    ("Rapport", date(2026, 6, 3), [], 0),
])
def test_un_rapport_en_echec_n_empeche_pas_les_revues(agent, monkeypatch, rapport, jour,
                                                      revues, code):
    """Le 1er juin 2026 est un lundi et un premier du mois."""
    import daily_report
    import domain_classifier
    faites: list = []

    def produire():
        if isinstance(rapport, Exception):
            raise rapport
        return rapport
    monkeypatch.setattr(domain_classifier, "classify_with_claude", lambda cfg: {})
    monkeypatch.setattr(agent, "daily_report", produire)
    monkeypatch.setattr(agent, "weekly_report", lambda: faites.append("hebdo") or "ok")
    monkeypatch.setattr(agent, "monthly_report", lambda: faites.append("mensuel") or "ok")
    assert daily_report.main(today=jour) == code
    assert faites == revues
