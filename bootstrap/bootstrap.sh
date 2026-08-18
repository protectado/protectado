#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Arnaud Ortais
# Dual-licensed: AGPL-3.0 (open source) or Commercial License — see LICENSE and LICENSE-COMMERCIAL.
# bootstrap.sh — Installation complète de Protectado sur Raspberry Pi vierge
#
# Usage (depuis le Pi, en SSH) :
#   curl -sSL https://raw.githubusercontent.com/protectado/protectado/main/bootstrap/bootstrap.sh | sudo bash
#
# Prérequis :
#   - Ubuntu Server 64-bit (le noyau linux-raspi fournit Landlock, requis par la sandbox nono)
#   - WiFi configuré et actif (via Raspberry Pi Imager)
#   - SSH actif

set -euo pipefail
trap 'echo ""; echo "❌  Erreur à la ligne $LINENO — arrêt."; echo ""; tail -5 "$LOG_FILE" 2>/dev/null | sed "s/^/    /"; echo ""; echo "    Logs complets : $LOG_FILE"; exit 1' ERR

# ── Configuration ────────────────────────────────────────────────────────────
REPO_URL="https://github.com/protectado/protectado.git"
INSTALL_DIR="/opt/protectado"
BRANCH_FILE="$INSTALL_DIR/data/branch"

# Branche suivie par cette installation, par ordre de priorité :
#   1. $PROTECTADO_BRANCH — surcharge explicite (machine de test sur « main ») ;
#   2. data/branch        — choix mémorisé lors d'une installation précédente, pour
#                           qu'une réinstallation ne fasse pas changer de branche ;
#   3. la branche DÉJÀ extraite dans /opt/protectado — une installation antérieure à
#                           data/branch tourne sur « main » : la basculer ailleurs sans
#                           le dire serait un changement de version en douce, et le
#                           « fetch » échoue si la branche visée n'est pas publiée.
#                           On ne déplace jamais une machine qui fonctionne ;
#   4. « stable »        — défaut des installations NEUVES. Promue MANUELLEMENT depuis
#                           « main », de sorte qu'une régression poussée le soir ne casse
#                           pas tous les foyers au réveil. Le rollback automatique ne
#                           couvre que le cas où le service ne redémarre pas : une
#                           régression fonctionnelle qui laisse l'agent actif passerait
#                           au travers.
if [ -n "${PROTECTADO_BRANCH:-}" ]; then
  BRANCH="$PROTECTADO_BRANCH"
elif [ -s "$BRANCH_FILE" ]; then
  BRANCH="$(tr -d '[:space:]' < "$BRANCH_FILE")"
elif [ -d "$INSTALL_DIR/.git" ]; then
  BRANCH="$(git -C "$INSTALL_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
  case "$BRANCH" in ''|HEAD) BRANCH="stable" ;; esac
else
  BRANCH="stable"
fi
LOG_FILE="/var/log/protectado-bootstrap.log"
INFO_FILE="/tmp/fw-setup-info.txt"
FW_PORT=8080
BACKUP_DIR="/opt/protectado-backup-$(date '+%Y%m%d-%H%M%S')"

# Utilisateur propriétaire des fichiers et sous lequel tourne le dashboard.
# Par défaut celui qui a lancé sudo — MAIS sur une installation existante, l'utilisateur
# déjà en place fait autorité, jamais le shell courant. Lancé depuis un shell root de
# debug (sans sudo), SUDO_USER est vide : on aurait REAL_USER=root, donc un
# « chown -R root:root /opt/protectado » ET une unité systemd régénérée en « User=root ».
# Autrement dit un debug manuel ferait perdre la séparation de privilèges et empêcherait
# l'updater automatique (qui tourne en tant que l'utilisateur du service) d'écrire.
REAL_USER="${SUDO_USER:-root}"
_installed_user="$(systemctl show -p User --value protectado-agent 2>/dev/null || true)"
if [ -n "$_installed_user" ] && [ "$_installed_user" != "root" ]; then
  REAL_USER="$_installed_user"            # 1) l'unité systemd déjà installée
elif [ -d "$INSTALL_DIR" ]; then
  _dir_owner="$(stat -c %U "$INSTALL_DIR" 2>/dev/null || true)"
  if [ -n "$_dir_owner" ] && [ "$_dir_owner" != "root" ]; then
    REAL_USER="$_dir_owner"               # 2) le propriétaire de l'installation
  fi
fi

# ── Helpers ──────────────────────────────────────────────────────────────────
log()  { echo "$(date '+%H:%M:%S') $*" | tee -a "$LOG_FILE"; }
ok()   { log "   ✓ $*"; }
step() { echo ""; log "→ [$1/$TOTAL_STEPS] $2"; }

# Enregistre la branche suivie et la version déployée dans data/ — hors versionnement,
# donc jamais écrasé par un « git reset --hard ». Deux usages :
#   - data/branch  : relu par l'updater, pour qu'une machine ne change jamais de branche
#                    toute seule ;
#   - data/version.json : lu par le tableau de bord et la page de connexion. L'agent est
#                    sandboxé (nono) et « git » n'est pas dans son profil : il ne peut pas
#                    interroger le dépôt lui-même, d'où ce fichier écrit par les scripts
#                    d'installation et de mise à jour, qui tournent hors sandbox.
record_version() {
  local dir="$1" branch="$2" commit="${3:-}"
  mkdir -p "$dir/data" 2>/dev/null || return 0
  printf '%s\n' "$branch" > "$dir/data/branch" 2>/dev/null || true
  [ -n "$commit" ] || commit="$(cd "$dir" && git rev-parse --short HEAD 2>/dev/null || echo "")"
  printf '{"commit": "%s", "branch": "%s", "updated_at": "%s"}\n' \
    "$commit" "$branch" "$(date -Is 2>/dev/null || date)" \
    > "$dir/data/version.json" 2>/dev/null || true
}
TOTAL_STEPS=7

