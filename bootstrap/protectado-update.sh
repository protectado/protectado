#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Arnaud Ortais
# Dual-licensed: AGPL-3.0 (open source) or Commercial License — see LICENSE and LICENSE-COMMERCIAL.
#
# Template installé par bootstrap dans /usr/local/sbin/protectado-update
# Propriété : root:root 755 — jamais écrasé par git pull
#
# Modèle de sécurité :
#   - Seul systemctl restart s'exécute en root
#   - git et pip tournent en root, sur un code qui appartient à root ; seule la
#     migration du schéma (data/) tourne en tant que __USER__ (service user)
#   - /usr/local/sbin/protectado-update est hors de portée du service user
#   - CE script est réinstallé depuis le dépôt par le runner à son démarrage
#     (system_maintenance.sync_updater) : il suit les mises à jour sans bootstrap.sh

set -euo pipefail

INSTALL_DIR="/opt/protectado"

# Branche suivie : celle MÉMORISÉE à l'installation (data/branch), jamais une valeur
# codée en dur. Une machine installée sur « main » ne doit pas basculer sur « stable »
# à la première mise à jour, ni l'inverse.
# Repli si le fichier manque (installation antérieure à sa création) : la branche
# réellement extraite dans le dépôt local — on ne déplace pas une machine qui tourne.
# Dernier recours seulement : « stable ».
BRANCH_FILE="$INSTALL_DIR/data/branch"
if [ -s "$BRANCH_FILE" ]; then
    BRANCH="$(tr -d '[:space:]' < "$BRANCH_FILE")"
else
    BRANCH="$(git -C "$INSTALL_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
    case "$BRANCH" in ''|HEAD) BRANCH="stable" ;; esac
fi
SVC_USER="__USER__"
LOG="/var/log/fw-update.log"
TRIGGER="$INSTALL_DIR/data/update.trigger"
FAILCOUNT="$INSTALL_DIR/data/update.failures"
MAX_FAILURES=3
TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')

log() { echo "[$TIMESTAMP] $*" | tee -a "$LOG"; }

# Ceinture et bretelles : le déclencheur est aussi supprimé par ExecStopPost= dans
# l'unité systemd. On le retire ici dès l'entrée pour qu'un arrêt brutal du script
# (OOM, coupure de courant, kill -9) ne laisse pas protectado-update.path relancer
# l'unité en boucle.
rm -f "$TRIGGER" 2>/dev/null || true

# Garde-fou anti-boucle : après MAX_FAILURES échecs consécutifs, on refuse de
# retenter. Une mise à jour qui échoue trois fois de suite ne réussira pas à la
# quatrième, et chaque tentative fait git reset --hard + pip install + restart des
# services : réessayer indéfiniment casse le boîtier au lieu de le réparer.
read_failures() { cat "$FAILCOUNT" 2>/dev/null || echo 0; }
note_failure()  { echo $(( $(read_failures) + 1 )) > "$FAILCOUNT" 2>/dev/null || true; }
clear_failures() { rm -f "$FAILCOUNT" 2>/dev/null || true; }

# Un échec en cours de route (git, pip, migration) sort par set -e sans passer par le
# chemin de rollback : le trap ERR garantit qu'il est compté lui aussi. « exit 1 »
# explicite ne déclenche pas ERR, donc pas de double comptage sur le rollback.
trap 'note_failure' ERR

FAILURES=$(read_failures)
case "$FAILURES" in ''|*[!0-9]*) FAILURES=0 ;; esac
if [ "$FAILURES" -ge "$MAX_FAILURES" ]; then
    log "CRIT  $FAILURES échecs consécutifs de mise à jour — abandon, plus aucune tentative"
    log "CRIT  Le boîtier reste sur la version actuellement installée (fonctionnelle)."
    log "CRIT  Intervention manuelle : journalctl -u protectado-update -n 100"
    log "CRIT  Pour réarmer après correction : rm $FAILCOUNT"
    exit 0
fi

# Le CODE appartient à root (cf. system_maintenance.py) : git et pip tournent donc en
# root, sur un dépôt entièrement à root, ce qui écarte aussi l'erreur « dubious
# ownership » des propriétés mélangées. Seul ce qui écrit dans data/ (la base) tourne
# en tant que SVC_USER, propriétaire de data/.
as_user() { sudo -u "$SVC_USER" -- "$@"; }

log "INFO  Vérification des mises à jour..."

cd "$INSTALL_DIR"

# Refspec EXPLICITE : le clone d'installation est mono-branche (git clone --branch),
# donc remote.origin.fetch ne ramène QUE la branche d'origine. Un « git fetch origin main »
# réussit alors sans créer refs/remotes/origin/main, et le reset échoue sur
# « ambiguous argument 'origin/main' ». Épingler data/branch sur une autre branche
# suffisait à casser toute mise à jour.
git fetch origin "+refs/heads/$BRANCH:refs/remotes/origin/$BRANCH" --quiet 2>&1 || {
    log "WARN  Impossible de joindre le dépôt — pas de mise à jour"
    exit 0
}

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse "origin/$BRANCH")

