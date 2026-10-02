# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Arnaud Ortais
# Dual-licensed: AGPL-3.0 (open source) or Commercial License — see LICENSE and LICENSE-COMMERCIAL.
"""
modes.py — le vocabulaire des modes d'accès, déclaré UNE fois.

POURQUOI CE MODULE EXISTE. Les trois modes d'accès étaient écrits en clair dans neuf
fichiers, et le vocabulaire avait déjà dérivé de deux façons :

  1. `free` (override de journée) et `permissive` (créneau de planning) désignaient LA
     MÊME CHOSE. D'où deux libellés « Libre » pour deux clés, et un mot qui ne veut plus
     rien dire pour le parent ;
  2. les libellés étaient internationalisés d'un côté (i18n/mode.*) et codés en dur en
     français de l'autre (scheduler.MODE_LABELS, claude_agent), donc un boîtier en anglais
     lisait « 📚 Travail » dans ses rapports.

Ce module est désormais la SEULE source. Un test refuse toute réapparition d'un mode
écrit en clair ailleurs.

LES MODES, ET CE QU'ILS SIGNIFIENT

  OFF       aucun accès. C'est le coucher, l'heure d'école, la punition.
  HOMEWORK  ce que la grille de l'enfant autorise pour les devoirs.
  FREE      ce que la grille de l'enfant autorise pour son temps libre.

Ce que chaque mode autorise dépend de l'ENFANT, pas du mode : c'est tout l'objet de la
grille d'accès (cf. le champ `access` d'un profil). « Temps libre » n'ouvre pas les mêmes
catégories à 6 ans et à 16 ans, et c'est la raison d'être de ce découpage.

NORMAL n'est pas un mode d'accès mais l'ABSENCE de dérogation : il ne s'applique à rien,
il annule. Il n'a donc sa place que dans les overrides de journée.

NOMS CHOISIS. Anciennes clés : blocked, work, permissive, free, normal. `permissive` était
un jugement ; `work` évoquait le travail du parent ; `blocked` se confondait avec « site
bloqué », au point que le journal disait « Tentative d'accès à X (bloqué) » et
« Changement de plage : Bloqué » pour deux choses sans rapport. Le doublon free/permissive
disparaît par unification : un seul mode, un seul nom.
"""

# ------------------------------------------------------------------ #
#  Les modes                                                          #
# ------------------------------------------------------------------ #

OFF      = "off"        # aucun accès
HOMEWORK = "homework"   # ce que la grille autorise pour les devoirs
FREE     = "free"       # ce que la grille autorise pour le temps libre

# Absence de dérogation. Jamais un état d'accès : on ne peut pas « être en normal ».
NORMAL = "normal"

# Modes qu'un créneau de planning peut porter, du plus fermé au plus ouvert. L'ordre est
# celui des écrans : il fait autorité pour l'affichage.
SLOT_MODES = (OFF, HOMEWORK, FREE)

# Valeurs acceptées pour une dérogation de journée : les trois modes, plus l'annulation.
OVERRIDE_MODES = SLOT_MODES + (NORMAL,)

# Modes qui laissent PASSER du trafic. Tout le reste ferme, y compris une valeur inconnue :
# le fail-safe de ce produit est de couper, jamais d'ouvrir par accident.
OPEN_MODES = frozenset({HOMEWORK, FREE})

# Clé i18n du libellé de chaque mode. Les libellés eux-mêmes vivent dans i18n/, jamais
# ici : ce module ne parle aucune langue.
LABEL_KEYS = {
    OFF:      "mode.off",
    HOMEWORK: "mode.homework",
    FREE:     "mode.free",
    NORMAL:   "mode.normal",
}

# ------------------------------------------------------------------ #
#  Compatibilité avec l'ancien vocabulaire                            #
# ------------------------------------------------------------------ #

# Un planning écrit avant ce renommage porte les anciennes clés. La traduction se fait à
# la LECTURE, pour qu'un boîtier mis à jour n'applique pas un mode inconnu, ce qui le
# ferait retomber sur « fermé » et couperait un enfant sans explication.
#
# `free` est le cas subtil : c'était le nom de la dérogation « journée libre », donc la
# même chose que `permissive`. Les deux convergent vers FREE, ce qui est précisément
# l'unification recherchée.
_LEGACY = {
    "blocked":    OFF,
    "work":       HOMEWORK,
    "permissive": FREE,
    "free":       FREE,
    "normal":     NORMAL,
}


def normalize(value) -> str:
    """Mode canonique pour une valeur venue d'une configuration ou de la base.

    Renvoie une chaîne vide pour une valeur inconnue plutôt que d'inventer un mode :
    l'appelant décide, et la décision par défaut du produit est de fermer.
    """
    key = str(value or "").strip().lower()
    if key in OVERRIDE_MODES:
        return key
    return _LEGACY.get(key, "")


def is_open(mode) -> bool:
    """Ce mode laisse-t-il passer du trafic ? Une valeur inconnue ferme."""
    return normalize(mode) in OPEN_MODES


def is_slot_mode(mode) -> bool:
    """Ce mode peut-il être porté par un créneau de planning ? (NORMAL n'en est pas un.)"""
    return normalize(mode) in SLOT_MODES


def label_key(mode) -> str:
    """Clé i18n du libellé, ou chaîne vide si le mode est inconnu."""
    return LABEL_KEYS.get(normalize(mode), "")
