# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Arnaud Ortais
# Dual-licensed: AGPL-3.0 (open source) or Commercial License — see LICENSE and LICENSE-COMMERCIAL.
"""
test_runner.py — le service privilégié : la file qu'il lit, et les règles qu'il pose.

Aucune commande n'est exécutée (fixture `runner`). Pour les règles, l'ORDRE compte autant
que le contenu : une autorisation d'appareil placée avant le rejet des contournements
les laisserait passer.
"""

import fnmatch
import json
import os
from types import SimpleNamespace as NS

import pytest

from conftest import regles

KIDS = "192.168.50.0/24"
IP_A, IP_B = "192.168.50.52", "192.168.50.53"


# ------------------------------------------------------------------ #
#  La file d'actions                                                  #
# ------------------------------------------------------------------ #

@pytest.fixture
def file_dir(tmp_path, monkeypatch):
    import action_queue
    d = tmp_path / "fw-queue"
    monkeypatch.setattr(action_queue, "ACTION_QUEUE_DIR", str(d))
    return d


def test_une_action_n_apparait_sous_son_nom_qu_une_fois_complete(file_dir, monkeypatch):
    """Défaut reproduit : écrite directement sous son nom, l'action était lue vide par
    le runner, mise de côté en .error, et perdue."""
    import action_queue

    vrai = os.replace

    def espion(src, dst):
        assert not fnmatch.fnmatch(os.path.basename(src), "action-*.json")
        assert json.load(open(src))["action"] == "apply_forward"
        return vrai(src, dst)

    monkeypatch.setattr(action_queue.os, "replace", espion)
    chemin = action_queue.enqueue("apply_forward", {"ips": [IP_A]})
    assert os.listdir(file_dir) == [os.path.basename(chemin)]


def test_des_depots_rapproches_restent_distincts_et_ordonnes(file_dir):
    import action_queue
    for i in range(200):
        action_queue.enqueue("apply_wifi_keys", {"n": i})
    lus = [json.load(open(file_dir / f))["args"]["n"] for f in sorted(os.listdir(file_dir))]
    assert lus == list(range(200))


def test_le_repertoire_cree_est_partage_avec_le_groupe(file_dir):
    import action_queue
    action_queue.enqueue("apply_wifi_keys", {})
    assert os.stat(file_dir).st_mode & 0o777 == 0o770


def test_une_file_inaccessible_leve_une_oserror(tmp_path, monkeypatch):
    import action_queue
    (tmp_path / "fichier").write_text("")
    monkeypatch.setattr(action_queue, "ACTION_QUEUE_DIR", str(tmp_path / "fichier" / "file"))
    with pytest.raises(OSError):
        action_queue.enqueue("apply_wifi_keys", {})


def test_le_runner_ignore_un_fichier_en_cours_d_ecriture(file_dir, monkeypatch):
    import action_queue
    import action_runner

    monkeypatch.setattr(action_runner, "ACTION_QUEUE_DIR", str(file_dir))
    os.makedirs(file_dir, exist_ok=True)
    (file_dir / f"{action_queue.TMP_PREFIX}abc.part").write_text('{"action": "apply_')
    chemin = action_queue.enqueue("apply_wifi_keys", {})
    assert action_runner.pending_action_files() == [chemin]


def test_les_quatre_ecrivains_passent_par_enqueue(monkeypatch, db, config):
    """Un seul chemin d'écriture : une correction de la file ne doit en oublier aucun."""
    import access_control
    import action_queue
    import claude_agent
    import dashboard
    import monitor

    appels: list = []
    monkeypatch.setattr(action_queue, "enqueue", lambda action, args: appels.append(action) or "x")
    monitor.ProtectadoMonitor._queue_action(None, "a1", {})
    claude_agent._queue_action("a2", {})
    dashboard._queue("a3", {})
    access_control._queue_forward_action([IP_A], blocked=True, profile="alice", reason="t")
    assert appels == ["a1", "a2", "a3", "apply_forward"]


# ------------------------------------------------------------------ #
#  Passerelle : contournements rejetés avant toute autorisation       #
# ------------------------------------------------------------------ #

DOT = [("-p", "tcp", "--dport", "853", "-m", "comment", "--comment", "pt:dot",
        "-j", "REJECT", "--reject-with", "tcp-reset"),
       ("-p", "udp", "--dport", "853", "-m", "comment", "--comment", "pt:dot",
        "-j", "REJECT")]