if [ "$LOCAL" = "$REMOTE" ]; then
    log "OK    Déjà à jour (${LOCAL:0:8})"
    clear_failures
    exit 0
fi

log "INFO  Mise à jour disponible : ${LOCAL:0:8} → ${REMOTE:0:8}"

# ---------------------------------------------------------------------------
# Chemin court : une publication qui ne touche QUE catalog/
# ---------------------------------------------------------------------------
# Le catalogue des services (services, libellés, domaines) est de la DONNÉE : il change
# parce qu'un service ajoute un domaine de diffusion, pas parce que le produit évolue. Lui
# appliquer la procédure complète — sauvegarde, pip, migration du schéma, redémarrage du
# runner ET de l'agent — coupe le réseau des enfants quelques secondes pour une ligne de
# JSON. C'est ce qui donne au catalogue son propre cycle de mise à jour, sans second dépôt
# ni appel sortant supplémentaire : il voyage dans la publication que ce script va déjà
# chercher, et l'agent le relit à chaud au cycle suivant, sans interruption
# (cf. monitor._maybe_reload_services).
#
# « git diff A B » compare deux ARBRES et n'a donc pas besoin d'ancêtre commun, ce qui est
# indispensable ici : le dépôt public est publié par snapshot orphelin, donc LOCAL et
# REMOTE n'en ont jamais. « A...B », lui, échouerait.
#
# Un diff VIDE ou illisible retombe sur la procédure complète : en cas de doute on fait le
# chemin long, qui est celui qui sait se rollback.
#
# PAS de « grep -qv » ici, et ce n'est pas un détail de style : -q fait sortir grep dès la
# première ligne hors catalog/, donc printf reçoit SIGPIPE si le diff dépasse le tampon du
# tube (64 ko), donc pipefail fait de la comparaison un statut non nul, donc le « ! »
# inverse la décision et une mise à jour de code complète prendrait le chemin court —
# nouveaux fichiers en place, mais ni dépendances ni redémarrage. Mesuré, reproductible.
# Cette forme lit l'entrée jusqu'au bout et ne décide que sur son contenu.
CHANGED=$(git diff --name-only "$LOCAL" "$REMOTE" 2>/dev/null || true)
HORS_CATALOGUE=$(printf '%s\n' "$CHANGED" | grep -v '^catalog/' || true)
if [ -n "$CHANGED" ] && [ -z "$HORS_CATALOGUE" ]; then
    git reset --hard "origin/$BRANCH" --quiet 2>&1
    printf '%s\n' "$BRANCH" > "$INSTALL_DIR/data/branch" 2>/dev/null || true
    printf '{"commit": "%s", "branch": "%s", "updated_at": "%s"}\n' \
        "${REMOTE:0:7}" "$BRANCH" "$(date -Is 2>/dev/null || date)" \
        > "$INSTALL_DIR/data/version.json" 2>/dev/null || true
    chown "$SVC_USER" "$INSTALL_DIR/data/branch" "$INSTALL_DIR/data/version.json" 2>/dev/null || true
    log "OK    Catalogue des services seul modifié — appliqué sans redémarrage (${REMOTE:0:8})"
    clear_failures
    exit 0
fi

# Sauvegarde dans /opt/ (persistant, survit à un reboot contrairement à /tmp/)
BACKUP="/opt/protectado-bk-$(date +%s)"
mkdir -p "$BACKUP"
[ -f data/config.json ]    && cp data/config.json    "$BACKUP/"
[ -f data/protectado.db ]  && cp data/protectado.db  "$BACKUP/"

# Alignement sur le distant — hooks git tournent en tant que SVC_USER.
# PAS de « git pull » : le dépôt public est publié par snapshot orphelin poussé en
# --force, donc l'historique distant est réécrit et les branches divergent à chaque
# publication (pull échouerait avec « Need to specify how to reconcile »). Cette
# installation est une cible de déploiement : le distant fait autorité.
# Les fichiers ignorés (data/config.json, *.db, .venv/) ne sont pas touchés.
git reset --hard "origin/$BRANCH" --quiet 2>&1
log "INFO  Code aligné sur origin/$BRANCH"

# Version déployée, lue par le tableau de bord et la page de connexion. Écrite ICI, hors
# sandbox : l'agent ne peut pas interroger git (nono ne lui donne ni le binaire ni le
# dépôt en écriture). data/ n'est pas versionné, donc ce fichier survit au reset.
printf '%s\n' "$BRANCH" > "$INSTALL_DIR/data/branch" 2>/dev/null || true
printf '{"commit": "%s", "branch": "%s", "updated_at": "%s"}\n' \
    "${REMOTE:0:7}" "$BRANCH" "$(date -Is 2>/dev/null || date)" \
    > "$INSTALL_DIR/data/version.json" 2>/dev/null || true
