#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Arnaud Ortais
# Dual-licensed: AGPL-3.0 (open source) or Commercial License — see LICENSE and LICENSE-COMMERCIAL.
#
# gateway_check.sh — vérification de bout en bout du mode passerelle, sur un boîtier réel.
#
# À lancer depuis un ordinateur connecté au Wi-Fi ENFANTS avec la clé d'un profil de test
# (voir tests/e2e/README.md). Chaque vérification affiche OK ou KO ; le script s'arrête au
# premier KO avec un code de sortie non nul.
#
#   PROFILE=test BLOCKED_DOMAIN=exemple-bloque.com bash tests/e2e/gateway_check.sh
#
set -uo pipefail

PROFILE="${PROFILE:?PROFILE : clé du profil de test (ex. test)}"
BLOCKED_DOMAIN="${BLOCKED_DOMAIN:?BLOCKED_DOMAIN : domaine bloqué pour ce profil dans son mode actuel}"
KIDS_GW="${KIDS_GW:-192.168.50.1}"
KIDS_IFACE="${KIDS_IFACE:-}"           # interface Wi-Fi enfants, si la machine en a plusieurs
DASHBOARD_URL="${DASHBOARD_URL:-}"     # ex. http://192.168.0.54, joignable HORS réseau enfants
SESSION="${SESSION:-}"                 # valeur du cookie fw_session d'une session parent
LONG_URL="${LONG_URL:-https://speed.cloudflare.com/__down?bytes=2000000000}"
OK_URL="${OK_URL:-https://example.com}"

C_OK=$'\033[32m'; C_KO=$'\033[31m'; C_Z=$'\033[0m'
ok() { echo "  ${C_OK}[ OK ]${C_Z} $*"; }
ko() { echo "  ${C_KO}[ KO ]${C_Z} $*"; exit 1; }
etape() { echo; echo "── $* ──"; }

for outil in curl dig; do
  command -v "$outil" >/dev/null || { echo "outil manquant : $outil"; exit 2; }
done

# Tout le trafic de test part par le Wi-Fi enfants, même sur une machine qui a aussi un
# câble vers la box : un test qui sortirait par le câble ne prouverait rien.
CURL_IF=(); DIG_IF=()
if [ -n "$KIDS_IFACE" ]; then
  SRC=$(ip -4 -o addr show dev "$KIDS_IFACE" | awk '{print $4}' | cut -d/ -f1 | head -1)
  [ -n "$SRC" ] || { echo "aucune adresse IPv4 sur $KIDS_IFACE"; exit 2; }
  CURL_IF=(--interface "$KIDS_IFACE"); DIG_IF=(-b "$SRC")
fi
c() { curl "${CURL_IF[@]}" -sS -o /dev/null "$@"; }

etape "0. Prérequis : Internet fonctionne depuis le réseau enfants"
c -m 10 "$OK_URL" && ok "$OK_URL joignable" \
  || ko "$OK_URL injoignable : profil en plage ouverte ? appareil reconnu par sa clé ?"

etape "1. Un DNS public tapé en dur est intercepté (port 53 redirigé vers Pi-hole)"
rep=$(dig "${DIG_IF[@]}" +time=5 +tries=1 @8.8.8.8 "$BLOCKED_DOMAIN" A +noall +comments +answer)
if echo "$rep" | grep -q "status: NXDOMAIN" \
   || echo "$rep" | awk '$4=="A"{print $5}' | grep -qx "0.0.0.0"; then
  ok "$BLOCKED_DOMAIN via 8.8.8.8 : réponse de blocage"
else
  ko "$BLOCKED_DOMAIN via 8.8.8.8 résolu normalement : $(echo "$rep" | awk '$4=="A"{print $5}' | head -1)"
fi

etape "2. DNS-over-HTTPS vers un résolveur connu"
c -m 8 -H 'accept: application/dns-json' \
  'https://cloudflare-dns.com/dns-query?name=example.com&type=A' \
  && ko "cloudflare-dns.com joignable par son nom" || ok "cloudflare-dns.com refusé par son nom"
