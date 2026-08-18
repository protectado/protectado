#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Arnaud Ortais
#
# uplink-persist.sh — Uplink + NAT PERSISTANT (Ubuntu, netplan + networkd-dispatcher, SANS NM).
#   'wifi'    : connecte wlan_up (radio interne) au Wi-Fi de la box, en NETPLAN natif (persistant).
#   'install' : ip_forward durable + un hook networkd-dispatcher qui, dès que wlan_up devient
#               routable, pose le routage par politique (enfants 192.168.50.0/24 → wlan_up,
#               même si eth0 porte la route par défaut) + NAT MASQUERADE + FORWARD.
#   DNS enfants pointé sur Pi-hole (192.168.50.1) — filtrage réel.
#   Ne touche jamais eth0 (admin, géré par netplan). Rien sur INPUT : SSH intact.
#
# Identifiants box en variables d'env (jamais écrits dans le dépôt) :
#   sudo BOX_SSID="MaBox" BOX_PASS="motdepasse" bash uplink-persist.sh wifi
#
# Usage :
#   sudo BOX_SSID=… BOX_PASS=… bash uplink-persist.sh wifi
#   sudo bash uplink-persist.sh install
#   sudo bash uplink-persist.sh status
#   sudo bash uplink-persist.sh uninstall
#
set -euo pipefail
export PATH="/usr/sbin:/sbin:/usr/bin:/bin:$PATH"

# shellcheck source=net-common.sh
. "$(dirname "${BASH_SOURCE[0]}")/net-common.sh"

UP_IFACE="wlan_up"
AP_IFACE="wlan_ap"
KIDS_SUBNET="192.168.50.0/24"
RT_TABLE="100"
KIDS_DNS="${KIDS_DNS:-192.168.50.1}"   # Pi-hole (filtrage). 1.1.1.1 pour bypass si besoin.
# Domaine réglementaire — MÊME source que l'AP enfants (config.network.country).
# Auparavant "CA" en dur ici et "FR" en dur dans ap-persist.sh : deux domaines
# contradictoires sur la même machine. $REGDOM reste accepté pour le dépannage.
REGDOM="${REGDOM:-$(pt_country)}"

NETPLAN_WIFI="/etc/netplan/60-protectado-uplink.yaml"
SYSCTL_FILE="/etc/sysctl.d/99-protectado-forward.conf"
DISPATCHER="/etc/networkd-dispatcher/routable.d/50-protectado-uplink"
DNSMASQ_CONF="/etc/protectado/dnsmasq-ap.conf"

C_OK=$'\033[32m'; C_NO=$'\033[31m'; C_Z=$'\033[0m'
ok()  { echo "  ${C_OK}[ OK ]${C_Z} $*"; }
no()  { echo "  ${C_NO}[ !! ]${C_Z} $*"; }
die() { echo "${C_NO}ERREUR :${C_Z} $*" >&2; exit 1; }
need_root() { [ "$(id -u)" -eq 0 ] || die "à lancer en root (sudo)"; }
up_has_internet() { ping -c1 -W2 -I "$UP_IFACE" 1.1.1.1 >/dev/null 2>&1; }

