#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Arnaud Ortais
#
# ap-persist.sh — AP enfants PERSISTANT (Ubuntu, systemd-networkd, SANS NetworkManager).
#   Pose la règle udev de nommage (wlan_ap / wlan_up), les confs hostapd + dnsmasq,
#   un fichier systemd-networkd qui laisse wlan_ap NON géré (pour que hostapd la prenne),
#   et deux services systemd. Au boot, l'AP remonte seul. AP-only : PAS de NAT/uplink ici.
#   Ne touche jamais eth0 (admin, géré par netplan/networkd) ni wlan_up (futur uplink).
#
# Usage :
#   sudo bash ap-persist.sh install     # pose tout, active les services (REBOOT ensuite)
#   sudo bash ap-persist.sh status      # vérifie l'état après reboot
#   sudo bash ap-persist.sh uninstall   # retire tout
#
set -euo pipefail
export PATH="/usr/sbin:/sbin:/usr/bin:/bin:$PATH"

# shellcheck source=net-common.sh
. "$(dirname "${BASH_SOURCE[0]}")/net-common.sh"

# SSID/clé enfants : config.json (network.kids) > variables d'env > défauts de test.
SSID="${KIDS_SSID:-Protectado-Test}"
PASSPHRASE="${KIDS_KEY:-123456789}"
if [ -f /opt/protectado/data/config.json ]; then
  _kids="$(python3 - <<'PY' 2>/dev/null
import json
try:
    k = (json.load(open('/opt/protectado/data/config.json')).get('network') or {}).get('kids') or {}
    print(k.get('ssid','')); print(k.get('key',''))
except Exception:
    print(); print()
PY
)"
  _s="$(printf '%s' "$_kids" | sed -n 1p)"; _k="$(printf '%s' "$_kids" | sed -n 2p)"
  [ -n "$_s" ] && SSID="$_s"; [ -n "$_k" ] && PASSPHRASE="$_k"
fi
AP_IP="192.168.50.1"
SUBNET_CIDR="192.168.50.1/24"
DHCP_FROM="192.168.50.50"
DHCP_TO="192.168.50.150"
# Domaine réglementaire : config.network.country (assistant), sinon déduit du noyau ou
# du fuseau horaire. Vide si indéterminable : country_code et ieee80211d sont alors omis
# (hostapd rejette la valeur littérale "00").
# Plus de "FR" en dur — cf. net-common.sh:pt_country().
COUNTRY="$(pt_country)"
HW_MODE="a"      # 5 GHz (choix retenu — enfants proches du Pi)
CHANNEL="36"     # canal 36 = 5 GHz non-DFS, propre
# …sauf si le canal 36 n'est pas autorisé dans le domaine retenu : on retombe alors sur
# 2,4 GHz plutôt que de laisser hostapd refuser de démarrer (cf. pt_pick_ap_band()).

UDEV_RULE="/etc/udev/rules.d/72-protectado-netnames.rules"
NETWORKD_UNMANAGED="/etc/systemd/network/25-protectado-ap.network"
HOSTAPD_CONF="/etc/protectado/hostapd-ap.conf"
AP_PRESTART="/etc/protectado/ap-prestart.sh"
INSTALL_BOOTSTRAP="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DNSMASQ_CONF="/etc/protectado/dnsmasq-ap.conf"
AP_UNIT="/etc/systemd/system/protectado-ap.service"
DHCP_UNIT="/etc/systemd/system/protectado-ap-dhcp.service"

C_OK=$'\033[32m'; C_NO=$'\033[31m'; C_Z=$'\033[0m'
ok()  { echo "  ${C_OK}[ OK ]${C_Z} $*"; }
no()  { echo "  ${C_NO}[ !! ]${C_Z} $*"; }
die() { echo "${C_NO}ERREUR :${C_Z} $*" >&2; exit 1; }
need_root() { [ "$(id -u)" -eq 0 ] || die "à lancer en root (sudo)"; }