DOH = [("-p", "tcp", "--dport", "443", "-m", "set", "--match-set", "protectado_doh",
        "dst", "-m", "comment", "--comment", "pt:doh", "-j", "REJECT",
        "--reject-with", "tcp-reset"),
       ("-p", "udp", "--dport", "443", "-m", "set", "--match-set", "protectado_doh",
        "dst", "-m", "comment", "--comment", "pt:doh", "-j", "REJECT")]


def test_la_base_passerelle_dans_l_ordre(runner):
    """Rejet des contournements, puis refus de fond ; rien sur OUTPUT, dont le boîtier a
    besoin pour ses propres résolutions chiffrées."""
    runner._ensure_gateway_base()
    assert regles(runner, runner.BYPASS_CHAIN) == DOT
    assert regles(runner, runner.FWD_CHAIN) == [("-s", KIDS, "-j", runner.BYPASS_CHAIN),
                                                ("-s", KIDS, "-j", "DROP")]
    assert not any("OUTPUT" in e for e in runner.journal)


def test_une_autorisation_s_insere_apres_le_rejet_des_contournements(runner):
    runner._apply_device_forward([IP_A], blocked=False)
    assert ("ipt", "filter", "-I", runner.FWD_CHAIN, "2", "-s", IP_A, "-j", "ACCEPT") \
        in runner.journal


def test_le_rearmement_repose_les_memes_regles(runner, monkeypatch):
    runner._ensure_gateway_base()
    premier = regles(runner, runner.BYPASS_CHAIN), regles(runner, runner.FWD_CHAIN)
    runner.journal.clear()
    monkeypatch.setattr(runner, "_gateway_active", False)
    monkeypatch.setattr(runner, "_gateway_hardware_ok", lambda: (True, ""))
    assert runner._rearm_gateway()
    assert (regles(runner, runner.BYPASS_CHAIN), regles(runner, runner.FWD_CHAIN)) == premier


ETABLIES = "-i wlan_up -o wlan_ap -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT"


@pytest.mark.parametrize("forward,corrige", [
    (["-j PROTECTADO_FWD", ETABLIES], False),
    ([ETABLIES, "-j PROTECTADO_FWD"], True),
])
def test_le_saut_vers_la_chaine_reste_en_tete_de_forward(runner, forward, corrige):
    """Derrière l'ACCEPT des connexions établies, un appareil coupé garderait ses flux."""
    runner.sorties[(runner.IPTABLES, "-S", "FORWARD")] = "".join(
        f"-A FORWARD {r}\n" for r in forward)
    runner._ensure_fwd_jump_first()
    remis = ("ipt", "filter", "-I", "FORWARD", "1", "-j", runner.FWD_CHAIN) in runner.journal
    assert remis is corrige


def test_le_saut_est_verifie_a_chaque_reverification(runner, monkeypatch):
    vus: list = []
    monkeypatch.setattr(runner, "_gateway_hardware_ok", lambda: (True, ""))
    monkeypatch.setattr(runner, "_ensure_fwd_jump_first", lambda: vus.append(1))
    assert runner._rearm_gateway() and vus == [1]


def test_aucun_transfert_ipv6_par_l_ap(runner):
    runner.outils.add("ip6tables")
    runner._ensure_gateway_base()
    ip6 = [e[1:] for e in runner.journal if e[0] == "ip6t"]
    assert ("-A", runner.FWD6_CHAIN, "-i", runner.AP_IFACE, "-j", "DROP") in ip6
    assert ("-A", runner.FWD6_CHAIN, "-o", runner.AP_IFACE, "-j", "DROP") in ip6
    assert ip6[-1] == ("-I", "FORWARD", "1", "-j", runner.FWD6_CHAIN)


def test_sans_ip6tables_le_reste_est_pose(runner):
    runner._ensure_gateway_base()
    assert not any(e[0] == "ip6t" for e in runner.journal)
    assert regles(runner, runner.FWD_CHAIN)


# ------------------------------------------------------------------ #
#  DNS-over-HTTPS : l'ipset des résolveurs connus                     #
# ------------------------------------------------------------------ #

