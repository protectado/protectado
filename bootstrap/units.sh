# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Arnaud Ortais
# Dual-licensed: AGPL-3.0 (open source) or Commercial License — see LICENSE and LICENSE-COMMERCIAL.
#
# units.sh — installe les unités systemd et le profil nono depuis le dépôt. À SOURCER :
#
#     . "$INSTALL_DIR/bootstrap/units.sh"
#     install_service_files "$INSTALL_DIR" "$SVC_USER"
#
# Un seul installeur pour bootstrap.sh (installation et mise à jour) et pour l'updater
# automatique : celui-ci ne réinstallait ni l'unité de l'agent ni le profil nono, donc une
# correction de l'un ou de l'autre n'atteignait jamais un boîtier déjà livré.
#
# SYSTEMD_DIR, ETC_DIR et DAEMON_RELOAD ne servent qu'aux tests.

install_service_files() {
  local dir="$1" user="$2"
  local systemd="${SYSTEMD_DIR:-/etc/systemd/system}" etc="${ETC_DIR:-/etc/protectado}"
  local nono_bin
  nono_bin="$(command -v nono 2>/dev/null || echo nono)"

  # Chaque copie est vérifiée : sans cela seule la dernière commande comptait, et une
  # installation ratée se déclarait réussie.
  sed -e "s|__USER__|$user|g" -e "s|__WORKDIR__|$dir|g" -e "s|nono run|$nono_bin run|g" \
      "$dir/protectado-agent.service" > "$systemd/protectado-agent.service" || return 1
  local unite
  for unite in protectado-runner.service protectado-update.service protectado-update.path; do
    sed -e "s|__WORKDIR__|$dir|g" "$dir/$unite" > "$systemd/$unite" || return 1
  done

  mkdir -p "$etc" || return 1
  sed -e "s|__WORKDIR__|$dir|g" "$dir/protectado-agent.json" > "$etc/agent.json" || return 1
  chmod 644 "$etc/agent.json" || return 1

  ${DAEMON_RELOAD:-systemctl daemon-reload}
}

# wait_healthy — attend que le tableau de bord réponde 200 sur /api/health, c'est-à-dire
# que le moniteur tourne et ait réussi un cycle récent. « systemctl is-active » ne
# prouvait rien : un agent dont le moniteur plante à chaque cycle reste actif.
# Jusqu'à HEALTH_TRIES sondes espacées de HEALTH_PAUSE secondes (60 s par défaut).
wait_healthy() {
  local i essais="${HEALTH_TRIES:-12}" pause="${HEALTH_PAUSE:-5}"
  for i in $(seq 1 "$essais"); do
    curl -fsS -m 4 -o /dev/null "http://127.0.0.1:8080/api/health" 2>/dev/null && return 0
    [ "$i" -lt "$essais" ] && sleep "$pause"
  done
  return 1
}