do_install() {
  need_root
  command -v hostapd >/dev/null || apt-get install -y hostapd
  command -v dnsmasq >/dev/null || apt-get install -y dnsmasq
  command -v iw >/dev/null || apt-get install -y iw       # absent d'Ubuntu Server par défaut
  # Neutraliser les services par défaut : on pilote nos propres units.
  systemctl unmask hostapd 2>/dev/null || true
  systemctl disable --now hostapd dnsmasq 2>/dev/null || true

  # 1) Nommage déterministe des interfaces (aucun MAC en dur).
  cat > "$UDEV_RULE" <<'EOF'
# Protectado — nommage déterministe des interfaces Wi-Fi (aucun MAC codé en dur).
# AP enfants : carte USB à chipset MT7612U (pilote mt76x2u). On matche le PILOTE, pas
# seulement le couple VID/PID de l'Alfa AWUS036ACM : toute carte du même chipset
# convient, et n'appairer que 0e8d:7612 les excluait sans raison. La règle VID/PID est
# conservée en second rideau pour une carte que le pilote n'aurait pas encore réclamée.
SUBSYSTEM=="net", ACTION=="add", DRIVERS=="mt76x2u", NAME="wlan_ap"
SUBSYSTEM=="net", ACTION=="add", ATTRS{idVendor}=="0e8d", ATTRS{idProduct}=="7612", NAME="wlan_ap"
# Uplink : radio interne du Pi (Infineon/Cypress, pilote brcmfmac).
SUBSYSTEM=="net", ACTION=="add", DRIVERS=="brcmfmac", NAME="wlan_up"
EOF

  # 2) systemd-networkd laisse wlan_ap NON géré → hostapd peut la piloter librement.
  #    (eth0 reste géré par netplan/networkd ; wlan_up sera géré au jalon uplink.)
  mkdir -p "$(dirname "$NETWORKD_UNMANAGED")"
  cat > "$NETWORKD_UNMANAGED" <<'EOF'
[Match]
Name=wlan_ap

[Link]
Unmanaged=yes
EOF

  # 3) hostapd (config validée au jalon 0a).
  mkdir -p /etc/protectado
  cat > "$HOSTAPD_CONF" <<EOF
interface=wlan_ap
driver=nl80211
ssid=$SSID
${COUNTRY:+country_code=$COUNTRY}
${COUNTRY:+ieee80211d=1}
hw_mode=$HW_MODE
channel=$CHANNEL
ieee80211n=1
auth_algs=1
wmm_enabled=1
wpa=2
wpa_key_mgmt=WPA-PSK
rsn_pairwise=CCMP
wpa_passphrase=$PASSPHRASE
EOF
  chmod 600 "$HOSTAPD_CONF"   # contient la clé en clair

  # 4) dnsmasq : DHCP seul (port=0 → aucune interférence avec Pi-hole/FTL ni resolved).
  cat > "$DNSMASQ_CONF" <<EOF
port=0
interface=wlan_ap
bind-interfaces
dhcp-range=$DHCP_FROM,$DHCP_TO,255.255.255.0,12h
dhcp-option=3,$AP_IP
dhcp-option=6,$AP_IP
dhcp-authoritative
dhcp-leasefile=/var/lib/misc/protectado-ap.leases
EOF

  # 4bis) Pré-démarrage : le domaine réglementaire et la bande utilisable ne sont
  #       connus qu'AU DÉMARRAGE — à l'installation, wlan_ap n'existe pas encore (le
  #       renommage udev n'a pas eu lieu) et le pays peut ne pas être encore choisi.
  #       Ce script réapplique country_code / hw_mode / channel juste avant hostapd.
  cat > "$AP_PRESTART" <<EOF
#!/usr/bin/env bash
# Généré par ap-persist.sh — ne pas éditer à la main.
set -euo pipefail
export PATH="/usr/sbin:/sbin:/usr/bin:/bin:\$PATH"
. "$INSTALL_BOOTSTRAP/net-common.sh"

CONF="$HOSTAPD_CONF"
country="\$(pt_country)"
pt_apply_regdom "\$country"
band="\$(pt_pick_ap_band wlan_ap)"
hw="\${band%% *}"; ch="\${band##* }"

sed -i -e "s/^hw_mode=.*/hw_mode=\$hw/" \\
       -e "s/^channel=.*/channel=\$ch/" "\$CONF"

# country_code : on retire les lignes existantes puis on ne les remet QUE si le pays est
# connu. hostapd rejette « country_code=00 » comme « country_code= » : dans le doute, la
# bonne réponse est l'absence de ligne, pas une valeur inventée.
sed -i -e "/^country_code=/d" -e "/^ieee80211d=/d" "\$CONF"
if [ -n "\$country" ]; then
    printf 'country_code=%s\\nieee80211d=1\\n' "\$country" >> "\$CONF"
fi
echo "[protectado-ap] domaine=\${country:-non renseigné} bande=\$hw canal=\$ch"
EOF
  chmod 755 "$AP_PRESTART"

  # 5) Service AP : hostapd prend wlan_ap et la remonte en mode AP.
  cat > "$AP_UNIT" <<EOF
[Unit]
Description=Protectado — Point d'accès enfants (hostapd sur wlan_ap)
Wants=sys-subsystem-net-devices-wlan_ap.device
After=sys-subsystem-net-devices-wlan_ap.device systemd-networkd.service
[Service]
ExecStartPre=$AP_PRESTART
ExecStart=/usr/sbin/hostapd $HOSTAPD_CONF
Restart=always
RestartSec=3
[Install]
WantedBy=multi-user.target
EOF

  # 6) Service DHCP : pose l'IP passerelle (après que hostapd a monté l'AP) puis dnsmasq.
  cat > "$DHCP_UNIT" <<EOF
[Unit]
Description=Protectado — DHCP réseau enfants (dnsmasq sur wlan_ap)
Requires=protectado-ap.service
After=protectado-ap.service
[Service]
ExecStartPre=/bin/sh -c 'for i in \$(seq 1 20); do ip link show wlan_ap 2>/dev/null | grep -q "state UP" && break; sleep 0.5; done; ip addr replace $SUBNET_CIDR dev wlan_ap'
ExecStart=/usr/sbin/dnsmasq -k --conf-file=$DNSMASQ_CONF
Restart=always
RestartSec=3
[Install]
WantedBy=multi-user.target
EOF

  systemctl daemon-reload
  networkctl reload 2>/dev/null || true      # applique le fichier .network SANS flapper eth0
  # En BOOT_ONLY (finalisation onboarding), l'orchestrateur pilote l'AP → ne PAS enabler
  # (sinon auto-start en plus de l'orchestrateur, ce qui casserait la posture CONFIG).
  [ "${BOOT_ONLY:-0}" = "1" ] || systemctl enable protectado-ap.service protectado-ap-dhcp.service >/dev/null
  echo "→ Installé : udev + networkd(unmanaged) + hostapd/dnsmasq + services (activés au boot)."
  echo "${C_OK}REBOOT requis${C_Z} pour appliquer proprement le renommage udev + les services :"
  echo "    sudo reboot"
  echo "  puis : sudo bash $0 status"
}

