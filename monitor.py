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
from datetime import datetime

from paths import CONFIG_PATH, ACTION_QUEUE_DIR, DATA_DIR
from pihole_api import PiHoleAPI
from scheduler import get_slot_at, get_all_temp_overrides
import domain_classifier as classifier
from arp_scanner import ARPScanner
import database as db
from access_control import apply_device_access
import device_grace

# Seuil pour considérer un volume de requêtes comme "inhabituel"
UNUSUAL_QUERY_THRESHOLD = 50  # requêtes vers un domaine inconnu en 5 min
ESCALATE_AFTER = 3            # événements inhabituels avant escalade Claude

# Déduplication des domaines bloqués
BLOCK_WINDOW_SEC     = 600    # 10 min — une seule alerte par fenêtre
KEEPALIVE_MAX_HITS   = 4      # ≤ 4 hits/cycle (fenêtre Pi-hole 5 min) → keepalive
KEEPALIVE_MIN_CYCLES = 3      # 3 cycles consécutifs bas → silence total

# Contournement DNS (dns_only) : durée de silence AVANT d'alerter. Un appareil en veille ne
# résout rien non plus — seule la persistance distingue le contournement de l'inactivité.
BYPASS_SILENCE_HOURS = 4


_global_monitor: "ProtectadoMonitor | None" = None


def notify_monitor():
    """Réveille le monitor global immédiatement (après une écriture en DB)."""
    if _global_monitor is not None:
        _global_monitor.notify()


