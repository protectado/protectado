#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Arnaud Ortais
#
# config-ap.sh — Posture CONFIG (onboarding, jalon assistant premier démarrage).
#   Diffuse un AP OUVERT "Protectado-Setup" sur wlan_ap, ISOLÉ (aucun NAT/forward),
#   avec un CAPTIVE PORTAL : tout DNS → le Pi, tout HTTP → l'assistant (dashboard :8080).
#   Sert UNIQUEMENT à l'assistant de configuration ; ne donne accès ni au réseau enfants
#   ni au réseau parents. Session TEMPORAIRE : tout dans /run (effacé au reboot).
#
#   DNS captif sur :5353 (le :53 est pris par Pi-hole/FTL) + REDIRECT iptables du :53.
#
# Usage :
#   sudo bash config-ap.sh up       # démarre le config-AP + captive portal, puis diagnostic
#   sudo bash config-ap.sh status
#   sudo bash config-ap.sh down
#
set -euo pipefail
export PATH="/usr/sbin:/sbin:/usr/bin:/bin:$PATH"

# shellcheck source=net-common.sh
. "$(dirname "${BASH_SOURCE[0]}")/net-common.sh"

# Détection de la carte AP : déléguée à net-common.sh (PT_AP_DRIVER / PT_AP_USB), pour
# que bootstrap.sh, ap-persist.sh et ce script ne divergent pas sur ce qu'est « la carte AP ».
IFACE=""

SETUP_SSID="Protectado-Setup"
CONF_IP="192.168.4.1"
CONF_CIDR="192.168.4.1/24"
DHCP_FROM="192.168.4.10"
DHCP_TO="192.168.4.100"
# L'AP de configuration tourne AVANT que le parent n'ait choisi son pays : pt_country()
# se rabat alors sur le domaine du noyau puis sur le fuseau horaire. Si rien ne permet de
# conclure, COUNTRY reste VIDE et la ligne country_code est omise de la conf : hostapd
# refuse la valeur littérale "00" et l'AP ne démarrait pas du tout.
# Le canal 6 en 2,4 GHz reste le choix de cette posture, pour la compatibilité maximale
# avec le téléphone d'un parent.
COUNTRY="$(pt_country)"
CHANNEL="6"                 # 2.4 GHz : compat maximale pour le téléphone d'un parent
DNS_PORT="5353"             # dnsmasq captif (évite le conflit :53 avec Pi-hole/FTL)
PORTAL_PORT="8080"          # dashboard / assistant

RUN_DIR="/run/protectado-config"
HOSTAPD_CONF="$RUN_DIR/hostapd.conf"
DNSMASQ_CONF="$RUN_DIR/dnsmasq.conf"
HOSTAPD_PID="$RUN_DIR/hostapd.pid"
DNSMASQ_PID="$RUN_DIR/dnsmasq.pid"

C_OK=$'\033[32m'; C_NO=$'\033[31m'; C_Z=$'\033[0m'
ok()  { echo "  ${C_OK}[ OK ]${C_Z} $*"; }
no()  { echo "  ${C_NO}[ !! ]${C_Z} $*"; }
die() { echo "${C_NO}ERREUR :${C_Z} $*" >&2; exit 1; }
need_root() { [ "$(id -u)" -eq 0 ] || die "à lancer en root (sudo)"; }
pid_alive() { local f="$1"; [ -f "$f" ] && kill -0 "$(cat "$f" 2>/dev/null)" 2>/dev/null; }

ensure_tools() {
  local m=()
  command -v hostapd >/dev/null || m+=(hostapd)
  command -v dnsmasq >/dev/null || m+=(dnsmasq)
  command -v iw >/dev/null || m+=(iw)
  command -v rfkill >/dev/null || m+=(rfkill)
  command -v iptables >/dev/null || m+=(iptables)
  if [ "${#m[@]}" -gt 0 ]; then
    apt-get install -y "${m[@]}"
    systemctl unmask hostapd 2>/dev/null || true
    systemctl disable --now hostapd dnsmasq 2>/dev/null || true
  fi
}

detect_iface() {
  IFACE="$(pt_find_ap_iface)" && [ -n "$IFACE" ]
}

# Règles iptables du captive portal (chaîne nat dédiée) + isolation (chaîne filter dédiée).
NAT_CHAIN="PROTECTADO_CONFIG_NAT"
FWD_CHAIN="PROTECTADO_CONFIG_FWD"

captive_up() {
  iptables -t nat -N "$NAT_CHAIN" 2>/dev/null || iptables -t nat -F "$NAT_CHAIN"
  iptables -t nat -A "$NAT_CHAIN" -p udp --dport 53 -j REDIRECT --to-ports "$DNS_PORT"
  iptables -t nat -A "$NAT_CHAIN" -p tcp --dport 53 -j REDIRECT --to-ports "$DNS_PORT"
  iptables -t nat -A "$NAT_CHAIN" -p tcp --dport 80 -j REDIRECT --to-ports "$PORTAL_PORT"
  iptables -t nat -C PREROUTING -i "$IFACE" -j "$NAT_CHAIN" 2>/dev/null \
    || iptables -t nat -I PREROUTING 1 -i "$IFACE" -j "$NAT_CHAIN"
  # Isolation : le config-AP ne route RIEN (ni enfants, ni parents, ni Internet).
  iptables -N "$FWD_CHAIN" 2>/dev/null || iptables -F "$FWD_CHAIN"
  iptables -A "$FWD_CHAIN" -j DROP
  iptables -C FORWARD -i "$IFACE" -j "$FWD_CHAIN" 2>/dev/null \
    || iptables -I FORWARD 1 -i "$IFACE" -j "$FWD_CHAIN"
}

