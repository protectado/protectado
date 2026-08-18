#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Arnaud Ortais
#
# protectado-boot.sh — Orchestrateur de POSTURE au démarrage (onboarding).
#   Lit config.json (source de vérité) : le boîtier est-il CONFIGURÉ ?
#     configuré  (config.json + "configured": true)  → posture GATEWAY (AP enfants + uplink + NAT)
#     NON conf.                                        → posture CONFIG  (assistant Protectado-Setup)
#   Détection par DÉCLARATION (le champ), jamais deviné d'après le matériel.
#
# Usage :
#   sudo bash protectado-boot.sh install     # pose le service (choix de posture au boot)
#   sudo bash protectado-boot.sh run          # (appelé par le service) applique la posture
#   sudo bash protectado-boot.sh status
#   sudo bash protectado-boot.sh reset        # efface l'état configuré → prochain boot = CONFIG
#   sudo bash protectado-boot.sh reset --full # sortie d'usine : efface config + uplink + états
#   sudo bash protectado-boot.sh uninstall
#
set -euo pipefail
export PATH="/usr/sbin:/sbin:/usr/bin:/bin:$PATH"

INSTALL_DIR="/opt/protectado"
CONFIG_PATH="$INSTALL_DIR/data/config.json"
CONFIG_AP="$INSTALL_DIR/bootstrap/config-ap.sh"
POSTURE_FILE="$INSTALL_DIR/data/posture.json"   # capacité détectée, lue par l'assistant
UNIT="/etc/systemd/system/protectado-boot.service"

C_OK=$'\033[32m'; C_NO=$'\033[31m'; C_Z=$'\033[0m'
die() { echo "${C_NO}ERREUR :${C_Z} $*" >&2; exit 1; }
need_root() { [ "$(id -u)" -eq 0 ] || die "à lancer en root (sudo)"; }

# Configuré ⟺ config.json existe ET "configured": true. (config.json = seule source de vérité)
is_configured() {
  [ -f "$CONFIG_PATH" ] || return 1
  python3 -c "import json,sys;sys.exit(0 if (json.load(open('$CONFIG_PATH')).get('configured') is True) else 1)" 2>/dev/null
}