# ── Détection installation existante ─────────────────────────────────────────
# Une installation réelle a un venv Python, pas seulement un dépôt git cloné.

# Git sur $INSTALL_DIR : TOUJOURS sous le propriétaire du dépôt, jamais en root aveuglément.
#   1) bootstrap tourne en root et termine par « chown -R $REAL_USER », donc l'exécution
#      root SUIVANTE se heurte à « fatal: detected dubious ownership » (git refuse un dépôt
#      possédé par un autre utilisateur) ;
#   2) surtout, un git lancé en root laisserait dans .git des objets root-owned que
#      l'updater automatique — qui tourne, lui, en tant que $REAL_USER via
#      /usr/local/sbin/protectado-update — ne pourrait plus écrire. La mise à jour de
#      production casserait à cause d'un debug manuel.
# Le clonage initial reste en root (le répertoire n'existe pas encore), suivi du chown.
repo_git() {
  local owner
  owner="$(stat -c %U "$INSTALL_DIR/.git" 2>/dev/null || echo root)"
  if [ "$owner" = "$(id -un)" ] || ! command -v sudo >/dev/null; then
    git -C "$INSTALL_DIR" "$@"
  else
    sudo -u "$owner" git -C "$INSTALL_DIR" "$@"
  fi
}

detect_existing() {
  systemctl is-active  --quiet protectado-agent 2>/dev/null && return 0
  systemctl is-enabled --quiet protectado-agent 2>/dev/null && return 0
  [ -d "$INSTALL_DIR/.venv" ] && return 0
  return 1
}

