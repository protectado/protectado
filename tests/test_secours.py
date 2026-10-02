# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Arnaud Ortais
# Dual-licensed: AGPL-3.0 (open source) or Commercial License — see LICENSE and LICENSE-COMMERCIAL.
"""
test_secours.py — le mode secours : quand il s'ouvre, ce qu'il ferme, et le retour à
l'état d'avant quand la box revient.

Le runner est piloté par la fixture `uplink` (clé refusée pendant 15 min = entrée) ;
aucune commande n'est lancée (fixture `runner`).
"""

import json

import pytest

import secours
from conftest import regles

IP = "192.168.50.52"


def _etat(runner):
    return secours.load(f"{runner.DATA_DIR}/{secours.STATE_NAME}")


def _cle_refusee_15_min(uplink):
    uplink["lu"] = (False, True)
    uplink["avancer"](15 * 60)


def _sans_cles(config):
    for profil in config.data["profiles"].values():
        profil.pop("wifi_key", None)
    config.save()


@pytest.mark.parametrize("avec_cles,ap_actif,cas", [(True, True, secours.KIDS),
                                                    (False, True, secours.SETUP),
                                                    (True, False, secours.SETUP)])
def test_le_cas_depend_du_reseau_enfants_diffuse(runner, uplink, config, avec_cles,
                                                  ap_actif, cas):
    if not avec_cles:
        _sans_cles(config)
    if not ap_actif:
        runner.codes[("systemctl", "is-active", "--quiet", "protectado-ap.service")] = 3
    _cle_refusee_15_min(uplink)
    assert (_etat(runner)["active"], _etat(runner)["case"]) == (True, cas)
    configured = json.loads(open(config.path).read())["configured"]
    assert configured is (cas == secours.KIDS)
    assert (("run", "systemctl", "reboot") in runner.journal) is (cas == secours.SETUP)


def test_en_cas_a_personne_n_est_autorise_a_sortir(runner, uplink):
    runner.apply_forward({"ips": [IP], "deny": False})
    assert ("-s", IP, "-j", "ACCEPT") in regles(runner, runner.FWD_CHAIN)
    runner.journal.clear()
    _cle_refusee_15_min(uplink)
    runner.apply_forward({"ips": [IP], "deny": False})
    runner.apply_pihole_mode({"profile": "alice", "mode": "free", "device_ips": [IP]})
    assert not [r for r in regles(runner, runner.FWD_CHAIN) if "ACCEPT" in r]
    # La chaîne a été vidée à l'entrée : seuls le saut des contournements et le refus
    # de fond y sont reposés.
    assert ("ipt", "filter", "-F", runner.FWD_CHAIN) in runner.journal
    # Le DNS et le HTTP des enfants vont au boîtier, avant le forçage DNS de la passerelle.
    assert ("ipt", "nat", "-I", "PREROUTING", "1", "-i", runner.AP_IFACE, "-j",
            runner.SECOURS_NAT) in runner.journal


def test_le_retour_de_la_box_ramene_l_etat_d_avant_cas_a(runner, uplink, config):
    avant = open(config.path).read()
    _cle_refusee_15_min(uplink)
    uplink["lu"] = (True, True)
    uplink["avancer"](60)
    etat = _etat(runner)
    assert not etat["active"] and etat["exit"]["action"] == secours.EXIT_AUTO
    assert open(config.path).read() == avant
    assert ("ipt", "nat", "-X", runner.SECOURS_NAT) in runner.journal
    assert ("run", "systemctl", "restart", "protectado-agent") in runner.journal
    runner.journal.clear()
    runner.apply_forward({"ips": [IP], "deny": False})
    assert [r for r in regles(runner, runner.FWD_CHAIN) if "ACCEPT" in r]


def test_le_retour_de_la_box_ramene_l_etat_d_avant_cas_b(runner, uplink, config):
    _sans_cles(config)
    avant = json.loads(open(config.path).read())
    _cle_refusee_15_min(uplink)
    assert runner._enforcement_mode() == "dns_only"    # posture Setup : rien n'est appliqué
    uplink["lu"] = (True, True)
    uplink["avancer"](60)
    assert json.loads(open(config.path).read()) == avant
    assert runner._enforcement_mode() == "gateway"
    assert runner.journal.count(("run", "systemctl", "reboot")) == 2


# ------------------------------------------------------------------ #
#  Page de secours (cas A)                                            #
# ------------------------------------------------------------------ #