c -m 8 --resolve cloudflare-dns.com:443:1.1.1.1 -H 'accept: application/dns-json' \
  'https://cloudflare-dns.com/dns-query?name=example.com&type=A' \
  && ko "1.1.1.1:443 joignable par son adresse" || ok "1.1.1.1:443 refusé par son adresse"
c -m 8 --resolve dns.google:443:8.8.8.8 'https://dns.google/resolve?name=example.com' \
  && ko "8.8.8.8:443 joignable par son adresse" || ok "8.8.8.8:443 refusé par son adresse"

etape "3. DNS-over-TLS (port 853)"
for ip in 1.1.1.1 9.9.9.9; do
  if timeout 6 bash -c "</dev/tcp/$ip/853" 2>/dev/null; then
    ko "$ip:853 joignable"
  fi
  ok "$ip:853 refusé"
done

etape "4. Domaines spéciaux (Firefox, iCloud Private Relay)"
for d in use-application-dns.net mask.icloud.com mask-h2.icloud.com; do
  statut=$(dig "${DIG_IF[@]}" +time=5 +tries=1 @"$KIDS_GW" "$d" A +noall +comments \
           | sed -n 's/.*status: \([A-Z]*\).*/\1/p')
  [ "$statut" = "NXDOMAIN" ] && ok "$d → NXDOMAIN" || ko "$d → ${statut:-pas de réponse}"
done

etape "5. Aucune sortie IPv6"
if curl "${CURL_IF[@]}" -6 -sS -o /dev/null -m 6 https://ipv6.google.com 2>/dev/null; then
  ko "IPv6 sortant possible depuis le réseau enfants"
fi
ok "pas de connectivité IPv6 sortante"

etape "6. Passage en « coupé » : les connexions en cours tombent, puis retour"
basculer() {  # $1 = off | restore
  if [ -n "$DASHBOARD_URL" ] && [ -n "$SESSION" ]; then
    if [ "$1" = off ]; then
      curl -fsS -m 10 -b "fw_session=$SESSION" -H 'Content-Type: application/json' \
        -d "{\"profile\":\"$PROFILE\",\"mode\":\"off\",\"minutes\":10}" \
        "$DASHBOARD_URL/api/temp-overrides" >/dev/null || ko "API : dérogation refusée"
    else
      curl -fsS -m 10 -b "fw_session=$SESSION" -X DELETE \
        "$DASHBOARD_URL/api/temp-overrides/$PROFILE" >/dev/null || ko "API : annulation refusée"
    fi
  else
    # Le tableau de bord refuse le réseau enfants : sans accès depuis un autre réseau,
    # c'est l'opérateur qui bascule, depuis le téléphone du parent.
    if [ "$1" = off ]; then
      read -r -p "   Coupez « $PROFILE » maintenant (onglet Enfants), puis Entrée… " _
    else
      read -r -p "   Annulez la coupure de « $PROFILE » (onglet Exceptions), puis Entrée… " _
    fi
  fi
}

curl "${CURL_IF[@]}" -sS -o /dev/null -m 600 "$LONG_URL" 2>/dev/null &
DL=$!
sleep 5
kill -0 "$DL" 2>/dev/null || ko "le téléchargement long n'a pas démarré ($LONG_URL)"
ok "téléchargement long en cours"
basculer off
debut=$(date +%s)
while kill -0 "$DL" 2>/dev/null; do
  if [ $(( $(date +%s) - debut )) -ge 10 ]; then
    kill "$DL" 2>/dev/null
    ko "le téléchargement en cours continue plus de 10 s après la coupure"
  fi
  sleep 1
done
ok "téléchargement interrompu en $(( $(date +%s) - debut )) s"
c -m 8 "$OK_URL" && ko "une nouvelle connexion aboutit malgré la coupure" \
  || ok "aucune nouvelle connexion n'aboutit"

basculer restore
for _ in $(seq 1 30); do
  c -m 5 "$OK_URL" && { ok "connectivité revenue"; echo; echo "${C_OK}Toutes les vérifications sont passées.${C_Z}"; exit 0; }
  sleep 2
done
ko "la connectivité n'est pas revenue 60 s après l'annulation de la coupure"