run_update() {
  echo ""
  echo "╔══════════════════════════════════════════════════╗"
  echo "║     Protectado — Installation existante         ║"
  echo "║               Mise à jour en cours...           ║"
  echo "╚══════════════════════════════════════════════════╝"
  echo ""
  log "Installation existante détectée — passage en mode mise à jour"

  cd "$INSTALL_DIR"
  LOCAL=$(repo_git rev-parse HEAD 2>/dev/null || echo "unknown")

  # Migration one-shot : anciens fichiers à la racine → data/
  mkdir -p "$INSTALL_DIR/data"
  [ -f "$INSTALL_DIR/config.json" ]    && mv "$INSTALL_DIR/config.json"    "$INSTALL_DIR/data/config.json"   && ok "Migration config.json → data/"
  [ -f "$INSTALL_DIR/protectado.db" ] && mv "$INSTALL_DIR/protectado.db" "$INSTALL_DIR/data/protectado.db" && ok "Migration protectado.db → data/"

  # Sauvegarde datée dans un répertoire persistant (hors /tmp)
  mkdir -p "$BACKUP_DIR"
  [ -f "$INSTALL_DIR/data/config.json" ]    && cp "$INSTALL_DIR/data/config.json"    "$BACKUP_DIR/config.json"   && ok "config.json    → $BACKUP_DIR"
  [ -f "$INSTALL_DIR/data/protectado.db" ] && cp "$INSTALL_DIR/data/protectado.db" "$BACKUP_DIR/protectado.db" && ok "protectado.db → $BACKUP_DIR"

  # Mise à jour du code
  log "   Récupération des mises à jour..."
  if repo_git fetch origin "$BRANCH" 2>&1 | tee -a "$LOG_FILE"; then
    REMOTE=$(repo_git rev-parse "origin/$BRANCH" 2>/dev/null || echo "")
    if [ -n "$REMOTE" ] && [ "$LOCAL" = "$REMOTE" ]; then
      ok "Déjà à jour (${LOCAL:0:8})"
    else
      # Le dépôt public est publié par SNAPSHOT : un commit orphelin poussé en --force.
      # L'historique distant est donc réécrit à chaque publication → le clone local
      # DIVERGE et « git pull » échoue net (« Need to specify how to reconcile
      # divergent branches »). /opt/protectado est une CIBLE DE DÉPLOIEMENT, jamais un
      # dépôt de travail : le distant fait autorité, on s'aligne dessus sans fusionner.
      # Les fichiers ignorés (data/config.json, *.db, .venv/) ne sont pas touchés par
      # reset --hard ; config.json est de toute façon sauvegardé/restauré ci-dessus.
      repo_git checkout --quiet -B "$BRANCH" >> "$LOG_FILE" 2>&1 \
        || log "   ⚠ impossible de se placer sur '$BRANCH'"
      if repo_git reset --hard --quiet "origin/$BRANCH" >> "$LOG_FILE" 2>&1; then
        ok "Code aligné sur origin/$BRANCH : ${LOCAL:0:8} → ${REMOTE:0:8}"
        record_version "$INSTALL_DIR" "$BRANCH" "${REMOTE:0:7}"
      else
        log "   ⚠ alignement sur origin/$BRANCH échoué — code local conservé"
      fi
    fi
  else
    log "   ⚠ fetch échoué (réseau ?) — utilisation du code local"
  fi

  # Restaurer config.json (jamais écrasé par git)
  [ -f "$BACKUP_DIR/config.json" ] && cp "$BACKUP_DIR/config.json" "$INSTALL_DIR/data/config.json"

  # Dépendances Python
  log "   Mise à jour des dépendances Python..."
  "$INSTALL_DIR/.venv/bin/pip" install -q --upgrade -r "$INSTALL_DIR/requirements.txt" >> "$LOG_FILE" 2>&1
  ok "Dépendances Python"

  # Migration base de données
  cd "$INSTALL_DIR"
  .venv/bin/python -c "import database; database.init_db()" >> "$LOG_FILE" 2>&1
  ok "Migration base de données"

  # Rétablir les permissions — git/pip en root créent des objets root-owned,
  # ce qui bloque les mises à jour suivantes via le script systemd (qui tourne en SVC_USER)
  chown -R "$REAL_USER:$REAL_USER" "$INSTALL_DIR"
  ok "Permissions rétablies ($REAL_USER)"

  # File d'actions : le groupe et l'appartenance étaient posés UNIQUEMENT à l'installation
  # initiale (step4_services), jamais réparés ici. Un boîtier mis à jour pouvait donc se
  # retrouver avec /tmp/fw-queue inaccessible au dashboard — plus aucune action exécutée,
  # et l'assistant tournait indéfiniment sur « recherche des réseaux ». Idempotent.
  groupadd -f protectado-queue >> "$LOG_FILE" 2>&1
  usermod -aG protectado-queue "$REAL_USER" >> "$LOG_FILE" 2>&1
  ok "Groupe protectado-queue vérifié ($REAL_USER membre)"

  # Régénérer les services systemd et le profil nono (applique les changements du repo)
  log "   Mise à jour des services systemd et profil nono..."
  NONO_BIN=$(command -v nono 2>/dev/null || echo "nono")
  sed -e "s|__USER__|$REAL_USER|g" \
      -e "s|__WORKDIR__|$INSTALL_DIR|g" \
      -e "s|nono run|$NONO_BIN run|g" \
      "$INSTALL_DIR/protectado-agent.service" \
      > /etc/systemd/system/protectado-agent.service
  sed -e "s|__WORKDIR__|$INSTALL_DIR|g" \
      "$INSTALL_DIR/protectado-runner.service" \
      > /etc/systemd/system/protectado-runner.service
  mkdir -p /etc/protectado
  sed -e "s|__WORKDIR__|$INSTALL_DIR|g" \
      "$INSTALL_DIR/protectado-agent.json" \
      > /etc/protectado/agent.json
  chmod 644 /etc/protectado/agent.json
  systemctl daemon-reload >> "$LOG_FILE" 2>&1
  ok "Services systemd et profil nono mis à jour"

  # Mise à jour du cron
  step5_autoupdate

  # Redémarrage
  log "   Redémarrage des services..."
  systemctl restart protectado-runner protectado-agent >> "$LOG_FILE" 2>&1
  sleep 5

  # Vérification + rollback si échec
  if ! systemctl is-active --quiet protectado-agent; then
    log "⚠  L'agent n'a pas redémarré — rollback vers ${LOCAL:0:8}..."
    cd "$INSTALL_DIR"
    repo_git reset --hard --quiet "$LOCAL" >> "$LOG_FILE" 2>&1
    mkdir -p "$INSTALL_DIR/data"
    [ -f "$BACKUP_DIR/config.json" ]   && cp "$BACKUP_DIR/config.json"   "$INSTALL_DIR/data/config.json"
    [ -f "$BACKUP_DIR/protectado.db" ] && cp "$BACKUP_DIR/protectado.db" "$INSTALL_DIR/data/protectado.db"
    .venv/bin/python -c "import database; database.init_db()" >> "$LOG_FILE" 2>&1
    systemctl restart protectado-runner protectado-agent >> "$LOG_FILE" 2>&1
    sleep 3
    if systemctl is-active --quiet protectado-agent; then
      ok "Rollback réussi — retour à ${LOCAL:0:8}"
    else
      log "❌  Rollback échoué — intervention manuelle requise"
      log "    journalctl -u protectado-agent -n 50"
    fi
    exit 1
  fi

  ok "Services redémarrés"

  LOCAL_IP=$(hostname -I | awk '{print $1}')
  echo ""
  echo "╔══════════════════════════════════════════════════╗"
  echo "║          Protectado mis à jour !                ║"
  echo "╚══════════════════════════════════════════════════╝"
  echo ""
  echo "  Dashboard  →  http://$LOCAL_IP     (Protectado, port 80)"
  echo "  Pi-hole    →  http://$LOCAL_IP:81  (admin/debug)"
  echo ""
  echo "  Sauvegarde →  $BACKUP_DIR"
  echo "  Logs       →  $LOG_FILE"
  echo ""
}

check_root() {
  if [ "$EUID" -ne 0 ]; then
    echo "Ce script doit être exécuté avec sudo."
    echo "Usage : curl -sSL <url>/bootstrap/bootstrap.sh | sudo bash"
    exit 1
  fi
}

detect_network() {
  # Préférer Ethernet (plus stable pour un serveur permanent)
  IFACE=""
  for candidate in eth0 eth1 enp1s0 enp2s0 end0; do
    if ip link show "$candidate" 2>/dev/null | grep -q "state UP"; then
      IFACE="$candidate"
      break
    fi
  done
  # Fallback : interface portant la route par défaut (WiFi ou autre)
  if [ -z "$IFACE" ]; then
    IFACE=$(ip route show default 2>/dev/null | awk '/default/{print $5}' | head -1)
  fi
  IFACE="${IFACE:-wlan0}"

  IPV4=$(ip route get 8.8.8.8 2>/dev/null | awk '/src/{print $7}' | head -1)
  IPV4="${IPV4:-192.168.1.1}"

  CONN_TYPE="WiFi"
  [[ "$IFACE" == eth* || "$IFACE" == en* ]] && CONN_TYPE="Ethernet"
  log "   Interface : $IFACE ($CONN_TYPE) — IP : $IPV4"
}

