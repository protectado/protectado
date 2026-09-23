#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Arnaud Ortais
# Dual-licensed: AGPL-3.0 (open source) or Commercial License — see LICENSE and LICENSE-COMMERCIAL.
#
# net-common.sh — fonctions réseau partagées par ap-persist.sh, config-ap.sh et
# uplink-persist.sh. À SOURCER, jamais à exécuter :
#
#     . "$(dirname "${BASH_SOURCE[0]}")/net-common.sh"
#
# Raison d'être : ces trois scripts codaient chacun leur domaine réglementaire en dur
# (FR pour deux d'entre eux, CA pour le troisième — deux valeurs contradictoires sur la
# même machine) et leur propre détection de la carte AP. Un boîtier installé hors de
# France émettait donc sur un plan de fréquences qui n'est pas le sien.

CONFIG_JSON="${CONFIG_JSON:-/opt/protectado/data/config.json}"

# ------------------------------------------------------------------ #
#  Domaine réglementaire                                              #
# ------------------------------------------------------------------ #

# pt_country — code pays ISO 3166-1 alpha-2 du boîtier.
#   1. config.network.country  (choisi par le parent dans l'assistant)
#   2. $PT_COUNTRY             (dépannage / tests)
#   3. le domaine déjà appliqué au noyau, s'il désigne un vrai pays
#   4. le fuseau horaire du système, traduit en pays via /usr/share/zoneinfo/zone.tab
#   5. ""                      (inconnu — l'appelant OMET alors country_code)
#
# Le repli n'est PAS "00". hostapd refuse "00" comme valeur littérale :
#   Line 4: Invalid country_code '00'
#   Failed to set up interface
# et l'AP de configuration ne montait plus du tout. Quand le pays est inconnu, la bonne
# réponse est de ne rien affirmer : on omet country_code et on laisse le pilote décider,
# plutôt que d'écrire une valeur que hostapd rejette ou qui restreint à tort.
#
# La déduction par fuseau horaire est LOCALE : zone.tab est un fichier livré avec le
# système, aucune requête réseau, aucun tiers interrogé. Elle donne le bon pays dans
# l'immense majorité des cas et n'est qu'un DÉFAUT : l'assistant demande le pays au
# parent, et sa réponse fait foi dès qu'elle est enregistrée.
pt_country() {
  local c=""
  if [ -f "$CONFIG_JSON" ]; then
    c="$(python3 - "$CONFIG_JSON" <<'PY' 2>/dev/null
import json, sys
try:
    n = json.load(open(sys.argv[1])).get('network') or {}
    c = (n.get('country') or '').strip().upper()
    print(c if len(c) == 2 and c.isalpha() else '')
except Exception:
    print('')
PY
)"
  fi
  [ -n "$c" ] || c="${PT_COUNTRY:-}"
  [ -n "$c" ] || c="$(pt_country_from_regdom)"
  [ -n "$c" ] || c="$(pt_country_from_timezone)"
  printf '%s' "$c"
}

# Domaine déjà appliqué au noyau. Souvent renseigné par la radio interne quand elle s'est
# associée à la box, qui diffuse son pays dans ses balises. "00" signifie « inconnu » et
# n'est donc pas retenu.
pt_country_from_regdom() {
  local c=""
  command -v iw >/dev/null || return 0
  c="$(iw reg get 2>/dev/null | sed -n 's/^country \([A-Z][A-Z]\):.*/\1/p' | head -1)"
  [ "$c" = "00" ] && c=""
  printf '%s' "$c"
}

# Fuseau horaire → pays, via la table livrée avec le système (aucun réseau).
pt_country_from_timezone() {
  local tz="" c="" tab=/usr/share/zoneinfo/zone.tab
  [ -r "$tab" ] || return 0
  tz="$(timedatectl show -p Timezone --value 2>/dev/null || true)"
  [ -n "$tz" ] || tz="$(cat /etc/timezone 2>/dev/null || true)"
  [ -n "$tz" ] || return 0
  c="$(awk -v tz="$tz" '$0 !~ /^#/ && $3 == tz { print $1; exit }' "$tab" 2>/dev/null)"
  printf '%s' "$c"
}

# pt_apply_regdom <country> — applique le domaine réglementaire au noyau.
# Pays inconnu : on ne force rien, le pilote garde son propre défaut.
pt_apply_regdom() {
  local country="${1:-$(pt_country)}"
  [ -n "$country" ] || return 0
  iw reg set "$country" 2>/dev/null || true
}