chown "$SVC_USER" "$INSTALL_DIR/data/branch" "$INSTALL_DIR/data/version.json" 2>/dev/null || true

# Restaurer config (jamais écrasée par git)
[ -f "$BACKUP/config.json" ] && cp "$BACKUP/config.json" data/config.json

# Paquets et fichiers système apparus après l'installation : ce sont des MIGRATIONS
# (bootstrap/migrations/), appliquées par le runner à son redémarrage ci-dessous.

# Dépendances en root (le venv est à root), migration du schéma en SVC_USER (data/).
# Versions exactes du lock, sans --upgrade : une nouvelle version d'une dépendance ne
# doit pas arriver sur le parc sans qu'un commit l'ait choisie.
.venv/bin/pip install -q -r requirements.lock 2>&1
log "INFO  Dépendances Python OK"

as_user .venv/bin/python -c "
import sys; sys.path.insert(0, '$INSTALL_DIR')
import database; database.init_db()
" 2>&1
log "INFO  Migration DB OK"

# Mettre à jour les fichiers systemd depuis le repo (applique les changements de templates)
# protectado-runner.service en fait partie : sans lui, une correction de l'unité du
# service privilégié (ordre de démarrage, par exemple) n'atteignait jamais les boîtiers
# déjà installés, qui ne repassent pas par bootstrap.sh.
# Tout passe par l'installeur partagé avec bootstrap.sh : unité de l'agent et profil
# nono compris, qui n'étaient jamais réinstallés ici.
. "$INSTALL_DIR/bootstrap/units.sh"
install_service_files "$INSTALL_DIR" "$SVC_USER" 2>&1

# Seules opérations nécessitant root
systemctl restart protectado-runner protectado-agent 2>&1

# Vérification + rollback : le tableau de bord doit répondre sur /api/health, donc avec un
# moniteur qui a réussi un cycle (cf. wait_healthy), pas seulement un service « actif ».
if ! wait_healthy; then
    note_failure
    log "ERROR Boîtier en mauvaise santé après mise à jour — rollback vers ${LOCAL:0:8} (échec $(read_failures)/$MAX_FAILURES)"
    git reset --hard "$LOCAL" --quiet 2>&1
    install_service_files "$INSTALL_DIR" "$SVC_USER" 2>&1 || true
    # Les paquets de la version rétablie : sans eux, un rollback dû à une dépendance
    # cassée relance l'ancien code sur les mêmes paquets et échoue aussi. Une version
    # antérieure au lock n'a que requirements.txt, sans --upgrade : n'installe que ce
    # qui manque. Un échec ici ne doit pas empêcher le redémarrage qui suit (set -e).
    if [ -f requirements.lock ]; then
        .venv/bin/pip install -q -r requirements.lock 2>&1 \
            || log "ERROR Réinstallation des dépendances de ${LOCAL:0:8} échouée"
    else
        .venv/bin/pip install -q -r requirements.txt 2>&1 \
            || log "ERROR Réinstallation des dépendances de ${LOCAL:0:8} échouée"
    fi
    [ -f "$BACKUP/config.json" ]   && cp "$BACKUP/config.json"   data/config.json
    [ -f "$BACKUP/protectado.db" ] && cp "$BACKUP/protectado.db" data/protectado.db
    as_user .venv/bin/python -c "
import sys; sys.path.insert(0, '$INSTALL_DIR')
import database; database.init_db()
" 2>&1
    systemctl restart protectado-runner protectado-agent 2>&1
    if wait_healthy; then
        rm -rf "$BACKUP"
        # Rétablir la version AFFICHÉE : après un rollback, l'interface doit montrer le
        # commit réellement en place, pas celui qu'on a tenté de déployer.
        printf '{"commit": "%s", "branch": "%s", "updated_at": "%s"}\n' \
            "${LOCAL:0:7}" "$BRANCH" "$(date -Is 2>/dev/null || date)" \
            > "$INSTALL_DIR/data/version.json" 2>/dev/null || true
        chown "$SVC_USER" "$INSTALL_DIR/data/version.json" 2>/dev/null || true
        log "INFO  Rollback réussi — retour à ${LOCAL:0:8}"
    else
        log "CRIT  Rollback échoué — intervention manuelle requise"
        log "CRIT  journalctl -u protectado-agent -n 50"
    fi
    exit 1
fi

rm -rf "$BACKUP"
clear_failures
log "OK    Mise à jour terminée — version ${REMOTE:0:8}"
