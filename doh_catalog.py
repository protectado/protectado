# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Arnaud Ortais
# Dual-licensed: AGPL-3.0 (open source) or Commercial License — see LICENSE and LICENSE-COMMERCIAL.
"""
doh_catalog.py — résolveurs DNS-over-HTTPS connus (catalog/doh_resolvers.json).

En mode passerelle, un appareil qui résout ses noms en HTTPS auprès d'un résolveur
public échappe à Pi-hole. Le catalogue sert deux usages : ses domaines sont refusés par
Pi-hole aux groupes enfants (le navigateur ne trouve plus son résolveur), et ses
adresses IPv4 voient leur port 443 rejeté depuis le réseau enfants (une adresse écrite
en dur dans une application).

C'est de la DONNÉE, dans catalog/ : elle se met à jour par le chemin « catalogue seul »
de l'updater, sans redémarrage. Un fichier absent ou invalide donne un catalogue vide
et un message, jamais une exception : une liste de contournements manquante ne doit
pas empêcher le reste de l'effecteur de fonctionner.
"""

import ipaddress
import json
import os
import re

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "catalog", "doh_resolvers.json")
_DOMAIN_RE = re.compile(r'^(?:[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$')

VIDE = {"domains": frozenset(), "ipv4": frozenset()}


def stamp(path: str = PATH) -> int:
    """Empreinte du fichier (mtime), 0 s'il est absent : son apparition est un changement."""
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return 0


def load(path: str = PATH) -> dict:
    """{"domains": frozenset, "ipv4": frozenset}. Les entrées invalides sont écartées une
    par une ; un fichier illisible donne un catalogue vide."""
    try:
        with open(path) as f:
            data = json.load(f)
        domains_src, ips_src = data.get("domains") or [], data.get("ipv4") or []
        if not isinstance(domains_src, list) or not isinstance(ips_src, list):
            raise ValueError("« domains » et « ipv4 » doivent être des listes")
    except (OSError, ValueError) as e:
        print(f"[DoH] catalogue {path} illisible, aucun résolveur DoH bloqué : {e}")
        return dict(VIDE)

    domains = set()
    for d in domains_src:
        d = str(d).strip().lower()
        if _DOMAIN_RE.match(d):
            domains.add(d)
        else:
            print(f"[DoH] domaine ignoré dans le catalogue : {d!r}")
    ips = set()
    for ip in ips_src:
        try:
            ips.add(str(ipaddress.IPv4Address(str(ip).strip())))
        except ValueError:
            print(f"[DoH] adresse ignorée dans le catalogue : {ip!r}")
    return {"domains": frozenset(domains), "ipv4": frozenset(ips)}