@pytest.fixture
def catalogue_doh(tmp_path, monkeypatch):
    import doh_catalog
    chemin = tmp_path / "doh.json"
    chemin.write_text(json.dumps({"domains": ["dns.google"], "ipv4": ["8.8.8.8"]}))
    monkeypatch.setattr(doh_catalog, "PATH", str(chemin))
    return chemin


def test_le_set_est_charge_avant_les_regles_qui_le_citent(runner, catalogue_doh):
    runner.outils.add("ipset")
    runner._ensure_gateway_base()
    assert regles(runner, runner.BYPASS_CHAIN) == DOT + DOH
    chargement = runner.journal.index(("run", "ipset", "restore"))
    regle = runner.journal.index(("ipt", "filter", "-A", runner.BYPASS_CHAIN) + DOH[0])
    assert chargement < regle


def test_sans_ipset_l_effecteur_continue_et_se_dit_degrade(runner, catalogue_doh):
    runner._ensure_gateway_base()
    assert regles(runner, runner.BYPASS_CHAIN) == DOT
    assert runner._active_status()[0] == "degraded" and "ipset" in runner._doh_problem


def test_le_set_n_est_recharge_que_si_le_catalogue_change(runner, catalogue_doh):
    runner.outils.add("ipset")
    runner._ensure_gateway_base()
    runner.journal.clear()
    runner._maybe_reload_doh()
    assert runner.journal == []
    st = os.stat(catalogue_doh)
    os.utime(catalogue_doh, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    runner._maybe_reload_doh()
    assert ("run", "ipset", "restore") in runner.journal


# ------------------------------------------------------------------ #
#  Compteurs de contournement, par appareil                           #
# ------------------------------------------------------------------ #

def test_chaque_appareil_identifie_a_ses_regles_avant_le_filet(runner):
    generiques = runner._bypass_rules(())
    assert runner._bypass_rules((IP_A, IP_B)) == (
        [("-s", ip) + r for ip in (IP_A, IP_B) for r in generiques] + generiques)


def test_la_chaine_est_reconstruite_quand_les_appareils_changent(runner, monkeypatch):
    vues: list = []
    monkeypatch.setattr(runner, "_rebuild_bypass", lambda: vues.append(runner._bypass_ips))
    runner._sync_bypass_ips({IP_B, IP_A})
    runner._sync_bypass_ips({IP_A, IP_B})
    assert vues == [(IP_A, IP_B)]


COMPTEURS = f"""Chain PROTECTADO_BYPASS (1 references)
    pkts      bytes target     prot opt in     out     source               destination
       3      180 REJECT     6    --  *      *       {IP_A}        0.0.0.0/0            tcp dpt:853 /* pt:dot */ reject-with tcp-reset
       2      120 REJECT     17   --  *      *       {IP_A}        0.0.0.0/0            udp dpt:853 /* pt:dot */ reject-with icmp-port-unreachable
       7      420 REJECT     6    --  *      *       {IP_B}        0.0.0.0/0            tcp dpt:443 match-set protectado_doh dst /* pt:doh */ reject-with tcp-reset
       9      540 REJECT     6    --  *      *       0.0.0.0/0            0.0.0.0/0            tcp dpt:853 /* pt:dot */ reject-with tcp-reset
"""


def test_les_compteurs_publies_par_appareil_et_par_type(runner, monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "_bypass_generation", "g1")
    runner.sorties[(runner.IPTABLES, "-L", runner.BYPASS_CHAIN)] = COMPTEURS
    runner._publish_bypass_counters()
    publie = json.loads((tmp_path / "bypass_counters.json").read_text())
    assert publie["generation"] == "g1"
    assert publie["counters"] == {IP_A: {"dot": 5}, IP_B: {"doh": 7}}


# ------------------------------------------------------------------ #
#  Pi-hole ne tient pas le 443                                        #
# ------------------------------------------------------------------ #
#  Défaut observé : un navigateur qui tente le HTTPS tombait sur l'interface de Pi-hole
#  au lieu du tableau de bord. La correction tourne à chaque démarrage, en root, et
#  redémarre le résolveur de la maison : elle ne doit agir qu'à bon escient.

ECRITURE = ("pihole-FTL", "--config", "webserver.port", "81o,[::]:81o")


@pytest.fixture
def ftl(runner):
    runner.outils.add("pihole-FTL")

    def lire(valeur):
        runner.sorties[("pihole-FTL", "--config", "webserver.port")] = valeur
    runner.lire = lire
    lire("81o,443os,[::]:81o,[::]:443os")
    return runner


def _ecritures(r):
    return [e[1:] for e in r.journal if e[:3] == ("run", "pihole-FTL", "--config") and len(e) > 4]


def _redemarrages(r):
    return [e[1:] for e in r.journal if e[:2] == ("run", "systemctl")]


def test_le_443_est_retire_et_le_resolveur_relance(ftl):
    ftl._ensure_pihole_ports()
    assert _ecritures(ftl) == [ECRITURE]
    assert _redemarrages(ftl) == [("systemctl", "restart", "pihole-FTL")]


def test_une_ecriture_echouee_ne_redemarre_pas_le_resolveur(ftl):
    """Couper le DNS de la maison pour un réglage qui n'a pas été écrit serait le pire."""
    ftl.codes[ECRITURE] = 1
    ftl._ensure_pihole_ports()
    assert _redemarrages(ftl) == []


@pytest.mark.parametrize("lecture", ["81o,[::]:81o", 'webserver.port = "81o,[::]:81o"',
                                     "'81o,[::]:81o'", " 81o,[::]:81o \n", ""])
def test_rien_n_est_touche_quand_il_n_y_a_rien_a_corriger(ftl, lecture):
    """Déjà bon, format d'affichage inattendu, ou lecture vide : ni écriture ni coupure."""
    ftl.lire(lecture)
    ftl._ensure_pihole_ports()
    assert _ecritures(ftl) == [] and _redemarrages(ftl) == []


def test_sans_pihole_ftl_rien_n_est_lance(runner):
    runner._ensure_pihole_ports()
    assert runner.journal == []


def test_l_installation_ne_demande_plus_le_443():
    """Un boîtier neuf ne doit pas naître avec le défaut."""
    from pathlib import Path
    script = (Path(__file__).resolve().parent.parent / "bootstrap" / "bootstrap.sh").read_text()
    (ligne,) = [l for l in script.splitlines() if "webserver.port" in l and "pihole-FTL" in l]
    assert "443" not in ligne and "81o" in ligne


# ------------------------------------------------------------------ #
#  Relevés de flux pour la détection de tunnel (PG-5)                 #
# ------------------------------------------------------------------ #

def _ct(src, dst, sport, octets, proto="udp", dport=51820):
    """Ligne « conntrack -L -o extended » : aller puis retour, octets répartis."""
    return (f"ipv4     2 {proto}      17 29 src={src} dst={dst} sport={sport} dport={dport} "
            f"packets=10 bytes={octets // 2} src={dst} dst=192.168.0.61 sport={dport} "
            f"dport={sport} packets=10 bytes={octets - octets // 2} mark=0 use=1\n")


def test_le_premier_releve_apres_demarrage_sert_de_reference(runner, monkeypatch, tmp_path):
    """Constaté sur le boîtier : 217 Mo d'un coup au premier relevé, soit tout ce que les
    connexions déjà ouvertes avaient transporté avant le démarrage du runner."""
    monkeypatch.setattr(runner, "_flow_prev", {})
    monkeypatch.setattr(runner, "_flow_samples", {})
    monkeypatch.setattr(runner, "_flow_started", False)
    runner.outils.add("conntrack")
    cle = ("conntrack", "-L", "-o")
    runner.sorties[cle] = _ct(IP_A, "203.0.113.9", 40000, 217_000_000)
    runner._sample_flows(now=1000.0)
    assert not (tmp_path / "flow_stats.json").exists()
    runner.sorties[cle] = _ct(IP_A, "203.0.113.9", 40000, 217_600_000)
    runner._sample_flows(now=1060.0)
    (releve,) = json.loads((tmp_path / "flow_stats.json").read_text())["samples"][IP_A]
    assert releve["bytes"] == 600_000


def test_un_releve_compte_ce_qui_a_circule_depuis_le_precedent(runner, monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "_flow_prev", {})
    monkeypatch.setattr(runner, "_flow_samples", {})
    monkeypatch.setattr(runner, "_flow_started", True)
    runner.outils.add("conntrack")
    cle = ("conntrack", "-L", "-o")
    runner.sorties[cle] = (_ct(IP_A, "203.0.113.9", 40000, 300_000)
                           + _ct(IP_A, "8.8.4.4", 40001, 20_000, "tcp", 443)
                           + _ct(IP_A, "192.168.50.1", 40002, 5_000, "udp", 53))
    runner._sample_flows(now=1000.0)
    runner.sorties[cle] = (_ct(IP_A, "203.0.113.9", 40000, 900_000)
                           + _ct(IP_A, "8.8.4.4", 40001, 20_000, "tcp", 443))
    runner._sample_flows(now=1300.0)
    (premier, second) = json.loads((tmp_path / "flow_stats.json").read_text())["samples"][IP_A]
    # Le boîtier lui-même (DNS vers 192.168.50.1) n'est pas une destination de sortie.
    assert premier == {"t": 1000.0, "bytes": 320_000, "top_dst": "203.0.113.9",
                       "top_share": 0.94}
    # Second relevé : seulement ce qui a circulé entre les deux.
    assert second == {"t": 1300.0, "bytes": 600_000, "top_dst": "203.0.113.9",
                      "top_share": 1.0}


def test_la_fenetre_ne_garde_que_sa_duree(runner, monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "_flow_prev", {})
    monkeypatch.setattr(runner, "_flow_samples", {})
    runner.outils.add("conntrack")
    for i in range(20):
        runner.sorties[("conntrack", "-L", "-o")] = _ct(IP_A, "203.0.113.9", 40000,
                                                         300_000 * (i + 1))
        runner._sample_flows(now=1000.0 + runner.FLOW_SAMPLE_SEC * i)
    echantillons = json.loads((tmp_path / "flow_stats.json").read_text())["samples"][IP_A]
    assert echantillons[-1]["t"] - echantillons[0]["t"] <= runner.FLOW_WINDOW_SEC


def test_sans_conntrack_aucun_releve(runner, tmp_path):
    runner._sample_flows(now=1000.0)
    assert not (tmp_path / "flow_stats.json").exists()
    assert not any(e[:2] == ("run", "conntrack") for e in runner.journal)


def test_ipset_installe_apres_coup_est_pris_en_compte(runner, catalogue_doh):
    """Signalé sur boîtier : l'alerte « degraded » restait après « apt install ipset »,
    tant que le runner n'était pas redémarré."""
    runner._ensure_gateway_base()
    assert runner._active_status()[0] == "degraded"
    runner.outils.add("ipset")
    runner._maybe_reload_doh()
    assert runner._active_status()[0] == "ok"
    assert regles(runner, runner.BYPASS_CHAIN)[-2:] == DOH
    assert runner.journal[-1][:2] == ("statut", "ok")


def test_ipset_toujours_absent_ne_noie_pas_le_journal(runner, catalogue_doh, caplog):
    with caplog.at_level("CRITICAL"):
        runner._ensure_gateway_base()
        for _ in range(5):
            runner._maybe_reload_doh()
    assert sum("ipset absent" in m for m in caplog.messages) == 1


# ------------------------------------------------------------------ #
#  Le code appartient à root ; l'updater se resynchronise             #
# ------------------------------------------------------------------ #
#  Le runner (root) exécute le code de /opt/protectado : s'il appartient à l'utilisateur
#  du service, tout processus de ce compte hors sandbox peut devenir root en le
#  modifiant. Ces tests changent des propriétaires : ils ne tournent qu'en root.

racine_requise = pytest.mark.skipif(os.geteuid() != 0, reason="chown exige root")
AUTRE = 65534          # nobody : l'utilisateur du service dans ces tests


@pytest.fixture
def installation(tmp_path):
    """Arborescence qui imite /opt/protectado, entièrement à l'utilisateur du service."""
    base = tmp_path / "protectado"
    for chemin, contenu in (("action_runner.py", "x"), (".venv/bin/python3.12", "bin"),
                            ("bootstrap/protectado-update.sh", "SVC_USER=\"__USER__\"\n"),
                            ("data/config.json", "{}"), ("data/protectado.db", "")):
        f = base / chemin
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(contenu)
    exterieur = tmp_path / "python-systeme"
    exterieur.write_text("")
    os.symlink(exterieur, base / ".venv" / "bin" / "python")
    for dossier, sous, fichiers in os.walk(base):
        for nom in sous + fichiers:
            p = os.path.join(dossier, nom)
            os.lchown(p, AUTRE, AUTRE)
            if not os.path.islink(p):
                os.chmod(p, 0o775 if os.path.isdir(p) else 0o664)
    os.chown(base, AUTRE, AUTRE)
    os.chown(exterieur, AUTRE, AUTRE)
    return base, exterieur


@racine_requise
def test_le_code_passe_a_root_et_les_donnees_restent_au_service(installation):
    import system_maintenance as sm
    base, exterieur = installation
    assert sm.secure_code(str(base), AUTRE) is True
    for chemin in ("", "action_runner.py", ".venv", ".venv/bin/python3.12", "bootstrap"):
        st = os.lstat(base / chemin)
        assert (st.st_uid, st.st_mode & 0o022) == (0, 0), chemin
    for chemin in ("data", "data/config.json", "data/protectado.db"):
        assert os.lstat(base / chemin).st_uid == AUTRE, chemin
    # Un lien symbolique n'est jamais suivi : sa cible, hors du code, ne change pas.
    assert os.stat(exterieur).st_uid == AUTRE
    assert sm.secure_code(str(base), AUTRE) is False          # rien à refaire


@racine_requise
def test_un_fichier_rendu_a_l_utilisateur_est_repris(installation):
    import system_maintenance as sm
    base, _ = installation
    sm.secure_code(str(base), AUTRE)
    os.chown(base / "action_runner.py", AUTRE, AUTRE)
    assert sm.secure_code(str(base), AUTRE) is True
    assert os.lstat(base / "action_runner.py").st_uid == 0


def test_l_updater_installe_suit_le_modele_du_depot(installation, tmp_path):
    import system_maintenance as sm
    base, _ = installation
    cible = tmp_path / "sbin" / "protectado-update"
    modele = str(base / "bootstrap" / "protectado-update.sh")
    assert sm.sync_updater("famille", str(cible), modele) is True
    assert cible.read_text() == 'SVC_USER="famille"\n'
    assert cible.stat().st_mode & 0o777 == 0o755
    assert sm.sync_updater("famille", str(cible), modele) is False
    cible.write_text("ancienne version")
    assert sm.sync_updater("famille", str(cible), modele) is True


def test_l_updater_ne_fait_plus_git_ni_pip_sous_l_utilisateur():
    """Le code étant à root, git et pip sous l'utilisateur ne pourraient plus écrire."""
    from pathlib import Path
    script = (Path(__file__).resolve().parent.parent / "bootstrap" / "protectado-update.sh").read_text()
    lignes = [l.strip() for l in script.splitlines() if not l.strip().startswith("#")]
    assert not [l for l in lignes if "as_user git" in l or "as_user .venv/bin/pip" in l]


# ------------------------------------------------------------------ #
#  Migrations système des boîtiers livrés                             #
# ------------------------------------------------------------------ #

@pytest.fixture
def migrations(tmp_path):
    """Dossier de migrations et registre isolés. poser(nom, corps) écrit un script."""
    import system_maintenance as sm
    dossier = tmp_path / "migrations"
    dossier.mkdir()
    os.chmod(tmp_path, 0o755)
    trace = tmp_path / "trace"

    def poser(nom, corps="exit 0"):
        f = dossier / nom
        f.write_text(f"#!/usr/bin/env bash\necho {nom} >> {trace}\n{corps}\n")
        os.chmod(f, 0o755)

    def lancer():
        return sm.run_pending(str(dossier), str(tmp_path / "migrations.done"),
                              str(tmp_path / "maintenance.json"), racine=str(tmp_path))

    def statut():
        return json.loads((tmp_path / "maintenance.json").read_text())

    def jouees():
        return trace.read_text().split() if trace.exists() else []
    return NS(poser=poser, lancer=lancer, statut=statut, jouees=jouees, dossier=dossier,
              fait=tmp_path / "migrations.done")



def test_les_migrations_passent_dans_l_ordre_et_une_seule_fois(migrations):
    migrations.poser("0002-deux.sh")
    migrations.poser("0001-un.sh")
    migrations.poser("notes.txt")                         # ignoré : pas une migration
    assert migrations.lancer() is True
    assert migrations.lancer() is True
    assert migrations.jouees() == ["0001-un.sh", "0002-deux.sh"]
    assert migrations.fait.read_text().split() == ["0001-un.sh", "0002-deux.sh"]
    assert migrations.statut()["state"] == "ok"


def test_un_echec_arrete_la_serie_se_dit_et_se_reprend(migrations):
    migrations.poser("0001-un.sh", "echo 'apt : réseau injoignable' >&2; exit 1")
    migrations.poser("0002-deux.sh")
    assert migrations.lancer() is False
    statut = migrations.statut()
    assert statut["state"] == "error" and "0001-un.sh" in statut["detail"]
    assert "réseau injoignable" in statut["detail"]
    assert statut["pending"] == ["0001-un.sh", "0002-deux.sh"]
    assert migrations.jouees() == ["0001-un.sh"]           # la suivante a attendu
    migrations.poser("0001-un.sh")                          # la cause a disparu
    assert migrations.lancer() is True
    assert migrations.jouees() == ["0001-un.sh", "0001-un.sh", "0002-deux.sh"]


@racine_requise
@pytest.mark.parametrize("abimer", ["groupe", "proprietaire"])
def test_une_migration_modifiable_hors_root_est_refusee(migrations, abimer):
    """Le runner exécute ces scripts en root : un script que l'utilisateur du service
    pourrait modifier serait une porte vers root."""
    migrations.poser("0001-un.sh")
    f = migrations.dossier / "0001-un.sh"
    if abimer == "groupe":
        os.chmod(f, 0o775)
    else:
        os.chown(f, AUTRE, AUTRE)
    assert migrations.lancer() is False
    assert migrations.jouees() == []
    assert "non sûre" in migrations.statut()["detail"]


def test_les_migrations_du_depot_respectent_la_procedure():
    """Cf. bootstrap/migrations/README.md : nom numéroté, effet, équivalent bootstrap et
    retour arrière écrits en tête, exécutable, syntaxe bash valide, numéros uniques."""
    import re
    import subprocess as sp
    import system_maintenance as sm
    from pathlib import Path
    dossier = Path(sm.MIGRATIONS_DIR)
    noms = sm.list_migrations(str(dossier))
    assert noms, "aucune migration"
    numeros = [n[:4] for n in noms]
    assert len(set(numeros)) == len(numeros)
    for nom in noms:
        f = dossier / nom
        tete = f.read_text()[:2000]
        for champ in ("# Effet :", "# Équivalent bootstrap :", "# Retour arrière :"):
            assert champ in tete, f"{nom} : {champ} manquant"
        assert os.access(f, os.X_OK), f"{nom} n'est pas exécutable"
        assert sp.run(["bash", "-n", str(f)]).returncode == 0, nom
    assert all(re.match(sm.MIGRATION_RE, n) for n in noms)


def test_seule_une_installation_neuve_marque_les_migrations_faites():
    """Défaut constaté : la mise à jour par bootstrap.sh, qui saute l'installation des
    paquets, marquait la migration des paquets comme faite sans l'avoir appliquée."""
    import re
    from pathlib import Path
    script = (Path(__file__).resolve().parent.parent / "bootstrap" / "bootstrap.sh").read_text()
    mise_a_jour = re.search(r"\nrun_update\(\) \{\n(.*?)\n\}\n", script, re.S).group(1)
    appels = [l for l in mise_a_jour.splitlines()
              if "mark_migrations_done" in l and not l.strip().startswith("#")]
    assert appels == []
    assert "mark_migrations_done" in script.split("\nrun_update() {")[0] + \
        script[script.index("step3_protectado() {"):]


# ------------------------------------------------------------------ #
#  Unités systemd et profil nono : un seul installeur partagé (P2-2)  #
# ------------------------------------------------------------------ #

def test_l_installeur_d_unites_pose_tout_avec_les_bons_chemins(tmp_path):
    """L'updater ne réinstallait ni l'unité de l'agent ni le profil nono : une correction
    de l'un ou de l'autre n'atteignait jamais un boîtier déjà installé."""
    import subprocess as sp
    from pathlib import Path
    racine = Path(__file__).resolve().parent.parent
    systemd, etc = tmp_path / "systemd", tmp_path / "etc"
    systemd.mkdir()

    def installer(source):
        return sp.run(["bash", "-c", f'. "{racine}/bootstrap/units.sh"; SYSTEMD_DIR="{systemd}" '
                       f'ETC_DIR="{etc}" DAEMON_RELOAD=: install_service_files "{source}" famille'],
                      capture_output=True).returncode

    assert installer(racine) == 0
    for unite in ("protectado-agent.service", "protectado-runner.service",
                  "protectado-update.service", "protectado-update.path"):
        assert "__" not in (systemd / unite).read_text(), unite
    agent = (systemd / "protectado-agent.service").read_text()
    assert "User=famille" in agent and f"{racine}/.venv/bin/python" in agent
    assert f"{racine}/data" in (etc / "agent.json").read_text()
    assert (etc / "agent.json").stat().st_mode & 0o777 == 0o644
    # Une source absente doit échouer, pas se déclarer réussie.
    assert installer(tmp_path / "absent") != 0


def test_bootstrap_et_updater_passent_par_le_meme_installeur():
    from pathlib import Path
    racine = Path(__file__).resolve().parent.parent / "bootstrap"
    for script in ("bootstrap.sh", "protectado-update.sh"):
        texte = (racine / script).read_text()
        assert "install_service_files" in texte, script
        assert '> /etc/systemd/system/protectado-agent.service' not in texte, script


@pytest.mark.parametrize("reponses,attendu", [("ko ko ok", 0), ("ko ko ko", 1)])
def test_l_attente_de_sante_des_scripts(tmp_path, reponses, attendu):
    """La fonction partagée sonde /api/health jusqu'au premier succès, dans la limite."""
    import subprocess as sp
    from pathlib import Path
    racine = Path(__file__).resolve().parent.parent
    (tmp_path / "reponses").write_text(reponses.replace(" ", "\n") + "\n")
    faux = tmp_path / "curl"
    faux.write_text(f'#!/bin/bash\nr=$(head -1 {tmp_path}/reponses); sed -i 1d {tmp_path}/reponses\n'
                    '[ "$r" = ok ]\n')
    faux.chmod(0o755)
    script = (f'export PATH="{tmp_path}:$PATH"; . "{racine}/bootstrap/units.sh"; '
              f'HEALTH_TRIES=3 HEALTH_PAUSE=0 wait_healthy')
    assert sp.run(["bash", "-c", script]).returncode == attendu


def test_les_scripts_attendent_la_sante_et_non_le_seul_service_actif():
    from pathlib import Path
    racine = Path(__file__).resolve().parent.parent
    for script in ("bootstrap/protectado-update.sh", "bootstrap/bootstrap.sh", "update.sh"):
        texte = (racine / script).read_text()
        assert "wait_healthy" in texte, script
        assert "is-active --quiet protectado-agent; then" not in texte, script


def test_bootstrap_se_relance_une_fois_depuis_la_copie_du_depot(tmp_path):
    """Un bootstrap.sh obsolète (cache de raw.githubusercontent.com, constaté) appliquait
    son ancienne logique au code neuf. Une fois le code en place, la suite revient à la
    copie du dépôt, une seule fois, avec la branche et le commit de départ."""
    import re as re_
    import subprocess as sp
    from pathlib import Path
    texte = (Path(__file__).resolve().parent.parent / "bootstrap/bootstrap.sh").read_text()
    fonction = re_.search(r"^reexec_from_repo\(\) \{.*?^\}", texte, re_.S | re_.M).group(0)
    (tmp_path / "bootstrap").mkdir()
    (tmp_path / "bootstrap/bootstrap.sh").write_text(
        'echo "relance $PROTECTADO_REEXEC $PROTECTADO_BRANCH $PROTECTADO_PREV_COMMIT"\n')

    def lancer(env=""):
        script = (f'log() {{ :; }}; INSTALL_DIR="{tmp_path}"; BRANCH=main; BACKUP_DIR=/b; '
                  f'export PROTECTADO_PREV_COMMIT=abc123; {env}\n{fonction}\n'
                  'reexec_from_repo; echo "suite ancienne"')
        return sp.run(["bash", "-c", script], capture_output=True, text=True).stdout.strip()

    assert lancer() == "relance 1 main abc123"
    assert lancer("export PROTECTADO_REEXEC=1;") == "suite ancienne"