# ── Étape 1 : Dépendances système ────────────────────────────────────────────
step1_system() {
  step 1 "Dépendances système"
  apt-get update -qq >> "$LOG_FILE" 2>&1
  # iw + rfkill : absents d'Ubuntu Server par défaut, et pourtant requis par
  # action_runner (scan Wi-Fi de l'assistant, vérification du mode AP) — pas seulement
  # par les scripts bootstrap qui les installaient chacun de leur côté.
  apt-get install -y \
    git python3-pip python3-venv \
    arp-scan curl wget openssl \
    iw rfkill \
    avahi-daemon \
    unattended-upgrades apt-listchanges \
    >> "$LOG_FILE" 2>&1
  ok "Paquets installés"

  # Nom mDNS « protectado.local » : promis par toute la documentation, mais rien ne le
  # fournissait jusqu'ici (ni avahi, ni hostname). avahi-daemon publie <hostname>.local
  # sur le LAN — c'est ce qui évite au parent d'avoir à retenir une IP qui peut changer.
  # Le nom d'hôte n'est FIXÉ QUE s'il est encore celui par défaut de l'image : on ne
  # renomme pas la machine de quelqu'un qui l'a déjà nommée.
  CURRENT_HOST="$(hostname)"
  case "$CURRENT_HOST" in
    ubuntu|raspberrypi|localhost)
      hostnamectl set-hostname protectado >> "$LOG_FILE" 2>&1 \
        && ok "Nom d'hôte → protectado" \
        || log "   ⚠ nom d'hôte inchangé ($CURRENT_HOST)"
      ;;
    *) log "   Nom d'hôte conservé : $CURRENT_HOST (mDNS → $CURRENT_HOST.local)" ;;
  esac
  systemctl enable --now avahi-daemon >> "$LOG_FILE" 2>&1 \
    && ok "mDNS actif (avahi-daemon)" \
    || log "   ⚠ avahi-daemon indisponible — seule l'adresse IP fonctionnera"

  # CAP_NET_RAW sur arp-scan — permet le scan réseau sans root
  setcap cap_net_raw+ep "$(which arp-scan)" >> "$LOG_FILE" 2>&1
  ok "cap_net_raw → arp-scan"

  # Mises à jour de sécurité automatiques (patches OS uniquement)
  dpkg-reconfigure -f noninteractive unattended-upgrades >> "$LOG_FILE" 2>&1
  ok "unattended-upgrades activé"
}

# ── Étape 2 : Pi-hole ────────────────────────────────────────────────────────
step2_pihole() {
  step 2 "Pi-hole"

  if command -v pihole &>/dev/null; then
    # Vérifier que c'est bien Pi-hole v6 (l'API REST n'existe qu'en v6)
    PIHOLE_VER=$(pihole version 2>/dev/null | grep -oP '(?:Pi-hole|Core) version(?: is)? v\K[0-9]+' | head -1 || echo "0")
    if [ "$PIHOLE_VER" -lt 6 ] 2>/dev/null; then
      echo "❌  Pi-hole v$PIHOLE_VER détecté — Protectado requiert Pi-hole v6."
      echo "    Mettez à jour Pi-hole : pihole -up"
      exit 1
    fi
    ok "Pi-hole v$PIHOLE_VER déjà installé — ignoré"
    return
  fi

  detect_network

  # Pré-configuration pour installation non-interactive
  mkdir -p /etc/pihole
  cat > /etc/pihole/setupVars.conf <<EOF
PIHOLE_INTERFACE=$IFACE
IPV4_ADDRESS=$IPV4/24
IPV6_ADDRESS=
PIHOLE_DNS_1=8.8.8.8
PIHOLE_DNS_2=1.1.1.1
QUERY_LOGGING=true
INSTALL_WEB_SERVER=true
INSTALL_WEB_INTERFACE=true
LIGHTTPD_ENABLED=true
CACHE_SIZE=10000
DNS_FQDN_REQUIRED=false
DNS_BOGUS_PRIV=true
DNSMASQ_LISTENING=local
BLOCKING_ENABLED=true
EOF
  ok "Configuration Pi-hole préparée"

  log "   Installation Pi-hole (peut prendre 2-3 minutes)..."
  curl -sSL https://install.pi-hole.net | bash /dev/stdin --unattended >> "$LOG_FILE" 2>&1
  ok "Pi-hole installé"

  # Générer un mot de passe admin aléatoire
  PIHOLE_PASS=$(openssl rand -base64 16 | tr -dc 'a-zA-Z0-9' | head -c 20)
  pihole setpassword "$PIHOLE_PASS" >> "$LOG_FILE" 2>&1
  echo "PIHOLE_PASSWORD=$PIHOLE_PASS" >> "$INFO_FILE"
  ok "Mot de passe Pi-hole défini (voir $INFO_FILE)"

  # Déplacer l'interface web Pi-hole sur :81 → le :80 reste libre pour le dashboard
  # Protectado (plus intuitif pour les parents). Le DNS (:53) n'est PAS concerné.
  if command -v pihole-FTL &>/dev/null; then
    pihole-FTL --config webserver.port '81o,443os,[::]:81o,[::]:443os' >> "$LOG_FILE" 2>&1 \
      && ok "Interface Pi-hole déplacée sur :81 (le :80 est réservé à Protectado)" \
      || log "   ⚠ Échec réglage webserver.port — Pi-hole reste sur :80"
    # Nom local protectado.admin → IP du Pi côté enfants (192.168.50.1). En mode gateway,
    # un appareil du Wi-Fi enfants peut ouvrir http://protectado.admin pour RETROUVER
    # l'adresse du dashboard (page info-IP seule ; le dashboard reste bloqué côté enfants).
    pihole-FTL --config dns.hosts '[ "192.168.50.1 protectado.admin" ]' >> "$LOG_FILE" 2>&1 \
      && ok "Nom local protectado.admin → 192.168.50.1 (rappel d'adresse côté enfants)" \
      || log "   ⚠ Échec réglage dns.hosts (protectado.admin)"
    systemctl restart pihole-FTL >> "$LOG_FILE" 2>&1 || true
  fi
}