captive_down() {
  iptables -t nat -D PREROUTING -i "$IFACE" -j "$NAT_CHAIN" 2>/dev/null || true
  iptables -t nat -F "$NAT_CHAIN" 2>/dev/null || true
  iptables -t nat -X "$NAT_CHAIN" 2>/dev/null || true
  iptables -D FORWARD -i "$IFACE" -j "$FWD_CHAIN" 2>/dev/null || true
  iptables -F "$FWD_CHAIN" 2>/dev/null || true
  iptables -X "$FWD_CHAIN" 2>/dev/null || true
}

write_confs() {
  mkdir -p "$RUN_DIR"
  cat > "$HOSTAPD_CONF" <<EOF
interface=$IFACE
driver=nl80211
ssid=$SETUP_SSID
${COUNTRY:+country_code=$COUNTRY}
hw_mode=g
channel=$CHANNEL
auth_algs=1
wmm_enabled=1
EOF
  cat > "$DNSMASQ_CONF" <<EOF
port=$DNS_PORT
interface=$IFACE
bind-interfaces
dhcp-range=$DHCP_FROM,$DHCP_TO,255.255.255.0,1h
dhcp-option=3,$CONF_IP
dhcp-option=6,$CONF_IP
dhcp-authoritative
address=/#/$CONF_IP
dhcp-leasefile=$RUN_DIR/leases
EOF
}

release_iface() {
  rfkill unblock wlan 2>/dev/null || true
  [ -n "$COUNTRY" ] && iw reg set "$COUNTRY" 2>/dev/null || true
  # Libérer wlan_ap de la posture GATEWAY (hostapd enfants) si elle tourne — sinon
  # deux hostapd sur la même interface → "Match already configured" + segfault.
  systemctl stop protectado-ap.service protectado-ap-dhcp.service 2>/dev/null || true
  pkill -f "hostapd .*/etc/protectado/hostapd-ap.conf" 2>/dev/null || true
  pkill -f "wpa_supplicant.*-i$IFACE\b" 2>/dev/null || true
  ip addr flush dev "$IFACE" 2>/dev/null || true
  ip link set "$IFACE" down 2>/dev/null || true
  sleep 1
}

do_down() {
  detect_iface || IFACE=""
  [ -f "$HOSTAPD_PID" ] && kill "$(cat "$HOSTAPD_PID")" 2>/dev/null || true
  [ -f "$DNSMASQ_PID" ] && kill "$(cat "$DNSMASQ_PID")" 2>/dev/null || true
  pkill -f "$HOSTAPD_CONF" 2>/dev/null || true
  pkill -f "$DNSMASQ_CONF" 2>/dev/null || true
  [ -n "$IFACE" ] && captive_down
  [ -n "$IFACE" ] && { ip addr flush dev "$IFACE" 2>/dev/null || true; ip link set "$IFACE" down 2>/dev/null || true; }
  rm -rf "$RUN_DIR"
  echo "→ Config-AP arrêté. (Un reboot aurait tout effacé.)"
}

do_up() {
  need_root; ensure_tools
  detect_iface || die "carte AP (MT7612U) introuvable — 2ᵉ radio branchée ?"
  do_down >/dev/null 2>&1 || true
  detect_iface
  release_iface
  write_confs
  hostapd -B -P "$HOSTAPD_PID" "$HOSTAPD_CONF" \
    || die "hostapd n'a pas démarré (config-AP)."
  sleep 1
  ip addr add "$CONF_CIDR" dev "$IFACE"
  ip link set "$IFACE" up
  dnsmasq --conf-file="$DNSMASQ_CONF" --pid-file="$DNSMASQ_PID"
  captive_up
  echo; echo "Config-AP '$SETUP_SSID' (OUVERT) démarré sur $IFACE — connecte un téléphone, l'assistant doit s'ouvrir."
  echo
  do_status
}

do_status() {
  detect_iface || die "carte AP introuvable."
  echo "── Config-AP (onboarding) — interface $IFACE ─────────────────────"
  iw dev "$IFACE" info 2>/dev/null | grep -q 'type AP' && ok "$IFACE en type AP" || no "$IFACE pas en type AP"
  iw dev "$IFACE" info 2>/dev/null | grep -q "ssid $SETUP_SSID" && ok "SSID '$SETUP_SSID' diffusé" || no "SSID '$SETUP_SSID' absent"
  ip -brief addr show "$IFACE" 2>/dev/null | grep -q "${CONF_IP}/24" && ok "IP $CONF_CIDR posée" || no "IP absente"
  pid_alive "$HOSTAPD_PID" && ok "hostapd tourne (ouvert)" || no "hostapd absent"
  pid_alive "$DNSMASQ_PID" && ok "dnsmasq captif tourne (:$DNS_PORT)" || no "dnsmasq absent"
  iptables -t nat -C PREROUTING -i "$IFACE" -j "$NAT_CHAIN" 2>/dev/null && ok "captive portal actif (DNS→Pi, :80→assistant)" || no "captive portal absent"
  iptables -C FORWARD -i "$IFACE" -j "$FWD_CHAIN" 2>/dev/null && ok "ISOLÉ (aucun forward depuis le config-AP)" || no "isolation absente"
  ip -brief addr show eth0 2>/dev/null | grep -q UP && ok "eth0 (admin) intact" || no "eth0 non UP"
  echo "  Baux : $(sed 's/^/                /' "$RUN_DIR/leases" 2>/dev/null | tr -s ' ' || echo '(aucun)')"
  echo "──────────────────────────────────────────────────────────────────"
}

case "${1:-}" in
  up)     do_up ;;
  status) do_status ;;
  down)   need_root; do_down ;;
  *) echo "Usage: sudo bash $0 {up|status|down}"; exit 1 ;;
esac
