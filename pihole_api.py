# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Arnaud Ortais
# Dual-licensed: AGPL-3.0 (open source) or Commercial License — see LICENSE and LICENSE-COMMERCIAL.
"""
pihole_api.py — Wrapper Pi-hole v6 REST API

Gère :
  - Authentification par session
  - Groupes par profil/mode
  - Assignation clients aux groupes
  - Listes de blocage par groupe
  - Changement de mode (slot)
"""

import re
import requests
from urllib.parse import quote as urlquote
from datetime import datetime, timedelta


class PiHoleAPI:
    def __init__(self, host: str, password: str):
        self.host = host.rstrip("/")
        self.password = password
        self._sid = None
        self._sid_expires = None
        self._group_cache = {}   # name → id
        # Dernière erreur d'API, exposée à l'interface. Sans ça, une authentification
        # refusée se traduisait par des listes VIDES indiscernables d'un réseau sans
        # appareil : la page Appareils restait blanche sans le moindre message.
        self.last_error = ""

    # ------------------------------------------------------------------ #
    #  Authentification                                                   #
    # ------------------------------------------------------------------ #

    def _authenticate(self) -> bool:
        try:
            r = requests.post(
                f"{self.host}/api/auth",
                json={"password": self.password},
                timeout=10
            )
            data = r.json()
            self._sid = data.get("session", {}).get("sid")
            validity = data.get("session", {}).get("validity", 1800)
            self._sid_expires = datetime.now() + timedelta(seconds=validity - 60)
            if self._sid is None:
                # Cas le plus fréquent : mot de passe de config.json désynchronisé du
                # mot de passe réel de Pi-hole (« pihole setpassword » ayant échoué).
                detail = (data.get("session", {}).get("message")
                          or data.get("error", {}).get("message")
                          or f"HTTP {r.status_code}")
                self.last_error = f"authentification Pi-hole refusée ({detail})"
                print(f"[PiHole] {self.last_error}")
                return False
            self.last_error = ""
            return True
        except Exception as e:
            self.last_error = f"Pi-hole injoignable sur {self.host} ({e.__class__.__name__})"
            print(f"[PiHole] Erreur auth : {e}")
            return False

    def _logout(self):
        """Ferme proprement la session Pi-hole."""
        if self._sid:
            try:
                requests.delete(
                    f"{self.host}/api/auth",
                    headers={"X-FTL-SID": self._sid},
                    timeout=5
                )
            except Exception:
                pass
            self._sid = None
            self._sid_expires = None

    def __del__(self):
        """Logout automatique à la destruction de l'objet."""
        self._logout()


    def _get_sid(self) -> str | None:
        if not self._sid or datetime.now() >= self._sid_expires:
            self._authenticate()
        return self._sid

    def _headers(self) -> dict:
        sid = self._get_sid()
        return {"X-FTL-SID": sid} if sid else {}

    def _get(self, endpoint: str, params: dict = None) -> dict:
        if params is None:
            params = {}
        try:
            r = requests.get(
                f"{self.host}/api{endpoint}",
                headers=self._headers(),
                params=params,
                timeout=10
            )
            if r.status_code == 401:
                self._sid = None
                r = requests.get(
                    f"{self.host}/api{endpoint}",
                    headers=self._headers(),
                    params=params,
                    timeout=10
                )
            if r.status_code >= 400:
                self.last_error = (self.last_error
                                   or f"GET {endpoint} → HTTP {r.status_code}")
                print(f"[PiHole] GET {endpoint} → HTTP {r.status_code}")
                return {}
            self.last_error = ""
            return r.json() if r.content else {}
        except Exception as e:
            self.last_error = f"GET {endpoint} : {e.__class__.__name__}"
            print(f"[PiHole] Erreur GET {endpoint} : {e}")
            return {}

    def _post(self, endpoint: str, payload: dict = None) -> dict:
        if payload is None:
            payload = {}
        try:
            r = requests.post(
                f"{self.host}/api{endpoint}",
                headers=self._headers(),
                json=payload,
                timeout=10
            )
            return r.json() if r.content else {}
        except Exception as e:
            print(f"[PiHole] Erreur POST {endpoint} : {e}")
            return {}

    def _put(self, endpoint: str, payload: dict = None) -> dict:
        if payload is None:
            payload = {}
        try:
            r = requests.put(
                f"{self.host}/api{endpoint}",
                headers=self._headers(),
                json=payload,
                timeout=10
            )
            return r.json() if r.content else {}
        except Exception as e:
            print(f"[PiHole] Erreur PUT {endpoint} : {e}")
            return {}

    def _delete(self, endpoint: str) -> bool:
        try:
            r = requests.delete(
                f"{self.host}/api{endpoint}",
                headers=self._headers(),
                timeout=10
            )
            return r.status_code in (200, 204)
        except Exception as e:
            print(f"[PiHole] Erreur DELETE {endpoint} : {e}")
            return False

    # ------------------------------------------------------------------ #
    #  Groupes                                                            #
    # ------------------------------------------------------------------ #

    def get_groups(self) -> list:
        data = self._get("/groups")
        return data.get("groups", [])

    def get_group_id(self, name: str) -> int | None:
        if name in self._group_cache:
            return self._group_cache[name]
        for g in self.get_groups():
            self._group_cache[g["name"]] = g["id"]
        return self._group_cache.get(name)

    def create_group(self, name: str, description: str = "") -> int | None:
        existing_id = self.get_group_id(name)
        if existing_id is not None:
            return existing_id

        data = self._post("/groups", {
            "name": name,
            "comment": description or f"Protectado — {name}",
            "enabled": True
        })
        group = data.get("group", {})
        gid = group.get("id")
        if gid is not None:
            self._group_cache[name] = gid
            print(f"[PiHole] Groupe créé : {name} (id={gid})")
        return gid

    # ------------------------------------------------------------------ #
    #  Clients                                                            #
    # ------------------------------------------------------------------ #

    def get_clients(self) -> list:
        data = self._get("/clients")
        return data.get("clients", [])

    def get_network_devices(self) -> list[dict]:
        """Appareils réseau vus par Pi-hole FTL — IP, MAC, vendor, hostname."""
        data = self._get("/network/devices")
        devices = []
        for dev in data.get("devices", []):
            mac    = dev.get("hwaddr", "")
            vendor = dev.get("macVendor", "")
            for ip_entry in dev.get("ips", []):
                ip = ip_entry.get("ip", "")
                if ip:
                    devices.append({
                        "ip":       ip,
                        "mac":      mac,
                        "vendor":   vendor,
                        "hostname": ip_entry.get("name", ""),
                        "last_seen": ip_entry.get("lastSeen"),
                    })
        return devices

    def set_client_comment(self, ip: str, comment: str) -> bool:
        """Met à jour le nom/commentaire d'un client Pi-hole."""
        data = self._put(f"/clients/{urlquote(ip, safe='')}", {"comment": comment})
        return bool(data)

    def ensure_client_exists(self, ip: str, mac: str = "", hostname: str = "") -> bool:
        """Crée le client dans Pi-hole s'il n'existe pas encore (groupe par défaut)."""
        clients = self.get_clients()
        if any(c.get("client") == ip for c in clients):
            return True
        comment = hostname or mac or ip
        data = self._post("/clients", {
            "client":  ip,
            "comment": f"Protectado — {comment}",
            "groups":  [],
        })
        return "client" in data or "clients" in data

    def assign_client_to_group(self, ip: str, group_name: str) -> bool:
        """Assigne un appareil (IP) à un groupe Pi-hole."""
        group_id = self.get_group_id(group_name)
        if group_id is None:
            print(f"[PiHole] Groupe introuvable : {group_name}")
            return False

        # Vérifier si le client existe
        clients = self.get_clients()
        client = next((c for c in clients if c.get("client") == ip), None)

        if client:
            # Mettre à jour le groupe
            data = self._put(f"/clients/{ip}", {"groups": [group_id]})
        else:
            # Créer le client
            data = self._post("/clients", {
                "client": ip,
                "comment": f"Protectado — {ip}",
                "groups": [group_id]
            })

        success = "clients" in data or "client" in data
        if success:
            print(f"[PiHole] {ip} → groupe {group_name}")
        return success

    # ------------------------------------------------------------------ #
    #  Domaines                                                           #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _domain_to_pattern(domain: str) -> str:
        """Convertit un root domain en regex couvrant aussi tous ses sous-domaines."""
        return r"(.*\.)?" + re.escape(domain) + r"$"

    def remove_domain_from_list(self, domain: str) -> bool:
        pattern = self._domain_to_pattern(domain)
        return self._delete(f"/domains/deny/regex/{urlquote(pattern, safe='')}")

    def remove_domain_from_group(self, domain: str, group_id: int) -> bool:
        """
        Retire un domaine uniquement du groupe Pi-hole spécifié.
        Si d'autres groupes utilisent encore l'entrée, elle est conservée pour eux.
        Si plus aucun groupe n'est associé, l'entrée est supprimée.
        """
        pattern = self._domain_to_pattern(domain)
        existing = {d["domain"]: d for d in self.get_deny_domains()}
        entry = existing.get(pattern)
        if not entry:
            return True  # déjà absent

        current_groups = entry.get("groups", [])
        remaining = [g for g in current_groups if g != group_id]

        self._delete(f"/domains/deny/regex/{urlquote(pattern, safe='')}")
        if remaining:
            self._post("/domains/deny/regex", {
                "domain": pattern,
                "comment": entry.get("comment", ""),
                "enabled": True,
                "groups": remaining,
            })
        return True

    def get_deny_domains(self) -> list:
        data = self._get("/domains/deny/regex")
        return data.get("domains", [])

    # ------------------------------------------------------------------ #
    #  Setup initial                                                      #
    # ------------------------------------------------------------------ #

    def setup_profiles(self, profiles: dict) -> bool:
        """
        Initialise tous les groupes et listes pour chaque profil.
        Génère dynamiquement les groupes depuis la liste des profils.
        Idempotent — peut être appelé plusieurs fois sans dupliquer.
        """
        print("[PiHole] Initialisation des profils...")

        # 1. Créer les 3 groupes standard pour chaque profil non-monitoring
        all_group_ids = {}
        for profile_name, profile in profiles.items():
            if profile.get("mode") == "monitoring":
                continue
            for mode in ("blocked", "work", "permissive"):
                gname = f"{profile_name}-{mode}"
                gid = self.create_group(gname)
                if gid is not None:
                    all_group_ids[gname] = gid

        # Groupe adulte global — aucune liste de blocage, accès total
        self.create_group("adult-override", "Protectado — Mode adulte (accès total)")

        # 2. Bloquer tout le DNS pour les groupes blocked — idempotent
        existing_deny = self.get_deny_domains()
        wildcard_group_ids: set[int] = set()
        for d in existing_deny:
            if d["domain"] == ".*":
                wildcard_group_ids.update(d.get("groups", []))

        for profile_name, profile in profiles.items():
            if profile.get("mode") == "monitoring":
                continue
            blocked_gid = all_group_ids.get(f"{profile_name}-blocked")
            if blocked_gid and blocked_gid not in wildcard_group_ids:
                self._post("/domains/deny/regex", {
                    "domain": ".*",
                    "comment": f"protectado:blocked:{profile_name}",
                    "enabled": True,
                    "groups": [blocked_gid]
                })
                print(f"[PiHole] Wildcard DNS ajouté pour {profile_name}-blocked")

        # 3. Assigner les appareils à leur groupe initial (mode actif)
        from scheduler import get_slot_at
        for profile_name, profile in profiles.items():
            if profile.get("mode") == "monitoring":
                continue
            slot = get_slot_at(profile_name, datetime.now())
            active_group = f"{profile_name}-{slot['mode']}"
            for device in profile.get("devices", []):
                self.assign_client_to_group(device["ip"], active_group)

        print("[PiHole] Setup terminé ✓")
        return True

    # ------------------------------------------------------------------ #
    #  Changement de mode (slot)                                          #
    # ------------------------------------------------------------------ #

    def switch_profile_mode(self, profile_name: str, mode: str,
                             device_ips: list, blacklist: list = None) -> bool:
        """
        Point d'entrée principal appelé par action_runner.
        Bascule tous les appareils d'un profil vers le bon groupe.
        """
        target_group = f"{profile_name}-{mode}"
        group_id = self.get_group_id(target_group)

        if group_id is None:
            print(f"[PiHole] Groupe {target_group} introuvable — setup requis ?")
            return False

        if blacklist is not None:
            self._sync_blacklist(group_id, mode, blacklist)

        # Basculer chaque appareil
        success = True
        for ip in device_ips:
            ok = self.assign_client_to_group(ip, target_group)
            success = success and ok

        if success:
            print(f"[PiHole] {profile_name} → mode {mode} ({len(device_ips)} appareils)")
        return success

    def _sync_blacklist(self, group_id: int, mode: str, domains: list):
        r"""
        Synchronise la blacklist vers un groupe Pi-hole.
        - Ajoute les nouveaux domaines avec le groupe cible.
        - Met à jour les entrées existantes manquant le groupe cible.
        - Retire le groupe des entrées obsolètes (supprime si aucun groupe restant).
        Gère les doublons de pattern (même regex, groupes différents) par fusion.
        Les entrées sont taguées "protectado:{mode}" pour permettre cleanup/audit.
        """
        tag = f"protectado:{mode}"
        target_patterns = {self._domain_to_pattern(d) for d in domains}

        # Construire l'état courant — fusionner les groupes si doublons de pattern
        existing: dict[str, dict] = {}
        for d in self.get_deny_domains():
            pat = d["domain"]
            if pat not in existing:
                existing[pat] = dict(d)
            else:
                merged = list(set(existing[pat].get("groups", []) + d.get("groups", [])))
                existing[pat]["groups"] = merged

        added = removed = updated = 0

        # Retirer le groupe des entrées protectado qui ne sont plus dans la cible
        for pattern, entry in existing.items():
            if (entry.get("comment", "").startswith("protectado:")
                    and group_id in entry.get("groups", [])
                    and pattern not in target_patterns):
                remaining = [g for g in entry.get("groups", []) if g != group_id]
                self._delete(f"/domains/deny/regex/{urlquote(pattern, safe='')}")
                if remaining:
                    self._post("/domains/deny/regex", {
                        "domain": pattern,
                        "comment": entry.get("comment", tag),
                        "enabled": True,
                        "groups": remaining,
                    })
                removed += 1

        # Ajouter / mettre à jour les entrées cibles
        for domain in domains:
            pattern = self._domain_to_pattern(domain)
            if pattern not in existing:
                self._post("/domains/deny/regex", {
                    "domain": pattern,
                    "comment": tag,
                    "enabled": True,
                    "groups": [group_id]
                })
                added += 1
            else:
                current_groups = existing[pattern].get("groups", [])
                if group_id not in current_groups:
                    merged = list(set(current_groups) | {group_id})
                    self._delete(f"/domains/deny/regex/{urlquote(pattern, safe='')}")
                    self._post("/domains/deny/regex", {
                        "domain": pattern,
                        "comment": existing[pattern].get("comment", tag),
                        "enabled": True,
                        "groups": merged
                    })
                    updated += 1

        if added or removed or updated:
            print(f"[PiHole] blacklist groupe {group_id} ({mode}) : {added} ajoutés, {removed} retirés, {updated} mis à jour")

    # ------------------------------------------------------------------ #
    #  API métier (requêtes DNS)                                          #
    # ------------------------------------------------------------------ #

    def get_summary(self) -> dict:
        return self._get("/stats/summary")

    def get_recent_queries(self, minutes: int = 5) -> list:
        since = int((datetime.now() - timedelta(minutes=minutes)).timestamp())
        data = self._get("/queries", {"from": since, "length": 500})
        return data.get("queries", [])

    def queries_by_client(self, queries: list) -> dict:
        by_client = {}
        for q in queries:
            client_ip = q.get("client", {}).get("ip", "")
            domain = q.get("domain", "")
            if client_ip:
                by_client.setdefault(client_ip, []).append(domain)
        return by_client

    def is_healthy(self) -> bool:
        data = self._get("/info/version")
        return "version" in data