# pt_channel_allowed <phy> <freq_mhz> — 0 si la fréquence est utilisable en AP.
# « disabled » et « no IR » (no initiate radiation) interdisent tous deux d'émettre :
# un AP sur un canal no-IR ne démarre pas. On lit l'état réel du noyau APRÈS
# application du domaine, plutôt que d'embarquer une table réglementaire à maintenir.
pt_channel_allowed() {
  local phy="$1" freq="$2" line
  command -v iw >/dev/null || return 0          # pas d'iw : on ne bloque rien
  line="$(iw phy "$phy" info 2>/dev/null | grep -F "* ${freq} MHz" | head -1)" || true
  [ -n "$line" ] || return 1                    # fréquence absente = non supportée
  case "$line" in
    *disabled*|*"no IR"*|*"No IR"*) return 1 ;;
  esac
  return 0
}

# pt_pick_ap_band <iface> — choisit la bande de l'AP enfants pour le domaine courant.
# Écrit « hw_mode channel » sur stdout (ex. « a 36 » ou « g 6 »).
#
# Canal 36 (5180 MHz) est le choix par défaut (non-DFS, propre, enfants proches du Pi).
# S'il n'est pas autorisé dans le domaine retenu, on retombe sur 2,4 GHz canal 6 plutôt
# que de laisser hostapd refuser de démarrer — un AP en 2,4 GHz vaut mieux que pas d'AP.
# Si l'état de la radio est indéterminable, on garde 5 GHz : c'est le comportement
# historique, on ne dégrade pas un boîtier qui fonctionne.
pt_pick_ap_band() {
  local iface="${1:-}" phy=""
  if [ -n "$iface" ] && [ -e "/sys/class/net/$iface/phy80211/name" ]; then
    phy="$(cat "/sys/class/net/$iface/phy80211/name" 2>/dev/null || true)"
  fi
  if [ -z "$phy" ]; then
    echo "a 36"; return 0
  fi
  if pt_channel_allowed "$phy" 5180; then
    echo "a 36"
  elif pt_channel_allowed "$phy" 2437; then
    echo "g 6"
  else
    echo "a 36"
  fi
}

# ------------------------------------------------------------------ #
#  Détection de la carte AP                                           #
# ------------------------------------------------------------------ #

# Chipset MT7612U (pilote mt76x2u) — la carte de référence est l'Alfa AWUS036ACM,
# mais TOUTE carte pilotée par mt76x2u convient. On matche donc le PILOTE en premier
# et le couple VID:PID seulement en secours : appairer sur 0e8d:7612 uniquement
# excluait sans raison les autres cartes du même chipset.
PT_AP_DRIVER="${PT_AP_DRIVER:-mt76x2u}"
PT_AP_USB="${PT_AP_USB:-0e8d:7612}"

# pt_iface_driver <iface> — nom du pilote noyau d'une interface (vide si inconnu).
pt_iface_driver() {
  local iface="$1" p
  p="$(readlink -f "/sys/class/net/$iface/device/driver" 2>/dev/null || true)"
  [ -n "$p" ] && basename "$p" || true
}

# pt_find_ap_iface — nom de l'interface de la carte AP, vide si absente.
pt_find_ap_iface() {
  local i name
  # 1) Par pilote — le critère fiable, indépendant du modèle exact de dongle.
  for i in /sys/class/net/*; do
    name="$(basename "$i")"
    [ -d "$i/wireless" ] || [ -e "$i/phy80211" ] || continue
    [ "$(pt_iface_driver "$name")" = "$PT_AP_DRIVER" ] && { printf '%s' "$name"; return 0; }
  done
  # 2) Secours : par identifiant USB, pour une carte que le pilote n'aurait pas nommée.
  command -v udevadm >/dev/null || return 1
  for i in /sys/class/net/*; do
    name="$(basename "$i")"
    [ -d "$i/wireless" ] || [ -e "$i/phy80211" ] || continue
    if udevadm info -q property -p "/sys/class/net/$name" 2>/dev/null \
         | grep -qiE "ID_(USB_)?(MODEL_)?ID=|ID_VENDOR_ID=" \
       && udevadm info -q property -p "/sys/class/net/$name" 2>/dev/null \
         | grep -qi "${PT_AP_USB%%:*}" ; then
      printf '%s' "$name"; return 0
    fi
  done
  return 1
}
