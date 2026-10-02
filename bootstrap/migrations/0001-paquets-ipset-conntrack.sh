#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Arnaud Ortais
#
# Effet : installe ipset (blocage DNS-over-HTTPS par adresse) et conntrack (relevés de
#         flux pour signaler un tunnel probable), absents des boîtiers installés avant
#         leur ajout.
# Équivalent bootstrap : liste « apt-get install » de step1_system (bootstrap.sh).
# Retour arrière : sudo apt-get remove ipset conntrack. Le runner passe alors en
#         « degraded » (blocage DoH par adresse inactif) et cesse les relevés de flux.
set -euo pipefail

manquants=()
for paquet in ipset conntrack; do
  dpkg -s "$paquet" >/dev/null 2>&1 || manquants+=("$paquet")
done
[ "${#manquants[@]}" -eq 0 ] && exit 0

export DEBIAN_FRONTEND=noninteractive
installer() { apt-get -o DPkg::Lock::Timeout=300 install -y -q "${manquants[@]}"; }
# Des listes de paquets périmées empêchent de trouver le paquet : on les rafraîchit une
# fois avant de conclure à un échec.
installer || { apt-get -o DPkg::Lock::Timeout=300 update -q && installer; }