# ── Détection de capacité GATEWAY ────────────────────────────────────────────
# Le mode gateway (AP enfants + NAT) n'a de sens QUE si le boîtier a à la fois :
#   • un uplink actif (Ethernet avec porteuse+IP, OU un wifi connecté en client), ET
#   • une interface wifi LIBRE (non utilisée comme uplink) pour porter l'AP enfants.
# Sinon → repli DNS-only (filtrage classique sur le réseau existant, sans AP captif).
# Décision prise ICI (root) ; jamais devinée par l'assistant sandboxé.
_wifi_ifaces() {
  local d
  for d in /sys/class/net/*/wireless; do
    [ -e "$d" ] && basename "$(dirname "$d")"
  done
}
_has_active_uplink() {
  local i n
  for i in /sys/class/net/*; do          # Ethernet : porteuse + IPv4
    n=$(basename "$i")
    case "$n" in eth*|en*)
      [ "$(cat "$i/carrier" 2>/dev/null || echo 0)" = 1 ] \
        && ip -4 addr show dev "$n" 2>/dev/null | grep -q 'inet ' && return 0 ;;
    esac
  done
  for n in $(_wifi_ifaces); do           # Wifi : client connecté
    iw dev "$n" link 2>/dev/null | grep -q 'Connected to' && return 0
  done
  return 1
}
_count_wifi()           { _wifi_ifaces | grep -c . || true; }
_count_wifi_connected() {
  local n c=0
  for n in $(_wifi_ifaces); do
    iw dev "$n" link 2>/dev/null | grep -q 'Connected to' && c=$((c+1))
  done
  echo "$c"
}
# Wifi libre = au moins une interface wifi non utilisée comme client uplink.
_has_free_wifi() { [ "$(( $(_count_wifi) - $(_count_wifi_connected) ))" -ge 1 ]; }

# BOOT — peut-on PROPOSER l'onboarding gateway (portail captif) ? L'uplink Wi-Fi de la box
# n'est pas encore établi (il naît de l'onboarding), donc on n'exige QUE le matériel :
# 2 radios wifi (une pour l'AP enfants, une pour se connecter à la box), dont une libre.
_gateway_boot_capable() { [ "$(_count_wifi)" -ge 2 ] && _has_free_wifi; }
# COMMIT (apply_configuration) — l'uplink DOIT être actif MAINTENANT : connexion active
# (filaire ou wifi) ET wifi libre pour l'AP. C'est la règle stricte, appliquée au « finir ».
_gateway_apply_capable() { _has_active_uplink && _has_free_wifi; }

# Imprime 'gateway'|'dns_only'. $1 = boot (défaut, écrit posture.json) | apply (règle stricte).
do_caps() {
  local scope="${1:-boot}" mode="dns_only"
  if [ "$scope" = apply ]; then
    _gateway_apply_capable && mode="gateway"
    echo "$mode"; return
  fi
  _gateway_boot_capable && mode="gateway"
  local up=false fw=false n; n=$(_count_wifi)
  _has_active_uplink && up=true
  _has_free_wifi && fw=true
  mkdir -p "$(dirname "$POSTURE_FILE")" 2>/dev/null || true
  printf '{"mode":"%s","uplink_active":%s,"free_wifi":%s,"wifi_count":%s}\n' \
    "$mode" "$up" "$fw" "$n" > "$POSTURE_FILE" 2>/dev/null || true
  echo "$mode"
}

do_run() {   # appelé par le service systemd au boot
  if is_configured; then
    logger -t protectado-boot "CONFIGURÉ → posture GATEWAY"
    systemctl start protectado-ap.service protectado-ap-dhcp.service 2>/dev/null || true
    # uplink (netplan wlan_up) + dispatcher NAT s'appliquent seuls (config-driven).
  else
    local mode; mode="$(do_caps)"
    if [ "$mode" = "gateway" ]; then
      logger -t protectado-boot "NON configuré + capable → posture CONFIG (assistant gateway captif)"
      systemctl stop protectado-ap.service protectado-ap-dhcp.service 2>/dev/null || true
      bash "$CONFIG_AP" up >/dev/null 2>&1 || logger -t protectado-boot "échec montée config-AP"
    else
      logger -t protectado-boot "NON configuré, gateway impossible (uplink/wifi libre absent) → onboarding DNS-only sur le LAN"
      # Pas d'AP captif : le dashboard reste joignable sur le réseau existant (IP:80).
      systemctl stop protectado-ap.service protectado-ap-dhcp.service 2>/dev/null || true
      bash "$CONFIG_AP" down >/dev/null 2>&1 || true
    fi
  fi
}

do_install() {
  need_root
  [ -x "$CONFIG_AP" ] || die "$CONFIG_AP introuvable (dépôt cloné ?)."
  cat > "$UNIT" <<EOF
[Unit]
Description=Protectado — orchestrateur de posture (config / gateway)
After=network.target pihole-FTL.service
Wants=network.target
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=$INSTALL_DIR/bootstrap/protectado-boot.sh run
[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  # L'orchestrateur CONTRÔLE l'AP enfants : couper leur auto-start (sinon ils montent
  # inconditionnellement et court-circuitent le choix de posture).
  systemctl disable protectado-ap.service protectado-ap-dhcp.service 2>/dev/null || true
  systemctl enable protectado-boot.service >/dev/null
  echo "→ Orchestrateur posé. Au boot : configured==true → GATEWAY, sinon CONFIG."
  is_configured && echo "  État actuel : ${C_OK}CONFIGURÉ${C_Z} (GATEWAY au prochain boot)" \
                || echo "  État actuel : ${C_NO}NON configuré${C_Z} (CONFIG au prochain boot)"
}

do_uninstall() {
  need_root
  systemctl disable --now protectado-boot.service 2>/dev/null || true
  rm -f "$UNIT"; systemctl daemon-reload
  systemctl enable protectado-ap.service protectado-ap-dhcp.service 2>/dev/null || true
  echo "→ Orchestrateur retiré ; auto-start des services gateway rétabli."
}

do_reset() {
  need_root
  if [ "${1:-}" = "--full" ]; then
    # Reset TOTAL (sortie d'usine) : efface config + uplink + états. Le prochain boot
    # repart comme un boîtier neuf → assistant, wlan_up libre, aucune ancienne valeur.
    rm -f "$CONFIG_PATH" \
          "$INSTALL_DIR/data/pending_config.json" \
          "$INSTALL_DIR/data/wifi_scan.json" \
          "$INSTALL_DIR/data/box_validation.json" \
          "$INSTALL_DIR/data/gateway_status.json"
    rm -f /etc/netplan/60-protectado-uplink.yaml
    netplan generate 2>/dev/null || true
    echo "→ Reset TOTAL : config, uplink Wi-Fi et états effacés. 'sudo reboot' → assistant (neuf)."
  elif [ -f "$CONFIG_PATH" ]; then
    # Reset simple : garde les valeurs, repasse juste en posture CONFIG.
    python3 -c "import json;p='$CONFIG_PATH';d=json.load(open(p));d['configured']=False;json.dump(d,open(p,'w'),indent=2)"
    echo "→ configured=false. 'sudo reboot' → posture CONFIG (valeurs conservées)."
  else
    echo "→ Pas de config.json — déjà en état non configuré."
  fi
}

do_status() {
  echo "── Orchestrateur de posture ──────────────────────────────────────"
  is_configured && echo "  état déclaré : ${C_OK}CONFIGURÉ${C_Z} → GATEWAY" \
                || echo "  état déclaré : ${C_NO}NON configuré${C_Z} → CONFIG"
  local up=no fw=no n; n=$(_count_wifi)
  _has_active_uplink && up=oui; _has_free_wifi && fw=oui
  echo "  matériel : radios wifi=$n · wifi libre=$fw · uplink actif=$up"
  echo "  → boot : $( _gateway_boot_capable && echo 'gateway (onboarding captif)' || echo 'dns_only (LAN)' )"
  echo "  → commit stricte : $( _gateway_apply_capable && echo gateway || echo 'refusé (dns_only)' )"
  local boot_en ap_en
  boot_en="$(systemctl is-enabled protectado-boot.service 2>/dev/null || true)"; boot_en="${boot_en:-absent}"
  ap_en="$(systemctl is-enabled protectado-ap.service 2>/dev/null || true)"; ap_en="${ap_en:-absent}"
  echo "  service protectado-boot : $boot_en"
  echo "  auto-start AP enfants   : $ap_en (doit être 'disabled' — piloté par l'orchestrateur)"
  echo "──────────────────────────────────────────────────────────────────"
}

case "${1:-}" in
  run)       do_run ;;
  install)   do_install ;;
  uninstall) need_root; do_uninstall ;;
  reset)     do_reset "${2:-}" ;;
  status)    do_status ;;
  caps)      do_caps "${2:-boot}" ;;
  *) echo "Usage: sudo bash $0 {install|run|status|caps|reset [--full]|uninstall}"; exit 1 ;;
esac