class ProtectadoMonitor:
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
        self._block_state: dict = {}   # (profile, domain) → état déduplication
        self._last_slot: dict = {}     # profile → dernier mode détecté
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
        self._bypass_silence: dict = {}  # ip → début du silence DNS constaté

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
        ts = datetime.now().strftime("%Y%m%d%H%M%S%f")
        path = os.path.join(ACTION_QUEUE_DIR, f"action-{ts}.json")
        payload = {"action": action, "args": args, "queued_at": datetime.now().isoformat()}
        with open(path, "w") as f:
            json.dump(payload, f)
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

        # Détecter un changement de slot
        prev_mode = self._last_slot.get(profile_key)
        if prev_mode != mode:
            print(f"[Monitor] {profile_key} : {prev_mode} → {mode} ({slot['slot_start']}-{slot['slot_end']})")
            self._last_slot[profile_key] = mode
            db.log_event(profile_key, "info", "",
                         f"Changement de plage : {mode} jusqu\'à {slot['slot_end']}",
                         message_key="event.mode_change",
                         params={"mode": mode, "until": slot["slot_end"]})

            # Appliquer via Pi-hole selon le mode
            self._apply_pihole_mode(profile_key, mode)

        # En "dns_only" (défaut) : le changement de mode est entièrement géré par Pi-hole
        # via _apply_pihole_mode, sans iptables (le Pi n'est pas routeur). En "gateway",
        # l'effet FORWARD est appliqué côté runner (root), pas ici — voir access_control /
        # action_runner. Aucun présupposé INCONDITIONNEL « iptables inutile » à ce niveau.

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
        blacklist = classifier.get_active_blacklist(mode)
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
        active_blacklist = set(dc.get_active_blacklist(current_mode))
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

    def _check_unusual_patterns(self, profile_key: str, profile: dict,
                                 queries_by_ip: dict):
        """
        Détecte des patterns non couverts par les règles.
        Accumule dans un buffer — escalade vers Claude si seuil atteint.
        """
        # Domaines connus = ceux déjà dans la DB
        import domain_classifier as dc
        known_domains = {d["domain"] for d in dc.get_all_domains()}

        for device in profile.get("devices", []):
            ip = device["ip"]
            domain_counts = {}
            for domain in queries_by_ip.get(ip, []):
                root = self._root_domain(domain)
                domain_counts[root] = domain_counts.get(root, 0) + 1

            for domain, count in domain_counts.items():
                if domain not in known_domains and count >= UNUSUAL_QUERY_THRESHOLD:
                    event = {
                        "profile": profile_key,
                        "domain": domain,
                        "count": count,
                        "timestamp": datetime.now().isoformat()
                    }
                    self._unusual_events.append(event)
                    print(f"[Monitor] Pattern inhabituel : {domain} × {count} pour {profile_key}")

        # Escalade vers Claude si assez d'événements inhabituels
        if len(self._unusual_events) >= ESCALATE_AFTER:
            self._escalate_to_claude()

    def _escalate_to_claude(self):
        """Appelle Claude uniquement pour les patterns inhabituels."""
        try:
            from claude_agent import analyze_unusual_patterns
            analyze_unusual_patterns(self._unusual_events)
        except Exception as e:
            print(f"[Monitor] Erreur escalade Claude : {e}")
        finally:
            self._unusual_events = []

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

    # Suffixes publics à deux niveaux les plus courants sur les marchés visés
    # (fr/en/es/pt) et chez les grands services. Sans cette liste, « bbc.co.uk » était
    # réduit à « co.uk » et « globo.com.br » à « com.br » : catégorisation fausse, et
    # surtout blocage d'un TLD ENTIER si un tel « domaine racine » atterrissait en
    # blacklist (le motif Pi-hole devient `(.*\.)?com\.br$`).
    # Liste embarquée volontairement : pas d'appel réseau, pas de dépendance.
    _MULTI_LEVEL_SUFFIXES = frozenset("""
        co.uk org.uk gov.uk ac.uk net.uk sch.uk me.uk ltd.uk plc.uk
        com.br net.br org.br gov.br edu.br
        com.ar com.mx com.co com.pe com.uy com.ve com.ec com.bo com.py
        com.es org.es gob.es edu.es
        com.pt org.pt gov.pt edu.pt
        com.au net.au org.au gov.au edu.au id.au
        co.nz net.nz org.nz govt.nz
        co.za org.za
        co.jp or.jp ne.jp ac.jp go.jp
        co.kr or.kr
        com.cn net.cn org.cn gov.cn edu.cn
        com.tr com.tw com.hk com.sg com.my com.ph com.vn com.ua com.pl
        co.in net.in org.in gov.in
        com.ru net.ru org.ru
        co.il org.il
    """.split())

    def _root_domain(self, domain: str) -> str:
        """Domaine « racine » facturable : deux labels, ou trois si le suffixe est composé.

        bbc.co.uk → bbc.co.uk (et non co.uk) ; www.globo.com.br → globo.com.br.
        """
        parts = domain.rstrip(".").lower().split(".")
        if len(parts) < 2:
            return domain
        if len(parts) >= 3 and ".".join(parts[-2:]) in self._MULTI_LEVEL_SUFFIXES:
            return ".".join(parts[-3:])
        return ".".join(parts[-2:])

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

    def _check_new_device_grace(self, devices: list[dict]):
        """Signale les appareils du réseau ENFANTS dont le délai de grâce est écoulé.

        `devices` : inventaire réseau du cycle (ARPScanner.scan()), déjà en main —
        on ne relance pas d'appel FTL ici.

        Un appareil qui rejoint le Wi-Fi du boîtier a un accès libre pendant
        network.new_device_grace_hours (24 h par défaut) ; passé ce délai, s'il n'est
        rattaché à aucun profil ni ignoré, le parent est averti. AUCUN blocage à ce
        stade — la mise en quarantaine viendra dans un second temps.

        Ne fait rien en dns_only (le Pi n'est pas routeur) ni pour les appareils restés
        sur le Wi-Fi de la box : seuls les clients de l'AP enfants sont concernés.
        """
        if not device_grace.is_active(self.config):
            return
        kids = [d for d in devices if device_grace.is_kids_ip(d.get("ip", ""))]
        pending = device_grace.unassigned(kids, self.config)
        device_grace.annotate(pending, self.config)

        for d in device_grace.newly_expired(pending):
            label = d.get("hostname") or d.get("mac") or d["ip"]
            db.log_event("global", "warning", d["ip"],
                         f"Attention : {label} circule en accès libre sur le réseau "
                         f"enfants depuis plus de "
                         f"{device_grace.grace_hours(self.config):.0f} h — aucune règle "
                         f"ne s'applique tant qu'il n'est pas rattaché à un profil "
                         f"(onglet Appareils)",
                         message_key="event.device_free_access",
                         params={"label": label,
                                 "h": round(device_grace.grace_hours(self.config))})
            device_grace.mark_notified(d)
            print(f"[Monitor] ⚠ Accès libre sur le réseau enfants : {label} ({d['ip']})")

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

    def run_cycle(self):
        self._check_expired_overrides()
        self._check_expired_temp_state()
        # Un SEUL inventaire réseau par cycle (appel FTL coûteux sur un Pi) — partagé
        # entre le suivi des nouveaux appareils et la liste des IP actives.
        devices = self.scanner.scan()
        self._check_new_device_grace(devices)

        self._maybe_classify_cloudflare()
        queries   = self.pihole.get_recent_queries(minutes=5)
        by_ip     = self.pihole.queries_by_client(queries)
        active_ips = {d["ip"] for d in devices}

        # DEUX vues volontairement différentes de la même lecture :
        #  - `by_ip` garde la fenêtre COMPLÈTE de 5 min, sur laquelle sont calibrés les
        #    seuils de détection (_check_blocked_domains, _check_unusual_patterns) ;
        #  - `by_ip_new` ne contient que les requêtes jamais comptabilisées, pour que les
        #    compteurs présentés au parent ne soient pas multipliés par le recouvrement
        #    des fenêtres.
        by_ip_new = self.pihole.queries_by_client(self._uncounted(queries))
        self._check_dns_bypass(by_ip)

        for pname, profile in self.config["profiles"].items():
            # Ignorer si aucun appareil configuré
            if not profile.get("devices"):
                continue
            # Incrémenter l'usage DNS + enregistrer domaine avec mode courant
            current_mode = get_slot_at(pname, datetime.now())["mode"]
            for device in profile.get("devices", []):
                for domain in by_ip_new.get(device["ip"], []):
                    root = self._root_domain(domain)
                    if root:
                        db.increment_usage(pname, root)
                        classifier.record_domain(root, current_mode)

            # Règles déterministes — aucun appel IA
            self._check_schedule(pname, profile, active_ips)
            self._check_blocked_domains(pname, profile, by_ip)
            self._check_unusual_patterns(pname, profile, by_ip)

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

    def stop(self):
        self._running = False