# ── Étape 3 : Protectado ────────────────────────────────────────────────────
step3_protectado() {
  step 3 "Protectado"

  # NB : pas de '| tee' sur les commandes git — il masquerait leur code de retour
  #      (le clone échouait silencieusement sur un dossier déjà présent). Sortie → log.
  if [ -d "$INSTALL_DIR/.git" ]; then
    log "   Mise à jour depuis git..."
    # Snapshot orphelin force-pushé → « pull » impossible (branches divergentes) :
    # on s'aligne sur le distant, comme dans le chemin de mise à jour.
    if repo_git fetch origin "$BRANCH" >> "$LOG_FILE" 2>&1 \
       && repo_git checkout --quiet -B "$BRANCH" >> "$LOG_FILE" 2>&1 \
       && repo_git reset --hard --quiet "origin/$BRANCH" >> "$LOG_FILE" 2>&1; then
      ok "Dépôt mis à jour (branche $BRANCH)"
      record_version "$INSTALL_DIR" "$BRANCH"
    else
      # SOFT-FAIL : on garde le code local et on continue l'install.
      log "   ⚠ Mise à jour git échouée (réseau/branche/état local) — code local conservé"
      ok "Dépôt prêt (local)"
    fi
  elif [ -e "$INSTALL_DIR" ]; then
    # Le dossier existe déjà mais n'est PAS un dépôt git (code scp'é, reste d'install…).
    # On NE clone PAS par-dessus (échouerait) : on garde ce qui est là. SOFT-FAIL.
    log "   ⚠ $INSTALL_DIR existe déjà (pas un dépôt git) — code local conservé, pas de mise à jour"
    ok "Dépôt prêt (local, non versionné)"
  else
    # Garde-fou : cloner une branche absente du distant échoue sèchement et l'installation
    # s'arrête sans rien poser. On vérifie donc qu'elle existe, et à défaut on se rabat
    # sur « main » en le DISANT. Un boîtier sur main vaut mieux qu'un boîtier vide.
    if [ -n "$(git ls-remote --heads "$REPO_URL" "$BRANCH" 2>/dev/null)" ]; then
      :
    elif [ -n "$(git ls-remote --heads "$REPO_URL" main 2>/dev/null)" ]; then
      log "   ⚠ branche '$BRANCH' absente du dépôt — repli sur 'main'"
      BRANCH="main"
    fi

    log "   Clonage du dépôt (branche $BRANCH)..."
    if git clone --branch "$BRANCH" --depth 1 "$REPO_URL" "$INSTALL_DIR" >> "$LOG_FILE" 2>&1; then
      ok "Dépôt cloné (branche $BRANCH)"
      record_version "$INSTALL_DIR" "$BRANCH"
    else
      log "   ⚠ Clonage échoué — vérifier réseau/accès au dépôt (l'installation peut être incomplète)"
    fi
  fi

  # nono (sandbox Landlock de l'agent IA) — install robuste (arm64 sur Pi, amd64 sur x86).
  if command -v nono &>/dev/null; then
    ok "nono déjà installé ($(nono --version 2>/dev/null | head -1))"
  else
    log "   Installation de nono..."
    NONO_ARCH=$(dpkg --print-architecture)   # arm64 sur Raspberry Pi
    # Dernière version : API GitHub (fiable), repli sur la redirection /latest.
    NONO_VER=$(curl -fsSL https://api.github.com/repos/always-further/nono/releases/latest 2>/dev/null \
                 | grep -oP '"tag_name"\s*:\s*"v?\K[0-9.]+' | head -1)
    [ -n "$NONO_VER" ] || NONO_VER=$(curl -fsSLI https://github.com/always-further/nono/releases/latest 2>/dev/null \
                 | grep -i '^location:' | grep -oP 'v\K[0-9.]+' | head -1)
    NONO_DEB="/tmp/nono-cli_${NONO_VER}_${NONO_ARCH}.deb"
    NONO_URL="https://github.com/always-further/nono/releases/download/v${NONO_VER}/nono-cli_${NONO_VER}_${NONO_ARCH}.deb"
    # Télécharger (échec dur), VÉRIFIER que c'est un vrai .deb, puis installer avec deps.
    if [ -n "$NONO_VER" ] \
       && curl -fSL -o "$NONO_DEB" "$NONO_URL" >> "$LOG_FILE" 2>&1 \
       && dpkg-deb --info "$NONO_DEB" >/dev/null 2>&1; then
      apt-get install -y "$NONO_DEB" >> "$LOG_FILE" 2>&1   # résout mieux les deps que dpkg -i
      rm -f "$NONO_DEB"
    else
      rm -f "$NONO_DEB"
    fi
    if command -v nono &>/dev/null; then
      ok "nono installé ($(nono --version 2>/dev/null | head -1))"
    else
      log "   ⚠ Échec install nono (arch=$NONO_ARCH, ver=${NONO_VER:-inconnue})."
      log "     URL testée : $NONO_URL"
      log "     → installe-le à la main puis relance, OU l'agent tournera sans sandbox."
    fi
  fi

  # Landlock présent ? nono en a besoin AU RUNTIME (absent des noyaux Raspberry Pi OS ;
  # présent sur Ubuntu linux-raspi). On prévient clairement plutôt que d'échouer au boot.
  if command -v nono &>/dev/null && ! grep -qw landlock /sys/kernel/security/lsm 2>/dev/null; then
    log "   ⚠ Landlock INACTIF sur ce noyau ($(uname -r)) — la sandbox nono ne s'initialisera pas."
    log "     Sur Raspberry Pi OS : noyau sans Landlock. Utiliser Ubuntu (linux-raspi) pour la sandbox."
  fi

  # Environnement Python
  log "   Création du venv Python..."
  python3 -m venv "$INSTALL_DIR/.venv" >> "$LOG_FILE" 2>&1
  "$INSTALL_DIR/.venv/bin/pip" install -q --upgrade pip >> "$LOG_FILE" 2>&1
  "$INSTALL_DIR/.venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt" >> "$LOG_FILE" 2>&1
  ok "Environnement Python prêt"

  # Répertoire de données (seul répertoire inscriptible par l'agent)
  mkdir -p "$INSTALL_DIR/data"

  # Initialiser la base de données
  cd "$INSTALL_DIR"
  .venv/bin/python -c "import database; database.init_db()" >> "$LOG_FILE" 2>&1
  ok "Base de données initialisée"

  # Code d'appairage — protège la FENÊTRE DE CONFIGURATION en posture dns_only.
  # Tant que le boîtier n'est pas configuré, l'assistant est ouvert sans authentification
  # (il n'y a pas encore de mot de passe parent à vérifier). En gateway ce n'est pas un
  # problème : l'assistant n'est joignable que depuis l'AP isolé Protectado-Setup. En
  # dns_only, le boîtier est joignable depuis TOUT le réseau de la maison : le premier
  # arrivé — y compris l'appareil d'un enfant — pourrait définir le mot de passe parent.
  # Ce code n'est affiché qu'ici, à la personne qui a lancé l'installation.
  # Généré une seule fois : une réinstallation ne doit pas invalider un code déjà noté.
  if [ ! -f "$INSTALL_DIR/data/pairing_code" ]; then
    tr -dc 'A-HJ-NP-Z2-9' < /dev/urandom | head -c 8 > "$INSTALL_DIR/data/pairing_code" || true
    chmod 640 "$INSTALL_DIR/data/pairing_code"
  fi
  echo "PAIRING_CODE=$(cat "$INSTALL_DIR/data/pairing_code" 2>/dev/null)" >> "$INFO_FILE"
  ok "Code d'appairage généré (affiché en fin d'installation)"

  # Ajuster les permissions après création de tous les fichiers
  chown -R "$REAL_USER:$REAL_USER" "$INSTALL_DIR"
  ok "Permissions ajustées ($REAL_USER)"
}

