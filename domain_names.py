# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Arnaud Ortais
# Dual-licensed: AGPL-3.0 (open source) or Commercial License — see LICENSE and LICENSE-COMMERCIAL.
"""
domain_names.py — réduction d'un nom de domaine à son domaine racine.

Une seule implémentation pour le moniteur (décomptes, classification) et l'assistant
(déblocage temporaire) : deux réductions divergentes débloqueraient un autre domaine que
celui qui est compté et bloqué.
"""

# Suffixes publics à deux niveaux les plus courants sur les marchés visés
# (fr/en/es/pt) et chez les grands services. Sans cette liste, « bbc.co.uk » était
# réduit à « co.uk » et « globo.com.br » à « com.br » : catégorisation fausse, et
# surtout blocage d'un TLD ENTIER si un tel « domaine racine » atterrissait en
# blacklist (le motif Pi-hole devient `(.*\.)?com\.br$`).
# Liste embarquée volontairement : pas d'appel réseau, pas de dépendance.
MULTI_LEVEL_SUFFIXES = frozenset("""
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


def root_domain(domain: str) -> str:
    """Domaine « racine » facturable : deux labels, ou trois si le suffixe est composé.

    bbc.co.uk → bbc.co.uk (et non co.uk) ; www.globo.com.br → globo.com.br.
    """
    parts = domain.rstrip(".").lower().split(".")
    if len(parts) < 2:
        return domain
    if len(parts) >= 3 and ".".join(parts[-2:]) in MULTI_LEVEL_SUFFIXES:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])
