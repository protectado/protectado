# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Arnaud Ortais
# Dual-licensed: AGPL-3.0 (open source) or Commercial License — see LICENSE and LICENSE-COMMERCIAL.
"""
monitor.py — Moteur de surveillance Python pur, sans IA.

Tourne toutes les 60 secondes.
Gère les règles déterministes :
  - Couvre-feu (heure)
  - Quota YouTube (minutes)
  - Bypass DNS
  - Domaines bloqués accédés

Appelle claude_agent.py UNIQUEMENT pour les patterns inhabituels.
Coût IA : ~0 en fonctionnement normal.
"""

import json
import time
import threading
import os
from collections import deque
from datetime import datetime, date

from paths import CONFIG_PATH, DATA_DIR
from pihole_api import PiHoleAPI
from scheduler import get_slot_at, get_all_temp_overrides, reopening_at
import domain_classifier as classifier
from arp_scanner import ARPScanner
import database as db
import modes
from access_control import apply_device_access
import services
import station_identity
import action_queue
from domain_names import root_domain

# Ce qui rend un domaine « inhabituel », donc digne d'un appel au modèle.
#
# L'ANCIEN CRITÈRE ÉTAIT INOPÉRANT : il définissait inhabituel par « absent du catalogue
# de domaines », alors que run_cycle vient d'y inscrire chaque domaine du cycle quelques
# lignes plus haut. Les seuls qui restaient absents étaient l'infrastructure partagée,
# écartée par record_domain, et c'est justement elle qui franchit le plus vite n'importe
# quel seuil de volume. Le produit payait donc un appel au modèle pour faire commenter du
# trafic de CDN, et écrivait trois fois la même phrase au parent.
#
# Deux signaux le remplacent, tous deux relatifs à l'enfant concerné :
#   1. VOLUME ANORMAL : le total du jour dépasse largement la moyenne de cet enfant sur
#      ce domaine les jours précédents. « Beaucoup » n'a de sens que comparé à son propre
#      usage : 400 requêtes vers un domaine de streaming sont banales chez l'un et
#      inédites chez l'autre.
#   2. PREMIÈRE FOIS : un domaine d'une catégorie sensible, connu du catalogue, mais dont
#      cet enfant n'a aucun historique. C'est le signal « il a découvert quelque chose ».
UNUSUAL_QUERY_THRESHOLD = 50   # plancher absolu : en dessous, aucun volume n'est notable
UNUSUAL_VOLUME_RATIO    = 4.0  # « largement » : quatre fois sa propre moyenne
UNUSUAL_BASELINE_MIN    = 20   # moyenne trop faible = pas une référence, on n'en tire rien
UNUSUAL_HISTORY_DAYS    = 7    # profondeur de la référence
UNUSUAL_FIRST_TIME_HITS = 25   # plancher du signal « première fois », pour écarter un clic
# Nombre de jours d'historique exigés AVANT que « première fois » veuille dire quelque
# chose. Sur un boîtier neuf, tout est une première fois : le premier soir, chaque site
# social ou de divertissement de l'enfant déclenchait le signal, donc une escalade vers le
# modèle tous les trois domaines, et autant de paragraphes dans le journal du parent.
# On ne prétend rien savoir d'un enfant qu'on observe depuis deux jours.
UNUSUAL_FIRST_TIME_MIN_DAYS = 3
# Catégories où une première apparition mérite d'être regardée. Un premier accès à un site
# éducatif ou de travail n'est pas un événement ; il n'y a aucune raison de le faire
# commenter par un modèle, ni de l'écrire au parent.
UNUSUAL_FIRST_TIME_CATEGORIES = frozenset({"entertainment", "social", "adult", "extremism"})
ESCALATE_AFTER = 3             # événements inhabituels avant escalade Claude

# Déduplication des domaines bloqués
BLOCK_WINDOW_SEC     = 600    # 10 min — une seule alerte par fenêtre
# Interrogation immédiate d'un domaine qui vient de bloquer quelqu'un : une seule fois par
# domaine et par heure. Sans cette fenêtre, un domaine bloqué en boucle par une
# application qui réessaie produirait une résolution par cycle.
CF_IMMEDIATE_WINDOW_SEC = 3600
KEEPALIVE_MAX_HITS   = 4      # ≤ 4 hits/cycle (fenêtre Pi-hole 5 min) → keepalive
KEEPALIVE_MIN_CYCLES = 3      # 3 cycles consécutifs bas → silence total
# Oubli de l'état d'un domaine bloqué resté muet aussi longtemps. Large devant la fenêtre
# de 10 min et les cycles de keepalive : l'oubli ne fait que suspendre le jugement au
# retour du domaine (aucun événement de plus), il borne seulement la mémoire.
BLOCK_STATE_TTL_SEC  = 24 * 3600
# Guet du fichier d'identité : délai entre « l'appareil a son bail » et « ses règles
# s'appliquent ». Le runner republie toutes les quelques secondes, on suit le même rythme.
IDENTITY_WATCH_SECONDS = 3
# Au plus tard, l'état de chaque profil est réappliqué à cet intervalle même sans
# changement : une action perdue ou un Pi-hole indisponible au moment d'un changement ne
# laisse pas un enfant sur un mauvais état jusqu'au changement de plage suivant.
REAPPLY_INTERVAL_SEC = 15 * 60

# Tunnel probable (VPN), en passerelle : sur un quart d'heure, un appareil actif dont presque
# tout le trafic va vers une seule adresse, sans requête DNS au boîtier. SEUILS
# EXTRAPOLÉS, NON CALIBRÉS sur un vrai VPN : à ajuster sur mesure réelle. Signalement
# seulement, jamais de coupure : un faux positif ne doit priver personne d'Internet.
VPN_WINDOW_SEC = 900         # un quart d'heure : signalé vite, sans juger sur une rafale
VPN_ACTIVE_BYTES = 40_000    # par relevé d'une minute : au-dessus d'une veille (notifications)
VPN_TOP_SHARE = 0.9          # part de la destination principale dans ce relevé
VPN_MIN_SAMPLES = 12         # relevés « tunnel » vers la même adresse, sur 15 dans la fenêtre
# Domaines DISTINCTS demandés au boîtier tolérés sur la fenêtre. On compte des domaines
# et non des requêtes : mesuré sur le boîtier, un iPhone en VPN ne demande QUE la
# vérification de connectivité d'iOS (netcts.cdn-apple.com), hors du tunnel, mais il la
# répète 7 fois un quart d'heure et 13 fois le suivant. Un seuil en requêtes manquait le
# tunnel une fois sur deux ; un appareil hors tunnel, lui, résout des dizaines de noms.
VPN_MAX_DNS_DOMAINS = 3
# Fin d'une session de tunnel : plus aucun relevé « tunnel » vers son adresse depuis ce
# délai (trois relevés d'une minute manqués). Mesuré sur le boîtier : un relevé isolé à
# 47 ko au milieu d'un tunnel actif, d'où une marge plutôt qu'une fin au premier creux.
VPN_END_GAP_SEC = 180
# Silence entre deux signalements d'une même clé inconnue sur un même appareil. Long :
# c'est une anomalie de configuration, pas un incident qui évolue de minute en minute.
UNKNOWN_KEY_SILENCE_SEC = 6 * 3600

# Contournement DNS (dns_only) : durée de silence AVANT d'alerter. Un appareil en veille ne
# résout rien non plus — seule la persistance distingue le contournement de l'inactivité.
BYPASS_SILENCE_HOURS = 4


_global_monitor: "ProtectadoMonitor | None" = None


def notify_monitor():
    """Réveille le monitor global immédiatement (après une écriture en DB)."""
    if _global_monitor is not None:
        _global_monitor.notify()