# ── Étape 4 : Services systemd ───────────────────────────────────────────────
step4_services() {
  step 4 "Services systemd"

  groupadd -f protectado-queue
  usermod -aG protectado-queue "$REAL_USER"
  touch /var/log/protectado-runner.log
  ok "Groupe protectado-queue configuré"

  NONO_BIN=$(command -v nono 2>/dev/null || echo "nono")

  sed -e "s|__USER__|$REAL_USER|g" \
      -e "s|__WORKDIR__|$INSTALL_DIR|g" \
      -e "s|nono run|$NONO_BIN run|g" \
      "$INSTALL_DIR/protectado-agent.service" \
      > /etc/systemd/system/protectado-agent.service

  sed -e "s|__WORKDIR__|$INSTALL_DIR|g" \
      "$INSTALL_DIR/protectado-runner.service" \
      > /etc/systemd/system/protectado-runner.service

  mkdir -p /etc/protectado
  sed -e "s|__WORKDIR__|$INSTALL_DIR|g" \
      "$INSTALL_DIR/protectado-agent.json" \
      > /etc/protectado/agent.json
  chmod 644 /etc/protectado/agent.json

  systemctl daemon-reload >> "$LOG_FILE" 2>&1
  systemctl enable protectado-runner protectado-agent >> "$LOG_FILE" 2>&1
  systemctl start protectado-runner protectado-agent >> "$LOG_FILE" 2>&1
  sleep 3

  STATUS_RUNNER=$(systemctl is-active protectado-runner || echo "inactive")
  STATUS_AGENT=$(systemctl is-active protectado-agent   || echo "inactive")
  ok "protectado-runner : $STATUS_RUNNER"
  ok "protectado-agent  : $STATUS_AGENT"

  if [ "$STATUS_AGENT" != "active" ]; then
    log "   ⚠ L'agent n'a pas démarré — vérifiez : journalctl -u protectado-agent"
  fi
}