do_wifi() {
  need_root
  command -v wpa_supplicant >/dev/null || apt-get install -y wpasupplicant
  python3 -c 'import yaml' 2>/dev/null || apt-get install -y python3-yaml >/dev/null 2>&1 || true
  # Reprendre les identifiants Wi-Fi de la box configurés à l'Imager (cloud-init) si non fournis.
  if [ -z "${BOX_SSID:-}" ]; then
    creds="$(python3 - "$NETPLAN_WIFI" <<'PY'
import glob, sys, yaml
skip = sys.argv[1]
for f in sorted(glob.glob("/etc/netplan/*.yaml")):
    if f == skip: continue
    try: d = yaml.safe_load(open(f)) or {}
    except Exception: continue
    for iface, cfg in ((d.get("network") or {}).get("wifis") or {}).items():
        for ssid, ap in ((cfg or {}).get("access-points") or {}).items():
            ap = ap or {}
            pw = ap.get("password") or ((ap.get("auth") or {}).get("password"))
            if ssid and pw:
                print(ssid + "\t" + pw); raise SystemExit
PY
)"
    BOX_SSID="$(printf '%s' "$creds" | cut -f1)"
    BOX_PASS="$(printf '%s' "$creds" | cut -f2)"
    [ -n "$BOX_SSID" ] && echo "→ Identifiants box repris du cloud-init : SSID '$BOX_SSID'."
  fi
  [ -n "${BOX_SSID:-}" ] || die "Aucun SSID box : fournis BOX_SSID=… BOX_PASS=… (ou configure le Wi-Fi à l'Imager)."
  if [ "${BOOT_ONLY:-0}" != "1" ]; then
    ip link show "$UP_IFACE" >/dev/null 2>&1 || die "$UP_IFACE absente (jalon AP/udev fait ?)."
  fi

  # Neutraliser toute config Wi-Fi cloud-init (sur l'ANCIEN nom wlan0), orpheline après
  # notre renommage udev (wlan0 → wlan_up) : sinon netplan-wpa-wlan0.service plante en boucle.
  # On fige cloud-init réseau, puis on RETIRE chirurgicalement le bloc `wifis` des fichiers
  # netplan existants (en préservant eth0), via python+yaml.
  printf 'network: {config: disabled}\n' > /etc/cloud/cloud.cfg.d/99-disable-network-config.cfg 2>/dev/null || true
  python3 -c 'import yaml' 2>/dev/null || apt-get install -y python3-yaml >/dev/null 2>&1 || true
  for nf in /etc/netplan/*.yaml; do
    [ "$nf" = "$NETPLAN_WIFI" ] && continue
    grep -q '^\s*wifis:' "$nf" 2>/dev/null || continue
    echo "→ Retrait du bloc Wi-Fi orphelin de $nf (eth0 conservé)…"
    python3 - "$nf" <<'PY' || echo "  ⚠ échec du retrait auto — édite $nf à la main (supprime la section wifis)."
import yaml, sys
p = sys.argv[1]
d = yaml.safe_load(open(p)) or {}
net = d.get("network") or {}
if "wifis" in net:
    del net["wifis"]
    yaml.safe_dump(d, open(p, "w"), default_flow_style=False, sort_keys=False)
PY
    chmod 600 "$nf"
  done

  umask 077
  cat > "$NETPLAN_WIFI" <<EOF
network:
  version: 2
  wifis:
    $UP_IFACE:
      dhcp4: true
${REGDOM:+      regulatory-domain: "$REGDOM"}
      access-points:
        "$BOX_SSID":
          password: "$BOX_PASS"
EOF
  chmod 600 "$NETPLAN_WIFI"
  if [ "${BOOT_ONLY:-0}" = "1" ]; then
    netplan generate           # appliqué au boot (wlan_up n'existe qu'après le renommage udev)
    ok "$UP_IFACE configuré en client de '$BOX_SSID' (appliqué au boot)."
    return
  fi
  netplan generate && netplan apply
  echo "→ $UP_IFACE configuré en client de '$BOX_SSID' (netplan, persistant). Connexion…"
  for _ in $(seq 1 20); do up_has_internet && break; sleep 1; done
  if up_has_internet; then
    ok "$UP_IFACE a Internet ($(ip -4 -br addr show "$UP_IFACE" | awk '{print $3}'))"
  else
    no "$UP_IFACE sans Internet — vérifie SSID/mdp/portée : networkctl status $UP_IFACE"
  fi
}

do_install() {
  need_root
  command -v iptables >/dev/null || apt-get install -y iptables
  [ -d /etc/networkd-dispatcher ] || apt-get install -y networkd-dispatcher
  systemctl enable --now networkd-dispatcher >/dev/null 2>&1 || true

  # 1) Forwarding IPv4 + rp_filter LOOSE (persistant).
  #    eth0 et wlan_up sont sur le même LAN box : en rp_filter STRICT (1, défaut Ubuntu),
  #    les retours arrivant sur wlan_up sont jetés (route équivalente via eth0). LOOSE (2)
  #    valide la source si elle est joignable par UNE interface → indispensable ici.
  cat > "$SYSCTL_FILE" <<'EOF'
net.ipv4.ip_forward=1
net.ipv4.conf.all.rp_filter=2
net.ipv4.conf.default.rp_filter=2
EOF
  sysctl --system >/dev/null
  # forcer aussi les interfaces déjà présentes (all=2 ne rétroagit pas toujours)
  sysctl -w net.ipv4.conf.wlan_up.rp_filter=2 net.ipv4.conf.eth0.rp_filter=2 >/dev/null 2>&1 || true

  # 2) Hook networkd-dispatcher : routage enfants + NAT quand wlan_up devient routable.
  mkdir -p "$(dirname "$DISPATCHER")"
  cat > "$DISPATCHER" <<EOF
#!/bin/sh
# Protectado — routage enfants + NAT dès que l'uplink devient routable. Généré par uplink-persist.sh.
[ "\$IFACE" = "$UP_IFACE" ] || exit 0
GW=\$(ip -4 route show default dev "$UP_IFACE" | awk '{print \$3; exit}')
[ -n "\$GW" ] || exit 0
# Route LOCALE du réseau enfants d'abord : sinon les réponses du Pi (sourcées 192.168.50.1,
# donc matchées par la règle 'from') partiraient vers l'uplink au lieu de rester sur l'AP.
ip route replace $KIDS_SUBNET dev "$AP_IFACE" table $RT_TABLE
ip route replace default via "\$GW" dev "$UP_IFACE" table $RT_TABLE
ip rule show | grep -q "from $KIDS_SUBNET lookup $RT_TABLE" || ip rule add from "$KIDS_SUBNET" lookup $RT_TABLE
iptables -t nat -C POSTROUTING -o "$UP_IFACE" -j MASQUERADE 2>/dev/null || iptables -t nat -A POSTROUTING -o "$UP_IFACE" -j MASQUERADE
iptables -C FORWARD -i "$AP_IFACE" -o "$UP_IFACE" -j ACCEPT 2>/dev/null || iptables -A FORWARD -i "$AP_IFACE" -o "$UP_IFACE" -j ACCEPT
iptables -C FORWARD -i "$UP_IFACE" -o "$AP_IFACE" -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT 2>/dev/null \\
  || iptables -A FORWARD -i "$UP_IFACE" -o "$AP_IFACE" -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
# MSS clamping : évite le "PMTU black hole" (certains sites/TLS qui pendent) sur uplink NAT.
iptables -t mangle -C FORWARD -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu 2>/dev/null \\
  || iptables -t mangle -A FORWARD -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu
EOF
  chmod 755 "$DISPATCHER"

  # 3) DNS enfants → Pi-hole (filtrage). En boot-only, le fichier suffit (service pas encore lancé).
  if [ -f "$DNSMASQ_CONF" ]; then
    sed -i "s/^dhcp-option=6,.*/dhcp-option=6,$KIDS_DNS/" "$DNSMASQ_CONF"
    [ "${BOOT_ONLY:-0}" = "1" ] || systemctl restart protectado-ap-dhcp.service
  fi

  if [ "${BOOT_ONLY:-0}" = "1" ]; then
    echo "→ Persistance uplink posée (appliquée au boot : sysctl + dispatcher + DNS=$KIDS_DNS)."
    return
  fi
  # 4) Appliquer tout de suite (simule l'événement routable).
  IFACE="$UP_IFACE" sh "$DISPATCHER" || true

  echo "→ Persistance uplink posée (sysctl + networkd-dispatcher + DNS enfants=$KIDS_DNS)."
  echo "  Test réel : ${C_OK}sudo reboot${C_Z} puis, sans rien lancer, le téléphone sur l'AP a Internet."
}