@pytest.fixture
def page(client, tmp_path, monkeypatch):
    """Client du réseau enfants ; actif(True) ouvre le mode secours ; `file` reçoit les
    actions déposées pour le runner."""
    import dashboard
    monkeypatch.setattr(dashboard, "_is_kids_client", lambda request: True)
    file = []
    monkeypatch.setattr(dashboard, "_queue", lambda action, args: file.append((action, args)))

    def actif(oui=True):
        etat = secours.entered(secours.empty_state(), secours.KIDS, "auth_refused", 900)
        secours.save(str(tmp_path / secours.STATE_NAME),
                     etat if oui else secours.exited(etat, secours.EXIT_AUTO, 900))
    client.file, client.actif = file, actif
    return client


def test_la_page_n_existe_qu_en_mode_secours(page):
    assert page.get("/secours").status_code == 404
    assert page.get("/api/secours/state").status_code == 404
    assert page.post("/api/secours/reset", json={"confirm": True}).status_code == 404
    page.actif()
    assert page.get("/secours").status_code == 200
    # Toute autre adresse y mène : c'est le portail captif.
    r = page.get("/hotspot-detect.html", follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (302, "/secours")
    page.actif(False)
    assert page.get("/secours").status_code == 404


def _reconnecter(page, mot_de_passe):
    return page.post("/api/secours/reconnect",
                     json={"ssid": "NouvelleBox", "key": "cle-de-la-box", "password": mot_de_passe})


def test_la_reconnexion_exige_le_mot_de_passe_parent(page, config, runner, monkeypatch):
    page.actif()
    r = _reconnecter(page, "mauvais")
    assert (r.status_code, r.json()["error_code"]) == (403, "bad_password")
    assert page.file == []
    # Le runner revérifie avant tout test de connexion.
    testee = []
    monkeypatch.setattr(runner, "validate_box_wifi", lambda args: testee.append(args))
    runner.secours_reconnect({"ssid": "NouvelleBox", "key": "cle-de-la-box",
                              "password": "mauvais"})
    assert testee == []
    assert _reconnecter(page, config.data["dashboard_password"]).json()["ok"] is True
    assert [a for a, _ in page.file] == ["secours_reconnect"]


def test_les_essais_sont_bloques_meme_apres_un_redemarrage(page, config, monkeypatch):
    page.actif()
    horloge, boot = [100.0], ["boot-1"]
    monkeypatch.setattr("dashboard.time.monotonic", lambda: horloge[0])
    monkeypatch.setattr(secours, "boot_id", lambda: boot[0])
    for _ in range(secours.FREE_ATTEMPTS):
        assert _reconnecter(page, "mauvais").status_code == 403
    r = _reconnecter(page, config.data["dashboard_password"])
    assert (r.status_code, r.json()["error_params"]["sec"]) == (429, secours.LOCK_BASE_SEC)
    # Redémarrage : l'horloge monotone repart de zéro, le blocage repart entier.
    horloge[0], boot[0] = 3.0, "boot-2"
    assert _reconnecter(page, config.data["dashboard_password"]).status_code == 429
    horloge[0] += secours.LOCK_BASE_SEC + 1
    assert _reconnecter(page, config.data["dashboard_password"]).json()["ok"] is True


def test_le_runner_refuse_reconnexion_et_reinitialisation_hors_mode_secours(runner, config,
                                                                            monkeypatch):
    testee = []
    monkeypatch.setattr(runner, "validate_box_wifi", lambda args: testee.append(args))
    runner.secours_reconnect({"ssid": "Box", "key": "cle-de-la-box",
                              "password": config.data["dashboard_password"]})
    runner.secours_reset({})
    assert testee == [] and runner._reset_deadline is None


def test_l_annulation_pendant_le_delai_n_efface_rien(runner, config, monkeypatch):
    secours.save(f"{runner.DATA_DIR}/{secours.STATE_NAME}",
                 secours.entered(secours.empty_state(), secours.KIDS, "auth_refused", 900))
    horloge = [500.0]
    monkeypatch.setattr(runner.time, "monotonic", lambda: horloge[0])
    effacements = []
    monkeypatch.setattr(runner.factory_reset, "run", lambda: effacements.append(1) or
                        {"effaces": [], "erreurs": []})
    runner.secours_reset({})
    horloge[0] += runner.RESET_DELAY_SEC / 2
    runner.secours_reset_cancel({})
    horloge[0] += runner.RESET_DELAY_SEC * 2
    runner._maybe_reset()
    assert effacements == [] and ("run", "systemctl", "reboot") not in runner.journal
    # Sans annulation, l'échéance efface puis redémarre.
    runner.secours_reset({})
    horloge[0] += runner.RESET_DELAY_SEC
    runner._maybe_reset()
    assert effacements == [1] and ("run", "systemctl", "reboot") in runner.journal


# ------------------------------------------------------------------ #
#  Sans réseau enfants (cas B) : Protectado-Setup, ouvert à tous      #
# ------------------------------------------------------------------ #

@pytest.fixture
def setup_b(config, tmp_path):
    """Boîtier configuré, repassé en Protectado-Setup par le mode secours."""
    config.data["configured"] = False
    config.data["network"]["box"] = {"ssid": "AncienneBox", "key": "cle-ancienne-box"}
    config.data.setdefault("openrouter", {})["api_key"] = "sk-or-secret-openrouter"
    config.data.setdefault("pihole", {})["password"] = "secret-pihole"
    config.save()
    secours.save(str(tmp_path / secours.STATE_NAME),
                 secours.entered(secours.empty_state(), secours.SETUP, "not_found", 86400))
    return config


def test_reconnecter_garde_le_mot_de_passe_et_les_profils(runner, setup_b, tmp_path,
                                                          monkeypatch):
    avant = json.loads(open(setup_b.path).read())
    validation = tmp_path / "box_validation.json"
    monkeypatch.setattr(runner, "BOX_VALIDATION", str(validation))
    monkeypatch.setattr(runner, "validate_box_wifi",
                        lambda args: validation.write_text('{"ok": true}'))
    runner.secours_reconnect({"ssid": "NouvelleBox", "key": "cle-nouvelle-box",
                              "password": avant["dashboard_password"]})
    apres = json.loads(open(setup_b.path).read())
    assert apres["dashboard_password"] == avant["dashboard_password"]
    assert apres["profiles"] == avant["profiles"]
    assert apres["configured"] is True
    assert apres["network"]["box"] == {"ssid": "NouvelleBox", "key": "cle-nouvelle-box"}
    assert ("run", "systemctl", "reboot") in runner.journal
    # L'assistant normal ne peut pas réécrire le mot de passe pendant le mode secours.
    secours.save(str(tmp_path / secours.STATE_NAME),
                 secours.entered(secours.empty_state(), secours.SETUP, "not_found", 0))
    runner.apply_configuration({"mode": "gateway", "admin_password": "volé123",
                                "box_ssid": "X"})
    assert json.loads(open(setup_b.path).read())["dashboard_password"] == \
        avant["dashboard_password"]


def test_aucune_reponse_ne_contient_un_secret_enregistre(client, setup_b):
    secrets = [setup_b.data["dashboard_password"], "sk-or-secret-openrouter",
               "secret-pihole", "cle-ancienne-box"]
    secrets += [p["wifi_key"] for p in setup_b.data["profiles"].values() if p.get("wifi_key")]
    reponses = [client.get(url) for url in (
        "/", "/onboarding", "/secours", "/api/secours/state", "/api/onboarding/state",
        "/api/onboarding/scan", "/api/onboarding/validate", "/api/child/status")]
    reponses += [client.post("/api/onboarding/prepare",
                             json={"admin_password": "volé123", "box_ssid": "X"}),
                 client.post("/api/onboarding/finish", json={})]
    for r in reponses:
        assert not [s for s in secrets if s in r.text], r.url
    assert json.loads(open(setup_b.path).read())["dashboard_password"] == secrets[0]


def test_mauvais_mot_de_passe_refuse_sur_protectado_setup(client, setup_b):
    r = client.post("/api/secours/reconnect",
                    json={"ssid": "NouvelleBox", "key": "cle-nouvelle-box", "password": "x"})
    assert (r.status_code, r.json()["error_code"]) == (403, "bad_password")


def test_la_page_enfant_previent_avant_le_mode_secours(client, tmp_path):
    import uplink_watch
    assert client.get("/api/child/status").json()["uplink_lost"] is None
    uplink_watch.save(str(tmp_path / uplink_watch.STATE_NAME),
                      uplink_watch.step(uplink_watch.empty_state(), False, False, 0)
                      | {"offline_sec": 2 * 3600 + 5 * 60})
    assert client.get("/api/child/status").json()["uplink_lost"] == {"h": 2, "min": 5}