# ── Étape 5 : Réseau — préparer l'ONBOARDING (posture « prêt à configurer ») ─
# Un boîtier neuf ne monte PAS le gateway : il pose les fichiers de l'AP + l'orchestrateur
# de posture et reste NON configuré → au boot il diffuse l'assistant (Protectado-Setup).
# Le gateway (uplink + NAT + clés réelles) est monté par l'assistant (apply_configuration).
step_network() {
  step 5 "Réseau (onboarding — prêt à configurer)"

  # Radio AP = 2ᵉ radio USB capable d'AP (chipset MT7612U, pilote mt76x2u).
  # Détection partagée avec config-ap.sh et ap-persist.sh via net-common.sh : trois
  # implémentations divergentes de « est-ce la carte AP ? » étaient une source de
  # désaccord silencieux entre l'installation et le démarrage.
  # net-common.sh n'est disponible qu'APRÈS le clonage du dépôt : bootstrap.sh est
  # exécuté via « curl | sudo bash », donc sans fichier voisin à sourcer au démarrage.
  # shellcheck source=net-common.sh
  . "$INSTALL_DIR/bootstrap/net-common.sh"

  local ap_radio=""
  ap_radio="$(pt_find_ap_iface || true)"
  if [ -z "$ap_radio" ]; then
    ok "Pas de 2ᵉ radio AP détectée → mode dns_only (pas d'onboarding passerelle)."
    return
  fi
  log "   Radio AP détectée ($ap_radio) → onboarding gateway."

  # 1) Poser les FICHIERS de l'AP enfants (règle udev + confs + services) SANS les activer
  #    (BOOT_ONLY : pas d'enable — c'est l'orchestrateur qui démarrera l'AP une fois configuré).
  BOOT_ONLY=1 bash "$INSTALL_DIR/bootstrap/ap-persist.sh" install >> "$LOG_FILE" 2>&1 \
    && ok "Fichiers AP enfants + règle udev posés (services désactivés)" \
    || log "   ⚠ ap-persist install a signalé une erreur (voir log)"

  # 2) Poser l'orchestrateur de posture (choisit config/gateway au boot selon config.json).
  bash "$INSTALL_DIR/bootstrap/protectado-boot.sh" install >> "$LOG_FILE" 2>&1 \
    && ok "Orchestrateur de posture posé" \
    || log "   ⚠ orchestrateur : voir log"

  # 3) Le boîtier reste NON configuré (pas de config.json 'configured:true') → posture CONFIG.
  NEED_REBOOT=1
  ok "Onboarding prêt — au reboot, le boîtier diffuse l'assistant 'Protectado-Setup'."
}

step5_autoupdate() {
  step 6 "Mises à jour automatiques"

  # Installer le script de mise à jour sécurisé dans /usr/local/sbin/
  # root:root 755 — hors de portée du service user
  sed "s|__USER__|$REAL_USER|g" \
      "$INSTALL_DIR/bootstrap/protectado-update.sh" \
      > /usr/local/sbin/protectado-update
  chown root:root /usr/local/sbin/protectado-update
  chmod 755 /usr/local/sbin/protectado-update
  ok "Script de mise à jour → /usr/local/sbin/protectado-update (root:root)"

  # Supprimer le sudoers si présent (plus nécessaire avec le path unit)
  rm -f /etc/sudoers.d/protectado-update
  ok "sudoers supprimé (remplacé par systemd path unit)"

  # Installer le path unit et le service unit (déclenchement sans sudo)
  sed "s|__WORKDIR__|$INSTALL_DIR|g" \
      "$INSTALL_DIR/protectado-update.service" \
      > /etc/systemd/system/protectado-update.service
  sed "s|__WORKDIR__|$INSTALL_DIR|g" \
      "$INSTALL_DIR/protectado-update.path" \
      > /etc/systemd/system/protectado-update.path
  systemctl daemon-reload >> "$LOG_FILE" 2>&1
  systemctl enable --now protectado-update.path >> "$LOG_FILE" 2>&1
  ok "systemd path unit activé (protectado-update.path)"

  cat > /etc/cron.d/protectado <<EOF
# Protectado — auto-update chaque nuit à 3h00
0 3 * * * root /usr/local/sbin/protectado-update >> /var/log/fw-update.log 2>&1

# Pi-hole — mise à jour chaque dimanche à 4h00
0 4 * * 0 root pihole -up >> /var/log/fw-update.log 2>&1

# Rapport journalier à 23h00 — fin de journée : l'agrégation de daily_report porte sur
# le jour COURANT (get_time_spent_today), elle n'est complète qu'en fin de soirée.
# Le parent le lit le lendemain.
0 23 * * * $REAL_USER $INSTALL_DIR/.venv/bin/python $INSTALL_DIR/daily_report.py >> /var/log/protectado-report.log 2>&1

# Politique de rétention — chaque dimanche à 2h00.
# Purge TOUTES les tables d'historique (timeline, usage quotidien, événements et
# rapports IA, catalogue de domaines non revus par le parent) selon
# config.privacy.retention_days (90 jours par défaut, 0 = illimité).
# Auparavant seule dns_timeline était purgée : le reste grossissait indéfiniment.
0 2 * * 0 $REAL_USER $INSTALL_DIR/.venv/bin/python -c "import sys, json; sys.path.insert(0,'$INSTALL_DIR'); import database, privacy; from paths import CONFIG_PATH; cfg=json.load(open(CONFIG_PATH)); print(database.purge_old_data(privacy.retention_days(cfg)))" >> /var/log/protectado-report.log 2>&1

EOF
  chmod 644 /etc/cron.d/protectado
  touch /var/log/protectado-report.log
  chown "$REAL_USER" /var/log/protectado-report.log
  ok "Crons configurés"
}