do_status() {
  echo "── Uplink+NAT persistant (Ubuntu/networkd) ───────────────────────"
  [ -f "$NETPLAN_WIFI" ] && ok "netplan Wi-Fi client posé" || no "netplan Wi-Fi absent (lance 'wifi')"
  up_has_internet && ok "$UP_IFACE a Internet" || no "$UP_IFACE sans Internet"
  [ -f "$SYSCTL_FILE" ] && ok "sysctl ip_forward posé" || no "sysctl absent"
  [ -x "$DISPATCHER" ] && ok "hook networkd-dispatcher présent" || no "dispatcher absent"
  [ "$(cat /proc/sys/net/ipv4/ip_forward)" = "1" ] && ok "forwarding actif" || no "forwarding inactif"
  iptables -t nat -C POSTROUTING -o "$UP_IFACE" -j MASQUERADE 2>/dev/null && ok "NAT MASQUERADE actif" || no "NAT absent"
  local krt; krt="$(ip route get 1.1.1.1 from 192.168.50.50 iif "$AP_IFACE" 2>/dev/null | head -1)"
  echo "$krt" | grep -q "dev $UP_IFACE" && ok "trafic enfants routé via $UP_IFACE" || no "trafic enfants PAS via $UP_IFACE → $krt"
  echo "  DNS enfants distribué : $(grep '^dhcp-option=6,' "$DNSMASQ_CONF" 2>/dev/null | cut -d, -f2)"
  ip -brief addr show eth0 2>/dev/null | grep -q UP && ok "eth0 (admin) intact" || no "eth0 non UP"
  echo "──────────────────────────────────────────────────────────────────"
}

do_uninstall() {
  need_root
  rm -f "$SYSCTL_FILE" "$DISPATCHER"
  sysctl -w net.ipv4.ip_forward=0 >/dev/null || true
  ip rule del from "$KIDS_SUBNET" lookup "$RT_TABLE" 2>/dev/null || true
  ip route flush table "$RT_TABLE" 2>/dev/null || true
  iptables -t nat -D POSTROUTING -o "$UP_IFACE" -j MASQUERADE 2>/dev/null || true
  iptables -D FORWARD -i "$AP_IFACE" -o "$UP_IFACE" -j ACCEPT 2>/dev/null || true
  iptables -D FORWARD -i "$UP_IFACE" -o "$AP_IFACE" -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT 2>/dev/null || true
  [ -f "$DNSMASQ_CONF" ] && { sed -i "s/^dhcp-option=6,.*/dhcp-option=6,192.168.50.1/" "$DNSMASQ_CONF"; systemctl restart protectado-ap-dhcp.service 2>/dev/null || true; }
  echo "→ NAT/routage retirés. (Le Wi-Fi client $NETPLAN_WIFI est CONSERVÉ ; rm + 'netplan apply' pour l'ôter.)"
}

case "${1:-}" in
  wifi)      do_wifi ;;
  install)   do_install ;;
  status)    do_status ;;
  uninstall) need_root; do_uninstall ;;
  *) echo "Usage: sudo [BOX_SSID=… BOX_PASS=…] bash $0 {wifi|install|status|uninstall}"; exit 1 ;;
esac