class ProtectadoMonitor:
    # Empreinte des fichiers du catalogue des services, pour le recharger à chaud.
    # None = jamais relevée : le premier passage ne recharge rien, il enregistre.
    #
    # Déclarée sur la CLASSE et non dans __init__, contrairement au reste de l'état : le
    # cycle la lit, et les tests construisent un moniteur par __new__ pour éprouver une
    # méthode sans monter un boîtier entier. Un défaut de classe leur évite de recopier
    # cette ligne en six endroits, et à un futur attribut de même nature de les casser.
    _services_stamp: tuple | None = None
    # Un seul appel IA d'escalade en vol (cf. _escalate_to_claude). Sur la classe, pour la
    # même raison que _services_stamp ; un seul moniteur existe par processus.
    _escalation_lock = threading.Lock()

    def __init__(self, config_path: str = CONFIG_PATH):
        with open(config_path) as f:
            self.config = json.load(f)

        self.pihole = PiHoleAPI(
            self.config["pihole"]["host"],
            self.config["pihole"]["password"]
        )
        self.scanner = ARPScanner(self.pihole)
        self._running = False
        self._wakeup = threading.Event()
        self._unusual_events = []  # buffer avant escalade vers Claude
        # (profil, domaine, jour) déjà signalés. En mémoire comme _bypass_alerted :
        # perdre cette trace à un redémarrage ne coûte qu'un doublon d'événement.
        self._unusual_seen: set = set()
        self._block_state: dict = {}   # (profile, domain) → état déduplication
        # domaine → dernière interrogation immédiate de Cloudflare. En mémoire : le pire
        # effet d'un redémarrage est une résolution DNS de plus.
        self._cf_asked: dict = {}
        # profile → (mode appliqué, cet état vient-il d'une dérogation ?). Le drapeau
        # sert à ne PAS annoncer un « changement de plage » quand c'est une dérogation
        # qui prend ou rend la main : ces deux transitions ont déjà leur propre message.
        self._last_slot: dict = {}
        # profile → adresses pour lesquelles l'accès a été appliqué. Complète _last_slot :
        # en liste blanche, un appareil qui apparaît est un changement d'état à appliquer.
        self._last_ips: dict = {}
        # profile → instant (time.monotonic) de la dernière application de son état.
        self._last_applied_at: dict = {}
        # Instant (time.time) du dernier cycle COMPLET, lu par /api/health : un moniteur
        # qui démarre mais échoue à chaque cycle doit faire échouer le contrôle de santé.
        self.last_cycle_ok = None
        self.interval = 60
        self._cloudflare_cycle: int = 0  # compteur pour classification périodique
        # Progression de la COMPTABILISATION. Le cycle tourne toutes les 60 s mais lit une
        # fenêtre de 5 min : sans ce repère, chaque requête était comptée dans 5 cycles
        # consécutifs et tous les chiffres montrés au parent (top domaines, temps passé,
        # rapports IA) étaient gonflés d'un facteur ~5.
        # (clé, valeur) du dernier élément compté — voir _query_marker().
        self._counted_marker: tuple | None = None
        self._marker_warned: bool = False
        self._arp_cycle: int = 0        # compteur pour le scan ARP périodique (dns_only)
        # Appareils déjà signalés comme contournant le DNS → une alerte par appareil et
        # par jour. En mémoire volontairement : une alerte informative répétée après un
        # redémarrage est bénigne, contrairement à une dérogation parentale perdue.
        self._bypass_alerted: dict = {}
        # Tentatives de contournement refusées en passerelle (cf. _check_bypass_attempts) :
        # (ip, type) → dernier compteur vu, génération des compteurs, et (ip, type) →
        # jour du dernier signalement. En mémoire, comme les autres déduplications.
        self._bypass_counts: dict = {}
        self._bypass_generation = None
        self._bypass_attempt_alerted: dict = {}
        # Détection de tunnel : ip → [(instant, requêtes DNS neuves)] sur la fenêtre. Les
        # sessions elles-mêmes vivent en base (vpn_sessions) : elles survivent à un
        # redémarrage de l'agent, qui sans cela signalait de nouveau le même tunnel.
        self._dns_history: dict = {}
        self._bypass_silence: dict = {}  # ip → début du silence DNS constaté
        # Identité des stations publiée par le runner (cf. station_identity.py). En
        # passerelle, c'est ELLE qui dit quel appareil appartient à quel profil, à la
        # place de la liste d'adresses IP saisie à la main.
        self._identity_path = os.path.join(DATA_DIR, station_identity.IDENTITY_NAME)
        self._identity_seen: str = ""    # updated_at du dernier état appliqué
        # (mac, keyid) → dernier signalement d'une clé valide sans profil. En mémoire,
        # comme les autres déduplications d'alerte : un doublon après redémarrage est bénin.
        self._unknown_key_alerted: dict = {}
        # Horodatage du dernier état connu de l'effecteur d'accès (data/gateway_status.json).
        # Sert à repousser les règles quand la couche paquet s'active APRÈS le démarrage.
        self._enforcement_stamp: str | None = None

        db.init_db()

    def reload_config(self):
        """Recharge config sans redémarrer le service."""
        with open(CONFIG_PATH) as f:
            self.config = json.load(f)
        # Le client Pi-hole doit suivre un changement d'hôte ou de mot de passe : sinon le
        # processus garde des identifiants périmés jusqu'au prochain redémarrage du service
        # et TOUS les appels échouent (listes vides, page Appareils blanche).
        ph = self.config.get("pihole", {})
        # host normalisé comme dans PiHoleAPI (rstrip '/'), sinon une barre finale dans
        # config.json ferait recréer le client à chaque rechargement.
        if ((ph.get("host") or "").rstrip("/") != self.pihole.host
                or ph.get("password") != self.pihole.password):
            print("[Monitor] Identifiants Pi-hole modifiés — client recréé")
            self.pihole = PiHoleAPI(ph.get("host", ""), ph.get("password", ""))
        self.scanner = ARPScanner(self.pihole)

    # ------------------------------------------------------------------ #
    #  File d'actions                                                     #
    # ------------------------------------------------------------------ #

    def _queue_action(self, action: str, args: dict):
        action_queue.enqueue(action, args)
        print(f"[Monitor] → {action}({args.get('ip', args.get('domain', ''))})")

    # ------------------------------------------------------------------ #
    #  Règles déterministes (sans IA)                                    #
    # ------------------------------------------------------------------ #

    def _check_schedule(self, profile_key: str, profile: dict, active_ips: set):
        """Applique le slot horaire courant — aucune IA nécessaire."""
        is_monitoring = profile.get("mode") == "monitoring"
        if is_monitoring:
            return

        slot = get_slot_at(profile_key, datetime.now())
        mode = slot["mode"]
        # get_slot_at() renvoie l'état EFFECTIF : une dérogation temporaire ou une
        # exception de journée y écrase le planning et rapporte 00:00–23:59. Pris pour
        # une plage horaire, cela produisait un « changement de plage : bloqué jusqu'à
        # 23:59 » chaque fois qu'une dérogation commençait ou se terminait — un doublon
        # du message de la dérogation elle-même, et une heure de fin inventée.
        state = (mode, bool(slot.get("override")))

        prev = self._last_slot.get(profile_key)
        # La liste d'appareils fait partie de l'état APPLIQUÉ, pas seulement le mode.
        # Le droit d'accès est maintenant une liste blanche par adresse : un appareil qui
        # rejoint le réseau au milieu d'une plage n'a AUCUNE autorisation tant qu'on n'a
        # pas réappliqué pour lui. Ne réappliquer qu'aux changements de plage laissait
        # donc un enfant sans réseau jusqu'à la plage suivante, parfois des heures.
        ips = tuple(sorted((d or {}).get("ip", "") for d in (profile.get("devices") or [])))
        prev_ips = self._last_ips.get(profile_key)

        if prev != state:
            print(f"[Monitor] {profile_key} : {prev} → {state} ({slot['slot_start']}-{slot['slot_end']})")
            # Rien à annoncer dans deux cas : au tout premier passage (démarrage du
            # service ou réarmement de l'effecteur — l'état n'a pas changé pour l'enfant,
            # c'est le monitor qui l'apprend), et sur les transitions de dérogation.
            if prev is not None and not state[1] and not prev[1]:
                if modes.is_open(mode):
                    db.log_event(profile_key, "info", "",
                                 f"Changement de plage : {mode} jusqu\'à {slot['slot_end']}",
                                 message_key="event.mode_change",
                                 params={"mode": mode, "until": slot["slot_end"]})
                else:
                    # Un créneau fermé rapporte 00:00–23:59 : ce sont des bornes de
                    # journée, pas une heure de fin. Le journal annonçait donc « coupé
                    # jusqu'à 23:59 » à 21 h, alors que l'accès rouvrait le lendemain à
                    # 7 h. On écrit l'instant de RÉOUVERTURE, sous forme d'horodatage
                    # brut pour qu'aucune langue ne se glisse dans un paramètre
                    # d'événement.
                    rouvre = reopening_at(profile_key)
                    if rouvre:
                        quand = rouvre.strftime("%Y-%m-%d %H:%M")
                        db.log_event(profile_key, "info", "",
                                     f"Accès coupé — réouverture le {quand}",
                                     message_key="event.access_closed",
                                     params={"at": quand})
                    else:
                        db.log_event(profile_key, "info", "",
                                     "Accès coupé — aucune réouverture prévue cette semaine",
                                     message_key="event.access_closed_no_opening",
                                     params={})

        maintenant = time.monotonic()
        perime = (maintenant - self._last_applied_at.get(profile_key, maintenant)
                  >= REAPPLY_INTERVAL_SEC)
        if prev != state or prev_ips != ips or perime:
            self._last_slot[profile_key] = state
            self._last_ips[profile_key] = ips
            self._last_applied_at[profile_key] = maintenant
            # Appliquer via Pi-hole selon le mode, et l'effecteur paquet côté runner
            self._apply_pihole_mode(profile_key, mode)

        # En "dns_only" (défaut) : le changement de mode est entièrement géré par Pi-hole
        # via _apply_pihole_mode, sans iptables (le Pi n'est pas routeur). En "gateway",
        # l'effet FORWARD est appliqué côté runner (root), pas ici — voir access_control /
        # action_runner. Aucun présupposé INCONDITIONNEL « iptables inutile » à ce niveau.

    def _sync_identity(self) -> bool:
        """Rattache les appareils à leur profil d'après la clé Wi-Fi utilisée, pas l'adresse.

        POURQUOI EN MÉMOIRE SEULEMENT. La liste d'appareils d'un profil devient une
        LECTURE de l'état du réseau, pas une intention du parent : elle change à chaque
        bail DHCP. L'écrire dans config.json ferait battre le fichier sans arrêt et
        risquerait d'écraser une modification de profil faite au même instant depuis le
        tableau de bord. La configuration reste ce que le parent a voulu ; la réalité vit
        ici et dans data/station_identity.json.

        ABSTENTION PLUTÔT QUE SUPPOSITION : si le fichier manque, est illisible ou trop
        vieux (runner arrêté), on ne touche à rien et les listes de config continuent de
        s'appliquer comme avant. Aucune régression possible par rapport au comportement
        antérieur.

        Les appareils hors du sous-réseau enfants sont conservés : ils ne passent pas par
        le point d'accès, donc l'identité par clé ne les concerne pas.
        """
        if (self.config.get("network") or {}).get("enforcement") != "gateway":
            return False
        identity = station_identity.load(self._identity_path)
        changed = station_identity.apply_to_config(self.config, identity)
        # Mémoire des appareils déjà vus, pour que l'écran distingue « jamais connecté »
        # de « pas connecté en ce moment ». Seulement sur une identité fraîche : une
        # identité périmée ferait croire qu'un appareil parti est encore là.
        if station_identity.is_fresh(identity):
            for pname, profile in (self.config.get("profiles") or {}).items():
                if (profile or {}).get("mode") != "monitoring":
                    db.record_seen_devices(pname, profile.get("devices") or [])
        if changed:
            for pname, profile in (self.config.get("profiles") or {}).items():
                if (profile or {}).get("mode") == "monitoring":
                    continue
                ips = [d.get("ip", "") for d in (profile.get("devices") or [])]
                print(f"[Monitor] {pname} : appareils reconnus par leur clé → "
                      f"{', '.join(ips) or 'aucun'}")
        self._signaler_cles_inconnues(identity)
        return changed

    def _signaler_cles_inconnues(self, identity: dict):
        """Signale une station qui a PROUVÉ connaître une clé sans qu'elle mène à un profil.

        DEUX FILTRES, chacun corrigeant un défaut constaté sur un vrai boîtier.

        1. La station doit être AUTORISÉE, c'est-à-dire avoir réussi sa poignée de main
           WPA. Un téléphone sur lequel traîne une clé périmée s'associe au niveau radio,
           échoue, se fait déconnecter et recommence : il apparaissait comme une station
           « sans profil » à chaque rafraîchissement. C'est un non-événement, et il
           remplissait le journal du parent de plusieurs lignes par minute.

        2. Une seule ligne par appareil et par clé, avec une longue fenêtre de silence.
           Le repère précédent était l'horodatage du fichier d'identité, qui change à
           chaque publication : il ne dédupliquait rien du tout.

        Reste donc le seul cas vraiment anormal : une clé valide qui ne correspond à aucun
        profil, typiquement un profil supprimé dont l'appareil est encore connecté.
        """
        now = datetime.now()
        for st in (identity.get("stations") or []):
            if st.get("profile") or not st.get("authorized"):
                continue
            mac, keyid = st.get("mac", ""), st.get("keyid", "")
            repere = (mac, keyid)
            derniere = self._unknown_key_alerted.get(repere)
            if derniere and (now - derniere).total_seconds() < UNKNOWN_KEY_SILENCE_SEC:
                continue
            self._unknown_key_alerted[repere] = now
            db.log_event("global", "warning", mac,
                         f"Appareil connecté avec une clé qui ne correspond à aucun "
                         f"profil (clé « {keyid or 'sans nom'} »)",
                         message_key="event.station_unknown_key",
                         params={"mac": mac, "keyid": keyid or "?"})

    def _apply_pihole_mode(self, profile_key: str, mode: str):
        """
        Configure Pi-hole selon le mode actif.
        Passe les IPs des appareils pour basculer leur groupe.
        """
        # Le profil monitoring n'a pas de groupes Pi-hole
        if self.config["profiles"].get(profile_key, {}).get("mode") == "monitoring":
            return

        profile = self.config["profiles"].get(profile_key, {})
        device_ips = [d["ip"] for d in profile.get("devices", [])]
        # La liste dépend de l'ENFANT depuis la grille d'accès : le même mode ne bloque
        # pas les mêmes catégories à 6 ans et à 16 ans.
        blacklist = classifier.get_active_blacklist(mode, profile, profile_key)
        self._queue_action("apply_pihole_mode", {
            "profile": profile_key,
            "mode": mode,
            "device_ips": device_ips,
            "blacklist": blacklist
        })

    def _check_blocked_domains(self, profile_key: str, profile: dict,
                                queries_by_ip: dict):
        """
        Logue les accès à des domaines bloqués avec déduplication :
        - Keepalives (≤ KEEPALIVE_MAX_HITS hits/cycle pendant KEEPALIVE_MIN_CYCLES cycles)
          → silence total, aucun log.
        - Activité réelle → un seul événement au début de chaque fenêtre de 10 min.
          Après 10 min de silence, la prochaine tentative génère un nouvel événement.
        """
        import domain_classifier as dc
        current_mode = get_slot_at(profile_key, datetime.now())["mode"]
        active_blacklist = set(dc.get_active_blacklist(current_mode, profile, profile_key))
        now = datetime.now()

        # Compter les hits par domaine root bloqué pour ce cycle
        domain_hits: dict[str, int] = {}
        for device in profile.get("devices", []):
            for domain in queries_by_ip.get(device["ip"], []):
                root = self._root_domain(domain)
                if root in active_blacklist:
                    domain_hits[root] = domain_hits.get(root, 0) + 1

        for domain, hits in domain_hits.items():
            key = (profile_key, domain)
            if key not in self._block_state:
                self._block_state[key] = {
                    "window_start": None,
                    "window_count": 0,
                    "hit_history": deque(maxlen=KEEPALIVE_MIN_CYCLES + 2),
                    "last_seen": None,
                }
            state = self._block_state[key]
            state["hit_history"].append(hits)
            state["last_seen"] = now
            history = list(state["hit_history"])

            # 1. Pas assez d'historique + trafic faible → suspendre le jugement
            if hits <= KEEPALIVE_MAX_HITS and len(history) < KEEPALIVE_MIN_CYCLES:
                continue

            # 2. Keepalive confirmé → silence total
            if (len(history) >= KEEPALIVE_MIN_CYCLES and
                    all(h <= KEEPALIVE_MAX_HITS for h in history[-KEEPALIVE_MIN_CYCLES:])):
                state["window_start"] = None
                state["window_count"] = 0
                continue

            # 3. Activité réelle — déduplication par fenêtre de 10 min
            in_window = (
                state["window_start"] is not None and
                (now - state["window_start"]).total_seconds() < BLOCK_WINDOW_SEC
            )
            if in_window:
                state["window_count"] += hits
            else:
                # Nouvelle fenêtre → un seul log
                state["window_start"] = now
                state["window_count"] = hits
                suffix = f" ({hits} tentatives)" if hits > 1 else ""
                db.log_event(
                    profile_key, "warning", domain,
                    f"Tentative d'accès à {domain} (bloqué — {current_mode}){suffix}",
                    message_key=("event.blocked_attempt_multi" if hits > 1
                                 else "event.blocked_attempt"),
                    params={"domain": domain, "mode": current_mode, "n": hits},
                )
                print(f"[Monitor] 🚫 {profile_key} → {domain} ({hits} req, {current_mode})")
                # Un domaine qui bloque VRAIMENT quelqu'un mérite d'être identifié tout
                # de suite : c'est celui dont le parent va demander ce qu'il est, et il
                # attendait jusqu'ici la passe des dix minutes, voire le modèle du soir.
                self._classify_blocked_domain(domain)

    def _check_unusual_patterns(self, profile_key: str, profile: dict,
                                 queries_by_ip: dict, known: dict, history: dict):
        """Repère ce qui sort de l'ordinaire POUR CET ENFANT, et l'accumule pour escalade.

        `queries_by_ip` : requêtes JAMAIS COMPTABILISÉES de ce cycle (by_ip_new). La
        fenêtre Pi-hole fait 5 min et le cycle 60 s : passer la fenêtre pleine faisait
        relire la même rafale par cinq cycles consécutifs, donc cinq événements identiques
        et jusqu'à cinq appels au modèle pour un seul comportement.

        `known` : {domaine: catégorie} du catalogue, prélu UNE fois pour tous les profils.
        La prélecture précède l'inscription des domaines de ce cycle, donc un domaine vu
        pour la toute première fois n'y figure pas encore : c'est voulu, le signal
        « première fois » porte sur un domaine DÉJÀ classé que cet enfant n'avait jamais
        visité, pas sur un domaine que personne n'a jamais vu. Il se déclenchera au cycle
        suivant, le temps que le compteur dépasse le plancher.
        `history` : {(profil, domaine): {date: compteur}}, prélu de même.

        TOUT EST RAISONNÉ PAR SERVICE, pas par domaine (cf. services.py). Une soirée de
        YouTube se répartit sur quatre domaines : chacun paraissait alors normal au regard
        de sa propre habitude, alors que le service entier avait quadruplé. Et un
        signalement par domaine écrivait quatre lignes au parent pour une seule soirée.

        Deux signaux, cf. les constantes en tête de module. L'infrastructure partagée est
        écartée : c'est du bruit, et c'est elle qui franchit le plus vite les seuils.
        """
        import domain_classifier as dc

        usage_today = db.get_usage_today(profile_key)

        # Regroupement par service, à trois endroits qui doivent être d'accord : le total
        # du jour, l'historique, et les domaines vus dans ce cycle.
        def libelle(domaine):
            return services.label_for_domain(domaine)

        total_par_service: dict = {}
        for domaine, n in usage_today.items():
            total_par_service[libelle(domaine)] = total_par_service.get(libelle(domaine), 0) + n

        # Historique additionné JOUR PAR JOUR : deux domaines d'un même service sollicités
        # le même soir comptent pour une seule journée, pas pour deux.
        histoire_par_service: dict = {}
        jours_du_profil = set()
        for (prof, domaine), par_date in history.items():
            if prof != profile_key:
                continue
            jours_du_profil |= set(par_date)
            cible = histoire_par_service.setdefault(libelle(domaine), {})
            for jour, n in par_date.items():
                cible[jour] = cible.get(jour, 0) + n

        # Profondeur d'observation de CET enfant. En dessous du minimum, le signal
        # « première fois » est muet, faute de savoir ce qui est habituel pour lui.
        jours_connus = len(jours_du_profil)

        # Services vus DANS CE CYCLE : un pic ne se signale que pendant qu'il a lieu, pas
        # tous les cycles de la journée parce que le total du jour reste élevé.
        vus_ce_cycle: dict = {}
        for device in profile.get("devices", []):
            for domain in queries_by_ip.get(device.get("ip", ""), []):
                root = self._root_domain(domain)
                if root and not dc.is_cdn(root):
                    vus_ce_cycle.setdefault(libelle(root), set()).add(root)

        for domain in sorted(vus_ce_cycle):
            # Un seul événement par enfant, par service et par jour. L'ancien code
            # dédupliquait par hasard (le domaine devenait « connu »), ce qui échouait
            # dès que le critère devenait autre chose que « inconnu ».
            marqueur = (profile_key, domain, date.today().isoformat())
            if marqueur in self._unusual_seen:
                continue

            total_jour = total_par_service.get(domain, 0)
            jours = list(histoire_par_service.get(domain, {}).values())
            moyenne = (sum(jours) / len(jours)) if jours else 0.0
            # Catégorie du service : ses domaines la partagent par construction. Pour un
            # domaine isolé, c'est celle du catalogue.
            un_domaine = sorted(vus_ce_cycle[domain])[0]
            categorie = (services.category_of(services.service_of(un_domaine))
                         if services.service_of(un_domaine) else known.get(un_domaine))

            motif = None
            if (moyenne >= UNUSUAL_BASELINE_MIN
                    and total_jour >= UNUSUAL_QUERY_THRESHOLD
                    and total_jour >= moyenne * UNUSUAL_VOLUME_RATIO):
                motif = "volume"
            elif (not jours
                  and jours_connus >= UNUSUAL_FIRST_TIME_MIN_DAYS
                  and categorie in UNUSUAL_FIRST_TIME_CATEGORIES
                  and total_jour >= UNUSUAL_FIRST_TIME_HITS):
                motif = "premiere_fois"

            if not motif:
                continue

            self._unusual_seen.add(marqueur)
            self._unusual_events.append({
                "profile": profile_key,
                "domain": domain,
                "count": total_jour,
                "reason": motif,
                "category": categorie or "unknown",
                "baseline": round(moyenne, 1),
                "timestamp": datetime.now().isoformat(),
            })
            print(f"[Monitor] Inhabituel ({motif}) : {domain} × {total_jour} "
                  f"pour {profile_key} (moyenne {moyenne:.0f})")

        # Escalade vers Claude si assez d'événements inhabituels
        if len(self._unusual_events) >= ESCALATE_AFTER:
            self._escalate_to_claude()

    def _escalate_to_claude(self):
        """Confie les patterns inhabituels à l'IA, dans un fil à part.

        Un appel IA dure plusieurs secondes, jusqu'à son délai : fait dans le cycle, il
        retenait le planning de tous les enfants pendant ce temps. Un seul appel en vol
        à la fois ; tant qu'il dure, les nouveaux événements restent dans le tampon et
        partiront avec l'escalade suivante.
        """
        if not self._escalation_lock.acquire(blocking=False):
            return
        lot, self._unusual_events = list(self._unusual_events), []

        def analyser():
            try:
                from claude_agent import analyze_unusual_patterns
                analyze_unusual_patterns(lot)
            except Exception as e:
                print(f"[Monitor] Erreur escalade Claude : {e}")
            finally:
                self._escalation_lock.release()

        self._escalation_thread = threading.Thread(target=analyser, daemon=True)
        self._escalation_thread.start()

    # ------------------------------------------------------------------ #
    #  Helpers                                                            #
    # ------------------------------------------------------------------ #



    # ------------------------------------------------------------------ #
    #  Comptabilisation : ne compter chaque requête qu'UNE fois           #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _query_marker(q: dict):
        """Repère monotone d'une requête FTL : ('id', n) si disponible, sinon ('time', t).

        L'identifiant est préféré à l'horodatage : il est strictement croissant et
        insensible à un changement d'heure système.
        """
        for key in ("id", "time"):
            v = q.get(key)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                return (key, v)
        return None

    def _uncounted(self, queries: list) -> list:
        """Requêtes pas encore comptabilisées, et avance du repère.

        Repli sûr : si FTL ne fournit ni identifiant ni horodatage exploitable, on
        conserve le comportement historique (tout compter) en le signalant une fois —
        mieux vaut un sur-comptage visible dans les logs qu'un silence qui ferait
        disparaître des requêtes.
        """
        marked = [(self._query_marker(q), q) for q in queries]
        usable = [(m, q) for m, q in marked if m is not None]

        if not usable:
            if queries and not self._marker_warned:
                self._marker_warned = True
                print("[Monitor] ⚠ requêtes FTL sans 'id' ni 'time' — comptabilisation "
                      "non dédupliquée (chiffres potentiellement gonflés)")
            return queries

        key = usable[0][0][0]
        values = [m[1] for m, _ in usable if m[0] == key]
        newest = max(values)

        prev = self._counted_marker
        if prev is None or prev[0] != key:
            fresh = [q for _, q in usable]          # premier cycle : on part de zéro
        elif newest < prev[1]:
            # Repère qui recule : FTL a redémarré (identifiants remis à zéro) ou l'heure
            # a été ajustée. On repart du lot courant plutôt que de tout ignorer.
            print("[Monitor] repère de comptabilisation réinitialisé (redémarrage FTL ?)")
            fresh = [q for _, q in usable]
        else:
            fresh = [q for m, q in usable if m[0] == key and m[1] > prev[1]]

        self._counted_marker = (key, newest)
        return fresh

    # Réduction au domaine racine : partagée avec l'assistant (cf. domain_names), pour
    # qu'un déblocage et un décompte parlent du même domaine.
    _root_domain = staticmethod(root_domain)

    # ------------------------------------------------------------------ #
    #  Cycle principal                                                    #
    # ------------------------------------------------------------------ #

    def _check_expired_overrides(self):
        """Remet les appareils dont le mode adulte a expiré dans leur groupe normal."""
        for ip in db.get_expired_override_ips():
            profile_key = None
            for pkey, profile in self.config["profiles"].items():
                if any(d["ip"] == ip for d in profile.get("devices", [])):
                    profile_key = pkey
                    break
            db.clear_device_override(ip)
            if profile_key:
                slot = get_slot_at(profile_key, datetime.now())
                # Point de passage unique — DNS aujourd'hui, + FORWARD en gateway (étape 2).
                apply_device_access(
                    self.pihole, [ip],
                    mode=slot["mode"], group=f"{profile_key}-{slot['mode']}",
                    profile=profile_key, reason="override_adulte_expiré",
                )
                db.log_event(profile_key, "info", ip,
                             "Mode adulte terminé — retour profil enfant",
                             message_key="event.adult_mode_ended")
                print(f"[Monitor] ⏱ Override expiré : {ip} → {profile_key}-{slot['mode']}")

    def _check_expired_temp_state(self):
        """Rattrape les dérogations temporaires échues — overrides de mode et déblocages
        de domaines.

        Les appelants posent aussi un threading.Timer pour la réactivité immédiate, mais
        un timer ne survit pas au redémarrage du service (l'auto-update en provoque un
        chaque nuit). C'est donc ICI que la correction est garantie : la base fait
        autorité, le cycle repasse toutes les 60 s. Idempotent — si le timer a déjà fait
        le travail, l'entrée a disparu de la base et il n'y a rien à faire.
        """
        # 1) Overrides de mode : get_all_temp_overrides() purge les entrées échues ;
        #    on compare avant/après pour savoir lesquelles viennent d'expirer.
        before = {o["profile"] for o in db.get_temp_overrides()}
        still_active = {o["profile"] for o in get_all_temp_overrides()}
        for profile in before - still_active:
            if profile not in self.config.get("profiles", {}):
                continue
            slot = get_slot_at(profile, datetime.now())
            self._apply_pihole_mode(profile, slot["mode"])
            db.log_event(profile, "info", "",
                         f"Dérogation temporaire terminée — retour en mode {slot['mode']}",
                         message_key="event.temp_override_ended",
                         params={"mode": slot["mode"]})
            print(f"[Monitor] ⏱ Dérogation expirée : {profile} → {slot['mode']}")

        # 2) Domaines débloqués temporairement : une seule resynchronisation suffit,
        #    quel que soit le nombre de domaines échus.
        expired = db.pop_expired_domain_unblocks()
        if expired:
            try:
                from claude_agent import _sync_pihole_blacklists
                _sync_pihole_blacklists(self.config)
                for row in expired:
                    db.log_event(row["profile"] or "global", "info", row["domain"],
                                 "Autorisation temporaire expirée — domaine rebloqué",
                                 message_key="event.domain_reblocked")
                print(f"[Monitor] ⏱ {len(expired)} domaine(s) rebloqué(s)")
            except Exception as e:
                print(f"[Monitor] Erreur re-blocage des domaines expirés : {e}")

    def _check_dns_bypass(self, by_ip: dict):
        """Détecte qu'un appareil D'ENFANT est présent sur le réseau sans jamais résoudre
        de nom via Protectado — donc qu'il utilise un autre résolveur.

        C'est la seule détection de contournement possible en dns_only : le Pi n'étant pas
        routeur, on ne voit ni le trafic ni les destinations. Le scan ARP constate la
        présence physique ; le silence DNS prolongé d'un appareil pourtant présent EST le
        signal.

        Sans objet en gateway, où le port 53 est forcé vers Pi-hole.

        Deux garde-fous contre les faux positifs, tirés de la conception de la détection
        de contournement :
          - SEULS les appareils rattachés à un profil enfant sont examinés. Le routeur de
            la box, une imprimante ou un objet connecté ont toutes les raisons d'utiliser
            un autre résolveur, et ne regardent personne ;
          - le silence doit DURER. Un appareil en veille ne résout rien non plus : c'est
            le cas nominal, pas une anomalie. Seule la persistance sur plusieurs heures,
            alors que l'appareil est vu sur le réseau, distingue les deux.
        """
        from access_control import enforcement_mode
        if enforcement_mode(self.config) != "dns_only":
            return

        # 1) Redemander un inventaire ARP toutes les ~10 minutes (opération root).
        self._arp_cycle += 1
        if self._arp_cycle >= 10:
            self._arp_cycle = 0
            self._queue_action("scan_arp", {})

        # 2) Exploiter le dernier résultat disponible.
        try:
            with open(os.path.join(DATA_DIR, "arp_scan.json")) as f:
                scan = json.load(f)
        except (OSError, ValueError):
            return
        if scan.get("error") or not scan.get("devices"):
            return
        present = {d.get("ip") for d in scan["devices"] if d.get("ip")}

        # 3) Appareils d'enfants uniquement.
        watched = {}
        for pname, profile in (self.config.get("profiles") or {}).items():
            if profile.get("mode") == "monitoring":
                continue
            for dev in profile.get("devices") or []:
                if dev.get("ip"):
                    watched[dev["ip"]] = pname

        now = datetime.now()
        for ip, pname in watched.items():
            if ip not in present:
                self._bypass_silence.pop(ip, None)      # absent du réseau : rien à dire
                continue
            if by_ip.get(ip):
                self._bypass_silence.pop(ip, None)      # il résout : tout va bien
                continue
            since = self._bypass_silence.setdefault(ip, now)
            silent_hours = (now - since).total_seconds() / 3600
            if silent_hours < BYPASS_SILENCE_HOURS:
                continue
            today = now.date().isoformat()
            if self._bypass_alerted.get(ip) == today:
                continue
            self._bypass_alerted[ip] = today
            db.log_event(pname, "warning", ip,
                         f"Contournement DNS probable : cet appareil est présent sur le "
                         f"réseau depuis plus de {BYPASS_SILENCE_HOURS} h sans jamais "
                         f"passer par Protectado pour résoudre les noms de sites — le "
                         f"filtrage ne s'applique pas à lui",
                         message_key="event.dns_bypass_suspected",
                         params={"h": BYPASS_SILENCE_HOURS})
            print(f"[Monitor] ⚠ Contournement DNS probable : {ip} ({pname})")

    def _check_bypass_attempts(self):
        """Signale au parent les contournements du filtrage REFUSÉS par la passerelle.

        Le runner publie, par appareil et par type, le nombre de paquets rejetés par
        PROTECTADO_BYPASS (data/bypass_counters.json). Seule une HAUSSE est une
        tentative :
          - première lecture : ce qui est déjà compté a pu être signalé avant un
            redémarrage du moniteur, on ne fait que noter ;
          - génération changée : la chaîne a été reconstruite, ses compteurs sont
            repartis de zéro, la référence redevient zéro ;
          - baisse dans une même génération : rien à signaler.
        Un signalement par appareil, par type et par jour.

        Ton factuel : une application peut tenter ces voies d'elle-même, sans que
        l'enfant y soit pour quelque chose.
        """
        from access_control import enforcement_mode
        if enforcement_mode(self.config) != "gateway":
            return
        try:
            with open(os.path.join(DATA_DIR, "bypass_counters.json")) as f:
                publie = json.load(f)
        except (OSError, ValueError):
            return
        generation = publie.get("generation")
        premiere = self._bypass_generation is None
        nouvelle_generation = not premiere and generation != self._bypass_generation
        self._bypass_generation = generation

        profil_de = {}
        for pname, profile in (self.config.get("profiles") or {}).items():
            for dev in (profile or {}).get("devices") or []:
                if dev.get("ip"):
                    profil_de[dev["ip"]] = pname

        aujourdhui = datetime.now().date().isoformat()
        for ip, par_type in (publie.get("counters") or {}).items():
            for type_, compte in (par_type or {}).items():
                cle = (ip, type_)
                # Dans une génération connue, toute règle est partie de zéro.
                avant = 0 if nouvelle_generation else self._bypass_counts.get(cle, 0)
                self._bypass_counts[cle] = compte
                if premiere or compte <= avant:
                    continue
                if self._bypass_attempt_alerted.get(cle) == aujourdhui:
                    continue
                self._bypass_attempt_alerted[cle] = aujourdhui
                db.log_event(profil_de.get(ip, "global"), "info", ip,
                             f"Tentative de contournement du filtrage refusée ({type_}) "
                             f"depuis {ip}",
                             message_key=f"event.bypass_attempt_{type_}",
                             params={"device": ip})
                print(f"[Monitor] Contournement refusé : {ip} ({type_})")

    def _note_dns(self, by_ip_new: dict):
        """Retient, par appareil, les noms demandés au boîtier à ce cycle, sur la fenêtre."""
        maintenant = time.time()
        for ip, domaines in (by_ip_new or {}).items():
            self._dns_history.setdefault(ip, []).append((maintenant, set(domaines)))
        for ip in list(self._dns_history):
            self._dns_history[ip] = [(t, n) for t, n in self._dns_history[ip]
                                     if maintenant - t <= VPN_WINDOW_SEC]
            if not self._dns_history[ip]:
                del self._dns_history[ip]

    def _record_traffic(self):
        """Ajoute au volume du jour de chaque enfant les relevés de flux pas encore comptés.

        Le runner publie une fenêtre glissante de 15 min, relue à chaque cycle : le dernier
        relevé compté est retenu EN BASE, sans quoi un redémarrage de l'agent recompterait
        la fenêtre. Un relevé se compte au jour de son horodatage. Un appareil sans enfant
        n'est pas compté.
        """
        try:
            with open(os.path.join(DATA_DIR, "flow_stats.json")) as f:
                releves = json.load(f).get("samples") or {}
        except (OSError, ValueError):
            return
        try:
            deja = float(db.get_meta("traffic_last_t", "0") or 0)
        except ValueError:
            deja = 0.0
        profil_de = {dev.get("ip"): pname
                     for pname, profile in (self.config.get("profiles") or {}).items()
                     if (profile or {}).get("mode") != "monitoring"
                     for dev in (profile or {}).get("devices") or [] if dev.get("ip")}
        volumes: dict = {}
        dernier = deja
        for ip, echantillons in releves.items():
            for e in echantillons or []:
                t = float(e.get("t", 0))
                if t <= deja:
                    continue
                dernier = max(dernier, t)
                profil = profil_de.get(ip)
                if not profil:
                    continue
                jour = datetime.fromtimestamp(t).date().isoformat()
                volumes[(jour, profil)] = volumes.get((jour, profil), 0) + int(e.get("bytes", 0))
        db.add_daily_traffic(volumes)
        if dernier > deja:
            db.set_meta("traffic_last_t", repr(dernier))

    def _check_vpn_suspects(self):
        """Signale un tunnel probable (VPN), à partir des relevés de flux du runner, et
        suit sa SESSION : début, dernière activité, fin.

        Signature d'ouverture, sur un quart d'heure : un volume réel (au-dessus d'une
        veille), concentré sur UNE adresse, pendant l'essentiel de la fenêtre, alors que
        l'appareil ne demande presque aucun nom au boîtier (ses résolutions passent dans le
        tunnel). Chaque critère écarte un faux positif connu : la veille (volume), la
        navigation (concentration), la vidéo en continu (elle résout des noms), la rafale
        (durée).

        Une session ouverte se prolonge tant que des relevés « tunnel » vers la même
        adresse continuent, et se ferme après VPN_END_GAP_SEC sans relevé. Un événement
        au journal par session. Seuils non calibrés (cf. VPN_*).
        """
        from access_control import enforcement_mode
        if enforcement_mode(self.config) != "gateway":
            return
        try:
            with open(os.path.join(DATA_DIR, "flow_stats.json")) as f:
                releves = json.load(f).get("samples") or {}
        except (OSError, ValueError):
            return
        maintenant = time.time()
        iso = lambda t: datetime.fromtimestamp(t).isoformat(timespec="seconds")
        profil_de = {dev.get("ip"): pname
                     for pname, profile in (self.config.get("profiles") or {}).items()
                     for dev in (profile or {}).get("devices") or [] if dev.get("ip")}
        ouvertes = {s["ip"]: s for s in db.get_open_vpn_sessions()}
        for ip, echantillons in releves.items():
            recents = [e for e in echantillons or []
                       if maintenant - float(e.get("t", 0)) <= VPN_WINDOW_SEC]
            tunnel = [e for e in recents if e.get("bytes", 0) >= VPN_ACTIVE_BYTES
                      and e.get("top_share", 0) >= VPN_TOP_SHARE]
            session = ouvertes.get(ip)
            if session:
                neufs = [e for e in tunnel if e.get("top_dst") == session["dst"]
                         and iso(float(e["t"])) > session["last_seen"]]
                if neufs:
                    db.touch_vpn_session(session["id"], iso(max(float(e["t"]) for e in neufs)),
                                         sum(int(e.get("bytes", 0)) for e in neufs))
                continue
            if not tunnel:
                continue
            destinations = [e.get("top_dst") for e in tunnel]
            cible = max(set(destinations), key=destinations.count)
            if destinations.count(cible) < VPN_MIN_SAMPLES:
                continue
            noms = set().union(*[n for t, n in self._dns_history.get(ip, [])
                                 if maintenant - t <= VPN_WINDOW_SEC])
            if len(noms) > VPN_MAX_DNS_DOMAINS:
                continue
            vers_cible = [e for e in tunnel if e.get("top_dst") == cible]
            instants = [float(e["t"]) for e in vers_cible]
            profil = profil_de.get(ip, "global")
            db.open_vpn_session(profil, ip, cible, iso(min(instants)), iso(max(instants)),
                                sum(int(e.get("bytes", 0)) for e in vers_cible))
            db.log_event(profil, "warning", ip,
                         f"Tunnel probable depuis {ip} : trafic concentré vers {cible} "
                         f"sans requête DNS depuis un quart d'heure",
                         message_key="event.vpn_suspected", params={"device": ip})
            print(f"[Monitor] Tunnel probable : {ip} → {cible}")
        # Fin : plus de relevé « tunnel » depuis VPN_END_GAP_SEC, y compris pour un
        # appareil parti du réseau (il n'a plus de relevé du tout).
        for session in db.get_open_vpn_sessions():
            dernier = datetime.fromisoformat(session["last_seen"]).timestamp()
            if maintenant - dernier > VPN_END_GAP_SEC:
                db.close_vpn_session(session["id"], session["last_seen"])
                print(f"[Monitor] Fin du tunnel probable : {session['ip']}")

    def _classify_blocked_domain(self, domain: str):
        """Lance l'identification Cloudflare d'un domaine bloqué, HORS du fil du cycle.

        TROIS GARDE-FOUS, dans l'ordre où ils comptent :

        1. JAMAIS DANS LE CYCLE. La résolution part dans un fil séparé. Le cycle tourne
           toutes les 60 s et fait tout le travail de surveillance : une résolution lente,
           ou un résolveur injoignable avec son délai d'attente, retarderait le blocage
           des autres enfants pour identifier un domaine.
        2. UNE FOIS PAR HEURE ET PAR DOMAINE. Une application qui réessaie en boucle
           produit un événement de blocage par fenêtre de 10 min ; sans cette limite, elle
           produirait autant de résolutions.
        3. LE MODÈLE RESTE GROUPÉ. On n'appelle QUE Cloudflare ici, jamais OpenRouter : la
           dépense en appels au modèle ne bouge pas d'un centime.
        """
        now = datetime.now()
        derniere = self._cf_asked.get(domain)
        if derniere and (now - derniere).total_seconds() < CF_IMMEDIATE_WINDOW_SEC:
            return
        self._cf_asked[domain] = now

        def _interroger():
            try:
                categorie = classifier.classify_now(domain)
                if categorie:
                    print(f"[Monitor] {domain} identifié à chaud : {categorie}")
            except Exception as e:
                # Une identification manquée n'est pas un incident : la passe groupée
                # repassera. On ne veut surtout pas qu'elle fasse du bruit.
                print(f"[Monitor] identification à chaud de {domain} impossible : {e}")

        threading.Thread(target=_interroger, daemon=True,
                         name=f"cf-{domain[:24]}").start()

    def _maybe_classify_cloudflare(self):
        """Classification Cloudflare toutes les 10 minutes (cycle 60s × 10 = 10 min)."""
        self._cloudflare_cycle += 1
        if self._cloudflare_cycle < 10:
            return
        self._cloudflare_cycle = 0
        try:
            n = classifier.classify_with_cloudflare(limit=100)
            if n:
                print(f"[Monitor] Cloudflare : {n} nouveaux domaines catégorisés")
        except Exception as e:
            print(f"[Monitor] Erreur classification Cloudflare : {e}")

    def _check_enforcement_rearm(self):
        """Repousse l'état d'accès de tous les profils quand l'effecteur vient de s'activer.

        Le mode n'est appliqué qu'au CHANGEMENT de plage (cf. _check_schedule). Si la
        couche paquet s'arme après coup — au boot, le service privilégié peut démarrer
        avant que la radio ne soit en mode AP — les règles du moment n'ont jamais été
        posées, et un enfant déjà en plage « bloqué » resterait ouvert jusqu'à la plage
        suivante, donc potentiellement des heures.

        Oublier le dernier mode connu suffit : le cycle en cours le redétecte comme un
        changement et réapplique tout, Pi-hole comme iptables.
        """
        try:
            with open(os.path.join(DATA_DIR, "gateway_status.json")) as f:
                gw = json.load(f)
        except (OSError, ValueError):
            return
        stamp = gw.get("updated_at") or ""
        if stamp == self._enforcement_stamp:
            return
        first = self._enforcement_stamp is None
        self._enforcement_stamp = stamp
        # « degraded » est un effecteur ARMÉ auquel il manque un blocage secondaire : les
        # règles d'accès doivent y être poussées comme pour « ok ».
        if first or gw.get("state") not in ("ok", "degraded"):
            return
        self._last_slot.clear()
        print("[Monitor] Effecteur d'accès (re)devenu actif — réapplication des règles.")

    def _maybe_reload_services(self):
        """Recharge le catalogue des services s'il a changé sur disque.

        Deux cas, et aucun ne justifie un redémarrage du service : une publication qui ne
        touche que catalog/ (l'updater l'applique alors sans toucher aux dépendances ni
        aux services, cf. bootstrap/protectado-update.sh), et un appoint local que le
        parent vient d'écrire. Le catalogue est ainsi la seule partie du produit qui se
        met à jour sans interruption, ce qui est exactement ce qu'on veut d'une liste de
        domaines : elle change souvent, et pour des raisons qui ne sont pas du code.

        Le coût est de deux os.stat par cycle. Un échec ne remet rien en cause : l'agent
        garde le catalogue qu'il a en mémoire.
        """
        stamp = services.stamp()
        if stamp == self._services_stamp:
            return
        premier = self._services_stamp is None
        self._services_stamp = stamp
        if premier:
            return
        try:
            combien = classifier.reload_services()
        except Exception as e:
            # Volontairement pas de réessai immédiat : l'empreinte est déjà enregistrée,
            # donc on ne rejoue pas l'échec à chaque cycle. Corriger le fichier change
            # son mtime, ce qui relance la tentative.
            print(f"[Monitor] Catalogue des services refusé — l'ancien reste en place : {e}")
            return
        print(f"[Monitor] Catalogue des services rechargé — {combien} services")

    def _purge_memory(self, now: datetime):
        """Oublie ce qui ne peut plus servir : marqueurs d'événements inhabituels des jours
        passés, états de domaines bloqués muets depuis BLOCK_STATE_TTL_SEC, demandes
        Cloudflare sorties de leur fenêtre. Sans cela ces mémoires ne perdaient jamais
        une entrée sur un boîtier qui tourne des mois."""
        aujourd_hui = now.date().isoformat()
        self._unusual_seen = {m for m in self._unusual_seen if m[2] == aujourd_hui}
        self._block_state = {
            k: v for k, v in self._block_state.items()
            if v.get("last_seen") and (now - v["last_seen"]).total_seconds() < BLOCK_STATE_TTL_SEC}
        self._cf_asked = {
            d: t for d, t in self._cf_asked.items()
            if (now - t).total_seconds() < CF_IMMEDIATE_WINDOW_SEC}

    def run_cycle(self):
        self._purge_memory(datetime.now())
        self._maybe_reload_services()
        self._check_enforcement_rearm()
        # AVANT toute décision : savoir qui est qui. Les plannings, les groupes Pi-hole et
        # la comptabilisation travaillent tous sur les listes d'appareils des profils.
        self._sync_identity()
        self._check_expired_overrides()
        self._check_expired_temp_state()
        # Un SEUL inventaire réseau par cycle (appel FTL coûteux sur un Pi) — partagé
        # entre le suivi des nouveaux appareils et la liste des IP actives.
        devices = self.scanner.scan()

        self._maybe_classify_cloudflare()
        queries   = self.pihole.get_recent_queries(minutes=5)
        by_ip     = self.pihole.queries_by_client(queries)
        active_ips = {d["ip"] for d in devices}

        # DEUX vues volontairement différentes de la même lecture :
        #  - `by_ip` garde la fenêtre COMPLÈTE de 5 min, sur laquelle sont calibrés les
        #    seuils de détection (_check_blocked_domains, _check_unusual_patterns) ;
        #  - `by_ip_new` ne contient que les requêtes jamais comptabilisées, pour que les
        #    compteurs présentés au parent ne soient pas multipliés par le recouvrement
        #    des fenêtres. C'est aussi la vue de _check_unusual_patterns, qui recevait la
        #    fenêtre pleine et signalait donc la même rafale à cinq cycles d'affilée.
        by_ip_new = self.pihole.queries_by_client(self._uncounted(queries))
        self._check_dns_bypass(by_ip)
        self._check_bypass_attempts()
        self._note_dns(by_ip_new)
        self._check_vpn_suspects()
        self._record_traffic()

        # Prélectures pour la détection d'anomalies : UNE fois par cycle, pas une fois par
        # profil. Le catalogue est lu pour les seuls domaines vus dans ce cycle, et non
        # dans son intégralité comme le faisait get_all_domains().
        domaines_du_cycle = {self._root_domain(d)
                             for domains in by_ip_new.values() for d in domains}
        known_categories = classifier.categories_of(domaines_du_cycle)
        # Historique par (profil, domaine) : la clé porte le profil, donc une seule requête
        # ne mélange RIEN entre les enfants. Elle évite seulement N allers-retours.
        usage_history = db.get_usage_history(days=UNUSUAL_HISTORY_DAYS)
        self._record_usage(by_ip_new)

        for pname, profile in self.config["profiles"].items():
            # Ignorer si aucun appareil configuré. On OUBLIE alors les adresses appliquées :
            # le runner retire seul l'autorisation d'une station qui se réassocie (elle
            # passe quelques secondes « non authentifiée »). Si le moniteur gardait
            # l'ancienne liste, le retour de l'appareil sur la même adresse ne serait pas
            # vu comme un changement, et l'autorisation ne serait jamais rétablie : constaté
            # sur boîtier, un téléphone bloqué des heures en pleine plage ouverte.
            if not profile.get("devices"):
                self._last_ips.pop(pname, None)
                continue
            # Règles déterministes — aucun appel IA. Chaque profil est isolé : une erreur
            # sur l'un ne doit pas laisser les autres sur leur état précédent.
            try:
                self._check_schedule(pname, profile, active_ips)
                self._check_blocked_domains(pname, profile, by_ip)
                self._check_unusual_patterns(pname, profile, by_ip_new,
                                             known_categories, usage_history)
            except Exception as e:
                print(f"[Monitor] Erreur profil {pname} : {e!r}")
        self.last_cycle_ok = time.time()

    def _record_usage(self, by_ip_new: dict):
        """Comptabilise les requêtes du cycle (usage du jour par profil, catalogue des
        domaines avec le mode courant), regroupées par (profil, domaine racine) et écrites
        en une transaction chacune (cf. database.increment_usage_bulk)."""
        usage, acces = {}, {}
        for pname, profile in self.config["profiles"].items():
            if not profile.get("devices"):
                continue
            current_mode = get_slot_at(pname, datetime.now())["mode"]
            for device in profile["devices"]:
                for domain in by_ip_new.get(device["ip"], []):
                    root = self._root_domain(domain)
                    if root:
                        usage[(pname, root)] = usage.get((pname, root), 0) + 1
                        # Le dernier profil traité donne le mode, comme en unitaire.
                        acces[root] = (acces.get(root, (0, None))[0] + 1, current_mode)
        db.increment_usage_bulk(usage)
        classifier.record_domains_bulk(acces)

    def run_once(self):
        """Pour tests."""
        self.run_cycle()

    # ------------------------------------------------------------------ #
    #  Boucle                                                             #
    # ------------------------------------------------------------------ #

    def notify(self):
        """Réveille le cycle immédiatement (appel externe après écriture en DB)."""
        self._wakeup.set()

    def start(self, interval: int = 60):
        global _global_monitor
        _global_monitor = self
        self.interval = interval
        self._running = True
        print(f"[Monitor] Démarré — cycle toutes les {interval}s (sans IA)")

        def loop():
            while self._running:
                self._wakeup.clear()
                try:
                    self.run_cycle()
                except Exception as e:
                    print(f"[Monitor] Erreur cycle : {e}")
                self._wakeup.wait(timeout=interval)

        thread = threading.Thread(target=loop, daemon=True)
        thread.start()

        # Un enfant qui rejoint le Wi-Fi ne doit pas attendre le prochain cycle pour
        # que ses règles s'appliquent. Ce guetteur ne fait rien lui-même : il réveille
        # le cycle dès que le runner republie l'identité des stations.
        def watch_identity():
            last = None
            while self._running:
                try:
                    stamp = os.path.getmtime(self._identity_path)
                except OSError:
                    stamp = None
                if stamp is not None and stamp != last:
                    if last is not None:
                        self.notify()
                    last = stamp
                time.sleep(IDENTITY_WATCH_SECONDS)

        threading.Thread(target=watch_identity, daemon=True).start()

    def stop(self):
        self._running = False