do_status() {
  echo "── AP enfants persistant (Ubuntu/networkd) ───────────────────────"
  if ip link show wlan_ap >/dev/null 2>&1; then ok "interface wlan_ap présente (renommage udev)"; else no "wlan_ap absente (udev/rename ?)"; fi
  if ip link show wlan_up >/dev/null 2>&1; then ok "interface wlan_up présente (radio interne)"; else no "wlan_up absente"; fi
  for s in protectado-ap protectado-ap-dhcp; do
    if systemctl is-active --quiet "$s"; then ok "$s actif"; else no "$s inactif"; fi
  done
  if iw dev wlan_ap info 2>/dev/null | grep -q 'type AP'; then ok "wlan_ap en type AP"; else no "wlan_ap pas en type AP"; fi
  if ip -brief addr show wlan_ap 2>/dev/null | grep -q "${AP_IP}/24"; then ok "IP $SUBNET_CIDR posée"; else no "IP passerelle absente"; fi
  if ip -brief addr show eth0 2>/dev/null | grep -q UP; then ok "eth0 (admin) intact"; else no "eth0 non UP"; fi
  echo "  Baux DHCP :"; sed 's/^/        /' /var/lib/misc/protectado-ap.leases 2>/dev/null || echo "        (aucun pour l'instant)"
  echo "──────────────────────────────────────────────────────────────────"
}

do_uninstall() {
  need_root
  systemctl disable --now protectado-ap-dhcp.service protectado-ap.service 2>/dev/null || true
  rm -f "$AP_UNIT" "$DHCP_UNIT" "$HOSTAPD_CONF" "$DNSMASQ_CONF" "$UDEV_RULE" "$NETWORKD_UNMANAGED" "$AP_PRESTART"
  systemctl daemon-reload
  networkctl reload 2>/dev/null || true
  echo "→ Désinstallé. REBOOT conseillé pour revenir aux noms d'interface par défaut."
}

case "${1:-}" in
  install)   do_install ;;
  status)    do_status ;;
  uninstall) need_root; do_uninstall ;;
  *) echo "Usage: sudo bash $0 {install|status|uninstall}"; exit 1 ;;
esac
