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
_eth_carrier() {                        # câble branché ET lien établi (lecture instantanée)
  local i n
  for i in /sys/class/net/*; do
    n=$(basename "$i")
    case "$n" in eth*|en*)
      [ "$(cat "$i/carrier" 2>/dev/null || echo 0)" = 1 ] && return 0 ;;
    esac
  done
  return 1
}
_eth_active() {                         # porteuse + IPv4 = uplink filaire exploitable
  local i n
  for i in /sys/class/net/*; do
    n=$(basename "$i")
    case "$n" in eth*|en*)
      [ "$(cat "$i/carrier" 2>/dev/null || echo 0)" = 1 ] \
        && ip -4 addr show dev "$n" 2>/dev/null | grep -q 'inet ' && return 0 ;;
    esac
  done
  return 1
}
_has_active_uplink() {
  local n
  _eth_active && return 0                # Ethernet : porteuse + IPv4
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

# Le portail captif ne vaut que si l'assistant répond DERRIÈRE lui. Un AP impeccable
# devant un dashboard mort donne exactement le symptôme le plus trompeur du produit :
# le téléphone s'associe, obtient son bail, ne reçoit rien sur le :80, conclut « pas
# d'Internet » et quitte le réseau au bout de quelques secondes. Vu de l'extérieur, le
# portail « ne monte pas », alors que hostapd et dnsmasq vont parfaitement bien.
# L'agent démarre en parallèle de ce service, d'où l'attente avant de conclure.
PORTAL_URL="http://127.0.0.1:8080/onboarding"   # port : cf. PORTAL_PORT de config-ap.sh
_wait_portal() {
  local i
  for i in $(seq 1 30); do
    curl -fsS -o /dev/null --max-time 2 "$PORTAL_URL" 2>/dev/null && return 0
    sleep 1
  done
  return 1
}

# ── AUTO-RÉPARATION au boot (posture CONFIG UNIQUEMENT) ──────────────────────
# Tant que le boîtier attend d'être configuré, il ne protège aucune famille et n'a donc
# rien à perdre à se remettre à jour tout seul : un parent dont l'assistant ne s'ouvre
# pas branche un câble Ethernet, redémarre, et le boîtier va chercher la version
# corrigée. C'est le SEUL rattrapage possible pour un défaut qui empêche justement
# d'atteindre l'interface par laquelle on aurait déclenché la mise à jour.
#
# Jamais sur un boîtier CONFIGURÉ : là, une mise à jour non demandée au démarrage
# couperait le filtrage d'une famille sans que personne ne l'ait décidé. Le chemin
# reste protectado-update, déclenché par le parent.
SELFHEAL_STATE="$INSTALL_DIR/data/selfheal.state"   # "<sha distant> <tentatives>"
SELFHEAL_MAX=3
BOOTSTRAP="$INSTALL_DIR/bootstrap/bootstrap.sh"

# git en root sur un dépôt possédé par le service user : « dubious ownership » sans
# ce -c. On ne touche PAS à la config globale pour autant, la portée reste l'appel.
_repo_git() { git -C "$INSTALL_DIR" -c safe.directory="$INSTALL_DIR" "$@" 2>/dev/null; }

# Branche suivie par CETTE installation, jamais une valeur en dur : même ordre de
# priorité que bootstrap.sh et protectado-update.sh, pour qu'une machine de test sur
# « main » ne se fasse pas rapatrier sur « stable » par une réparation automatique.
_tracked_branch() {
  local b=""
  [ -s "$INSTALL_DIR/data/branch" ] && b="$(tr -d '[:space:]' < "$INSTALL_DIR/data/branch")"
  [ -n "$b" ] || b="$(_repo_git rev-parse --abbrev-ref HEAD || true)"
  case "$b" in ''|HEAD) b="stable" ;; esac
  printf '%s' "$b"
}

# La PORTEUSE est lue en premier parce qu'elle est instantanée : sans câble on sort
# aussitôt et un boîtier sans Ethernet ne paie pas une seconde de démarrage en plus.
# Ce n'est qu'avec un câble branché qu'on accorde du temps au DHCP.
_wait_eth() {
  local i
  _eth_carrier || return 1
  for i in $(seq 1 30); do
    _eth_active && return 0
    sleep 1
  done
  return 1
}

# Tentatives déjà faites POUR CE sha distant. Compter par version cible, et non
# globalement, est ce qui rend le garde-fou utilisable : on cesse de s'acharner sur une
# version qui ne répare rien, mais toute nouvelle publication réarme la réparation.
_selfheal_attempts() {
  local sha="$1" f_sha="" f_n=""
  [ -s "$SELFHEAL_STATE" ] && read -r f_sha f_n < "$SELFHEAL_STATE" || true
  case "${f_n:-}" in ''|*[!0-9]*) f_n=0 ;; esac
  [ "$f_sha" = "$sha" ] && printf '%s' "$f_n" || printf '0'
}

do_selfheal() {
  local branch url remote local_sha tries
  [ -f "$BOOTSTRAP" ] || return 0
  command -v git >/dev/null || return 0

  if ! _wait_eth; then
    logger -t protectado-boot "auto-réparation : pas d'Ethernet actif → ignorée (brancher un câble puis redémarrer force une mise à jour)"
    return 0
  fi

  branch="$(_tracked_branch)"
  url="$(git config -f "$INSTALL_DIR/.git/config" --get remote.origin.url 2>/dev/null || true)"
  [ -n "$url" ] || { logger -t protectado-boot "auto-réparation : dépôt d'origine inconnu → ignorée"; return 0; }

  remote="$(timeout 30 git ls-remote "$url" "refs/heads/$branch" 2>/dev/null | awk 'NR==1{print $1}')"
  if [ -z "$remote" ]; then
    logger -t protectado-boot "auto-réparation : dépôt injoignable (branche $branch) → ignorée, le boîtier garde sa version"
    return 0
  fi
  local_sha="$(_repo_git rev-parse HEAD || echo unknown)"

  # Deux motifs de redéploiement, et deux seulement : une version plus récente existe,
  # ou l'assistant ne répond pas alors qu'on est déjà à jour (artefacts déployés dans
  # /etc désynchronisés du code, dépendance cassée, service mort).
  if [ "$local_sha" = "$remote" ] && _wait_portal; then
    logger -t protectado-boot "auto-réparation : déjà à jour (${remote:0:8}) et assistant joignable → rien à faire"
    return 0
  fi

  tries="$(_selfheal_attempts "$remote")"
  if [ "$tries" -ge "$SELFHEAL_MAX" ]; then
    logger -t protectado-boot "auto-réparation : $tries tentatives déjà faites sur ${remote:0:8} sans succès → abandon (une nouvelle publication réarmera)"
    return 0
  fi
  printf '%s %s\n' "$remote" "$((tries + 1))" > "$SELFHEAL_STATE" 2>/dev/null || true

  if [ "$local_sha" = "$remote" ]; then
    logger -t protectado-boot "auto-réparation : à jour (${remote:0:8}) mais assistant muet → redéploiement (tentative $((tries + 1))/$SELFHEAL_MAX)"
  else
    logger -t protectado-boot "auto-réparation : ${local_sha:0:8} → ${remote:0:8} sur $branch (tentative $((tries + 1))/$SELFHEAL_MAX)"
  fi

  # Garde de 15 min : un bootstrap qui pend ne doit pas retenir le démarrage sans fin.
  # bootstrap.sh détecte l'installation existante et bascule en mise à jour ; il ne
  # redemande rien à personne et ne redémarre jamais la machine de lui-même.
  if timeout 900 bash "$BOOTSTRAP" >> /var/log/protectado-bootstrap.log 2>&1; then
    logger -t protectado-boot "auto-réparation : bootstrap terminé"
  else
    logger -t protectado-boot "auto-réparation : bootstrap en échec ou interrompu (voir /var/log/protectado-bootstrap.log)"
  fi

  if _wait_portal; then
    rm -f "$SELFHEAL_STATE" 2>/dev/null || true   # réparé : le compteur est réarmé
    logger -t protectado-boot "auto-réparation : assistant joignable après redéploiement"
  else
    logger -t protectado-boot "ALERTE : assistant toujours muet après auto-réparation — systemctl status protectado-agent"
  fi
}

do_run() {   # appelé par le service systemd au boot
  if is_configured; then
    logger -t protectado-boot "CONFIGURÉ → posture GATEWAY"
    systemctl start protectado-ap.service protectado-ap-dhcp.service 2>/dev/null || true
    # uplink (netplan wlan_up) + dispatcher NAT s'appliquent seuls (config-driven).
  else
    local mode out
    do_selfheal            # AVANT do_caps : la posture doit refléter le code d'après
    mode="$(do_caps)"
    if [ "$mode" = "gateway" ]; then
      logger -t protectado-boot "NON configuré + capable → posture CONFIG (assistant gateway captif)"
      systemctl stop protectado-ap.service protectado-ap-dhcp.service 2>/dev/null || true
      # Surtout PAS de >/dev/null 2>&1 ici : la sortie de config-ap.sh est le seul
      # endroit où l'on apprend POURQUOI l'AP n'est pas monté (carte absente, hostapd
      # qui refuse le canal, apt indisponible sans uplink…). La jeter transforme une
      # panne explicable en mystère.
      if out="$(bash "$CONFIG_AP" up 2>&1)"; then
        logger -t protectado-boot "config-AP monté"
        if _wait_portal; then
          logger -t protectado-boot "portail captif opérationnel (assistant joignable)"
        else
          logger -t protectado-boot "ALERTE : config-AP monté mais l'assistant NE RÉPOND PAS sur $PORTAL_URL — le téléphone verra un réseau sans page. Vérifier : systemctl status protectado-agent"
        fi
      else
        logger -t protectado-boot "échec montée config-AP :"
        printf '%s\n' "$out" | logger -t protectado-boot
      fi
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
# L'orchestrateur tourne depuis une COPIE dans /run, et non depuis le dépôt. En posture
# CONFIG il peut relancer bootstrap, dont le « git reset --hard » réécrit ce script
# PENDANT son exécution : bash relit le fichier par décalage d'octets après chaque
# commande, et se retrouverait à exécuter le milieu d'une autre ligne. La copie est
# hors d'atteinte du reset ; les scripts qu'elle appelle (config-ap.sh) sont, eux,
# lus depuis $INSTALL_DIR, donc déjà à jour quand elle les invoque.
ExecStart=/bin/bash -c 'install -m 0755 $INSTALL_DIR/bootstrap/protectado-boot.sh /run/protectado-boot.run && exec /run/protectado-boot.run run'
# Le défaut (90 s) tuerait une auto-réparation en plein bootstrap, au pire moment
# possible. La garde réelle est le « timeout 900 » posé autour de bootstrap.
TimeoutStartSec=1800
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
