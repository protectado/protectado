# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Arnaud Ortais
# Dual-licensed: AGPL-3.0 (open source) or Commercial License — see LICENSE and LICENSE-COMMERCIAL.
"""
action_queue.py — dépôt d'une action pour le runner root.

Le runner traite tout fichier « action-*.json » dès qu'il apparaît. Le fichier est donc
écrit sous un nom temporaire qui ne porte pas ce motif, puis renommé d'un coup
(os.replace, atomique dans un même répertoire) : le runner ne voit jamais un fichier
incomplet, qu'il jugerait illisible et mettrait de côté, action perdue.

Le nom porte l'horodatage d'abord, pour que l'ordre alphabétique lu par le runner soit
l'ordre de dépôt, puis un suffixe aléatoire, pour que deux processus qui déposent dans
la même microseconde n'écrasent pas le fichier l'un de l'autre.
"""

import json
import os
import tempfile
import uuid
from datetime import datetime

from paths import ACTION_QUEUE_DIR

# Préfixe des fichiers en cours d'écriture : ni « action- », ni « .json ».
TMP_PREFIX = ".tmp-"


def enqueue(action: str, args: dict) -> str:
    """Dépose une action et renvoie le chemin du fichier. Lève OSError si la file est
    inaccessible : c'est à l'appelant de dire ce que cet échec signifie pour lui.

    Le répertoire est normalement créé par le runner (root:protectado-queue 2770). S'il
    manque, on le crée en 0770, pour que le runner et les autres membres du groupe
    puissent y lire et écrire.
    """
    if not os.path.isdir(ACTION_QUEUE_DIR):
        os.makedirs(ACTION_QUEUE_DIR, exist_ok=True)
        os.chmod(ACTION_QUEUE_DIR, 0o770)
    now = datetime.now()
    payload = {"action": action, "args": args, "queued_at": now.isoformat()}
    fd, tmp = tempfile.mkstemp(prefix=TMP_PREFIX, suffix=".part", dir=ACTION_QUEUE_DIR)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f)
        path = os.path.join(ACTION_QUEUE_DIR, f"action-{now.strftime('%Y%m%d%H%M%S%f')}-"
                                              f"{uuid.uuid4().hex[:8]}.json")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path
