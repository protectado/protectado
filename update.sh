#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Arnaud Ortais
# Dual-licensed: AGPL-3.0 (open source) or Commercial License — see LICENSE and LICENSE-COMMERCIAL.
# update.sh — Mise à jour manuelle de Protectado
# Version git-based : pull depuis GitHub, migration DB, restart services.
# Usage : sudo bash update.sh

set -euo pipefail

INSTALL_DIR="/opt/protectado"
# Branche suivie : celle mémorisée à l'installation (data/branch). $PROTECTADO_BRANCH
# permet de forcer ponctuellement. Repli : la branche extraite localement, puis « stable ».
BRANCH="${PROTECTADO_BRANCH:-}"
if [ -z "$BRANCH" ] && [ -s "$INSTALL_DIR/data/branch" ]; then
  BRANCH="$(tr -d '[:space:]' < "$INSTALL_DIR/data/branch")"
fi
if [ -z "$BRANCH" ]; then
  BRANCH="$(git -C "$INSTALL_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
fi
case "$BRANCH" in ''|HEAD) BRANCH="stable" ;; esac
BACKUP_DIR="/opt/protectado-bk-manuel"   # persistant : /tmp est vidé au reboot

echo "╔══════════════════════════════════════╗"
echo "║   Protectado — Mise à jour          ║"
echo "╚══════════════════════════════════════╝"

if [ ! -d "$INSTALL_DIR/.git" ]; then
  echo "❌  $INSTALL_DIR n'est pas un dépôt git."
  echo "    Utilisez bootstrap.sh pour une installation initiale."
  exit 1
fi

cd "$INSTALL_DIR"

# ── Sauvegarde ───────────────────────────────────────────────────────────────
# Config et base vivent sous data/ (et sont ignorées par git, donc jamais écrasées) —
# on les sauvegarde quand même pour pouvoir revenir en arrière proprement.
echo "→ Sauvegarde de la configuration..."
mkdir -p "$BACKUP_DIR"
[ -f data/config.json ]   && cp data/config.json   "$BACKUP_DIR/" && echo "   config.json sauvegardé ✓"
[ -f data/protectado.db ] && cp data/protectado.db "$BACKUP_DIR/" && echo "   protectado.db sauvegardé ✓"

# ── Mise à jour du code ───────────────────────────────────────────────────────
echo "→ Vérification des mises à jour..."
git fetch origin "$BRANCH" --quiet

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse "origin/$BRANCH")

if [ "$LOCAL" = "$REMOTE" ]; then
  echo "   Déjà à jour (${LOCAL:0:8}) — rien à faire."
  exit 0
fi

echo "   Mise à jour : ${LOCAL:0:8} → ${REMOTE:0:8}"
# Le dépôt public est publié par SNAPSHOT (commit orphelin poussé en --force) : l'historique
# distant étant réécrit à chaque publication, « git pull » échoue sur des branches
# divergentes. Cette installation est une cible de déploiement → on s'aligne sur le distant.
# Les fichiers ignorés (data/config.json, *.db, .venv/) ne sont pas touchés.
git reset --hard "origin/$BRANCH" --quiet
echo "   Code aligné sur origin/$BRANCH ✓"

# Version déployée — affichée dans le tableau de bord et sur la page de connexion.
printf '%s\n' "$BRANCH" > data/branch 2>/dev/null || true
printf '{"commit": "%s", "branch": "%s", "updated_at": "%s"}\n' \
  "${REMOTE:0:7}" "$BRANCH" "$(date -Is 2>/dev/null || date)" > data/version.json 2>/dev/null || true

# Restaurer config.json (ceinture et bretelles : data/ est ignoré par git)
[ -f "$BACKUP_DIR/config.json" ] && cp "$BACKUP_DIR/config.json" data/config.json

# ── Dépendances Python ───────────────────────────────────────────────────────
echo "→ Mise à jour des dépendances Python..."
.venv/bin/pip install -q --upgrade -r requirements.txt
echo "   Dépendances OK ✓"

# ── Migration base de données ─────────────────────────────────────────────────
echo "→ Migration base de données..."
.venv/bin/python -c "import database; database.init_db(); print('   DB OK ✓')"

# ── Redémarrage ───────────────────────────────────────────────────────────────
echo "→ Redémarrage des services..."
sudo systemctl restart protectado-runner protectado-agent
sleep 3

# ── Vérification ─────────────────────────────────────────────────────────────
STATUS_RUNNER=$(systemctl is-active protectado-runner || echo "inactive")
STATUS_AGENT=$(systemctl is-active protectado-agent   || echo "inactive")

echo ""
echo "  protectado-runner : $STATUS_RUNNER"
echo "  protectado-agent  : $STATUS_AGENT"

if [ "$STATUS_AGENT" != "active" ]; then
  echo ""
  echo "⚠  L'agent n'a pas redémarré — rollback..."
  git reset --hard "$LOCAL" --quiet
  [ -f "$BACKUP_DIR/config.json" ]   && cp "$BACKUP_DIR/config.json"   data/config.json
  [ -f "$BACKUP_DIR/protectado.db" ] && cp "$BACKUP_DIR/protectado.db" data/protectado.db
  .venv/bin/python -c "import database; database.init_db()"
  sudo systemctl restart protectado-runner protectado-agent
  echo "   Rollback vers ${LOCAL:0:8} effectué."
  exit 1
fi

echo ""
echo "╔══════════════════════════════════════╗"
echo "║        Mise à jour terminée !        ║"
echo "╚══════════════════════════════════════╝"
echo ""
echo "  Dashboard → http://$(hostname -I | awk '{print $1}')   (Pi-hole sur le :81)"
echo ""