# ── Étape 6 : Récapitulatif ──────────────────────────────────────────────────
step6_summary() {
  step 7 "Installation terminée"

  LOCAL_IP=$(hostname -I | awk '{print $1}')
  DASHBOARD_URL="http://$LOCAL_IP"

  echo ""
  echo "╔══════════════════════════════════════════════════╗"
  echo "║          Protectado installé avec succès !      ║"
  echo "╚══════════════════════════════════════════════════╝"
  echo ""
  echo "  Dashboard  →  $DASHBOARD_URL     (port 80)"
  # Le nom .local n'est annoncé au parent que s'il est RÉELLEMENT résolvable : mDNS est
  # capricieux selon les box et les téléphones, et une adresse annoncée qui ne répond pas
  # est pire que pas d'adresse du tout. L'IP reste donc l'information de référence.
  MDNS_NAME="$(hostname).local"
  if getent hosts "$MDNS_NAME" >/dev/null 2>&1 || avahi-resolve -4 -n "$MDNS_NAME" >/dev/null 2>&1; then
    echo "             ou http://$MDNS_NAME"
  fi
  echo "  Pi-hole    →  http://$LOCAL_IP:81  (admin/debug)"
  echo ""

  if [ -f "$INFO_FILE" ]; then
    echo "  ┌─ Informations de configuration ─────────────────"
    while IFS='=' read -r key val; do
      printf "  │  %-22s %s\n" "$key :" "$val"
    done < "$INFO_FILE"
    echo "  └──────────────────────────────────────────────────"
    echo ""
    echo "  → Gardez ces informations pour le wizard de configuration."
  fi

  echo ""
  echo "  Prochaine étape : ouvrez $DASHBOARD_URL depuis"
  echo "  n'importe quel appareil du réseau et suivez le wizard."
  echo ""
  echo "  Logs bootstrap : $LOG_FILE"
  echo ""
}

# ── Purge ────────────────────────────────────────────────────────────────────
run_purge() {
  echo ""
  echo "╔══════════════════════════════════════════════════╗"
  echo "║     Protectado — Désinstallation complète       ║"
  echo "╚══════════════════════════════════════════════════╝"
  echo ""
  read -rp "  ⚠  Supprimer TOUTE l'installation (code + données + Pi-hole) ? [oui/N] : " CONFIRM
  [ "$CONFIRM" != "oui" ] && echo "Annulé." && exit 0

  # ── 1. Nettoyage Pi-hole AVANT de supprimer le venv ──────────────────────
  if [ -f "$INSTALL_DIR/data/config.json" ] && [ -x "$INSTALL_DIR/.venv/bin/python" ]; then
    log "Nettoyage Pi-hole..."
    "$INSTALL_DIR/.venv/bin/python" - <<'PYEOF' || log "  ⚠ Nettoyage Pi-hole partiel (continuer)"
import sys, json
sys.path.insert(0, '/opt/protectado')
from paths import CONFIG_PATH
from pihole_api import PiHoleAPI
from urllib.parse import quote as urlquote

with open(CONFIG_PATH) as f:
    config = json.load(f)

ph = PiHoleAPI(config["pihole"]["host"], config["pihole"]["password"])

# Groupes créés par Protectado
_SUFFIXES = ("-blocked", "-work", "-permissive")
for g in ph.get_groups():
    name = g.get("name", "")
    if any(name.endswith(s) for s in _SUFFIXES) or name == "adult-override":
        ph._delete(f"/groups/{g['id']}")
        print(f"  Groupe supprimé : {name}")

# Clients enregistrés par Protectado
for c in ph.get_clients():
    if "Protectado" in c.get("comment", ""):
        ph._delete(f"/clients/{urlquote(c['client'], safe='')}")
        print(f"  Client supprimé : {c['client']}")

# Domaines bloquants Protectado
for d in ph.get_deny_domains():
    if "protectado" in d.get("comment", "").lower():
        ph._delete(f"/domains/deny/regex/{urlquote(d['domain'], safe='')}")
        print(f"  Règle supprimée : {d['domain'][:50]}")

print("Pi-hole nettoyé.")
PYEOF
    ok "Pi-hole nettoyé"
  else
    log "  ⚠ config.json ou venv absent — nettoyage Pi-hole ignoré"
  fi

  # ── 2. Arrêt et suppression ───────────────────────────────────────────────
  systemctl stop    protectado-agent protectado-runner 2>/dev/null || true
  systemctl disable protectado-agent protectado-runner 2>/dev/null || true
  rm -f /etc/systemd/system/protectado-agent.service
  rm -f /etc/systemd/system/protectado-runner.service
  systemctl daemon-reload

  rm -f /etc/cron.d/protectado
  rm -rf /etc/protectado
  rm -rf "$INSTALL_DIR"
  rm -f /var/log/protectado-*.log /var/log/fw-*.log
  rm -rf /tmp/fw-queue

  echo ""
  echo "  ✓ Installation et configuration Pi-hole supprimées."
  echo "  Pour réinstaller : curl -fsSL https://raw.githubusercontent.com/protectado/protectado/main/bootstrap/bootstrap.sh | sudo bash"
  echo ""
}

# ── Main ─────────────────────────────────────────────────────────────────────
main() {
  check_root
  touch "$LOG_FILE"

  if [ "${1:-}" = "purge" ]; then
    run_purge
    exit 0
  fi

  # Détection : installation existante → mise à jour, sinon → installation initiale
  if detect_existing; then
    run_update
    exit 0
  fi

  rm -f "$INFO_FILE"

  echo ""
  echo "╔══════════════════════════════════════════════════╗"
  echo "║        Protectado — Bootstrap Raspberry Pi      ║"
  echo "╚══════════════════════════════════════════════════╝"
  echo ""
  log "Début de l'installation — $(date)"

  step1_system
  step2_pihole
  step3_protectado
  step4_services
  step_network
  step5_autoupdate
  step6_summary

  if [ "${NEED_REBOOT:-0}" = "1" ]; then
    echo ""
    echo "  ⚠  Onboarding prêt — REDÉMARREZ pour lancer l'assistant :"
    echo "         sudo reboot"
    echo "     Au reboot, le boîtier diffuse le Wi-Fi ouvert 'Protectado-Setup' :"
    echo "     un parent s'y connecte et configure tout via l'assistant web."
  fi
}

main "$@"
