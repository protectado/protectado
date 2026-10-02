# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Arnaud Ortais
# Dual-licensed: AGPL-3.0 (open source) or Commercial License — see LICENSE and LICENSE-COMMERCIAL.
"""
access_grid.py — ce que chaque mode autorise, POUR CET ENFANT.

CE QUE ÇA REMPLACE. La liste des catégories bloquées était écrite en dur dans une
requête SQL, identique pour tous : le mode devoirs coupait divertissement et social, le
temps libre ne coupait qu'adulte et extrémisme. Le même réglage pour un enfant de 6 ans et
un adolescent de 16 ans, ce qui est absurde dans les deux sens. À 6 ans, « temps libre »
ouvrait tout le divertissement et tous les réseaux sociaux, ce qu'aucun parent ne veut. À
16 ans, couper le social pendant les devoirs est précisément l'intérêt du produit, mais
rien ne le distinguait du reste.

CE QUE LA GRILLE EST. Pour chaque mode ouvert, la liste des catégories AUTORISÉES. Tout ce
qui n'y figure pas est bloqué. C'est plus sûr que l'inverse : une catégorie ajoutée au
produit demain sera bloquée par défaut partout, et non ouverte chez tous les enfants.

TROIS RÈGLES QUI NE SE CONFIGURENT PAS, et qui n'apparaissent donc pas dans la grille :

  1. `adult` et `extremism` sont bloqués dans TOUS les modes, à TOUT âge. C'est la
     promesse du produit, et un parent ne peut pas la lever, même par erreur. Une case
     à cocher ici serait une promesse conditionnelle ;
  2. `cdn` n'est JAMAIS bloqué. C'est de l'infrastructure partagée : la couper casse les
     sites autorisés sans rien protéger ;
  3. `unknown` n'est jamais bloqué non plus. Tout domaine est inconnu jusqu'à sa
     classification, et bloquer l'inconnu couperait l'internet à chaque site neuf. C'est
     un choix assumé, et c'est le pipeline de classification qui réduit ce trou, pas la
     grille.

LA TRANCHE D'ÂGE DONNE LE DÉFAUT, jamais la règle finale : le parent peut corriger la
grille de chaque enfant, dans les deux sens, sauf sur les trois règles ci-dessus.
"""

import modes
import privacy

# Catégories qui APPARAISSENT dans la grille, dans l'ordre d'affichage. Les autres sont
# régies par les trois règles ci-dessus et ne se cochent pas.
GRID_CATEGORIES = ("education", "work", "other", "entertainment", "social")

# Bloquées partout, toujours. Cf. règle 1.
ALWAYS_BLOCKED = frozenset({"adult", "extremism"})

# Jamais bloquées. Cf. règles 2 et 3.
ALWAYS_ALLOWED = frozenset({"cdn", "unknown"})

# Défauts par tranche d'âge (cf. privacy.age_band, qui fait autorité sur les tranches).
#
# Deux choses à assumer dans ce tableau. À 6-9 ans, le temps libre N'OUVRE PAS le
# divertissement : la soupape est l'exception par domaine, le parent autorisant nommément
# ce qu'il veut, plutôt qu'une catégorie entière. Et 13-15 et 16+ ont la MÊME grille : la
# différence à 16 ans vit dans le niveau de vie privée, pas dans l'accès. Mieux vaut le
# dire que d'inventer une distinction pour remplir une ligne.
DEFAULTS_BY_BAND = {
    "6-9": {
        modes.HOMEWORK: ("education", "work"),
        modes.FREE:     ("education", "work", "other"),
    },
    "10-12": {
        modes.HOMEWORK: ("education", "work"),
        modes.FREE:     ("education", "work", "other", "entertainment"),
    },
    "13-15": {
        modes.HOMEWORK: ("education", "work", "other"),
        modes.FREE:     ("education", "work", "other", "entertainment", "social"),
    },
    "16+": {
        modes.HOMEWORK: ("education", "work", "other"),
        modes.FREE:     ("education", "work", "other", "entertainment", "social"),
    },
}

# Tranche retenue quand l'âge est inconnu : la plus protectrice. On ne suppose pas un
# adolescent, exactement comme privacy.default_level_for_age.
BAND_FALLBACK = "6-9"


def default_grid(profile: dict) -> dict:
    """Grille par défaut d'un profil, déduite de sa tranche d'âge."""
    band = privacy.age_band(privacy.age_of(profile or {}))
    table = DEFAULTS_BY_BAND.get(band) or DEFAULTS_BY_BAND[BAND_FALLBACK]
    return {mode: set(cats) for mode, cats in table.items()}


def grid_of(profile: dict) -> dict:
    """Grille EFFECTIVE d'un profil : {mode ouvert: catégories autorisées}.

    La grille explicite du profil (champ `access`) fait foi quand elle est lisible, sinon
    on retombe sur le défaut de la tranche. Chaque mode est traité séparément : un profil
    qui n'a personnalisé que ses devoirs garde le défaut pour son temps libre.

    NETTOYAGE SYSTÉMATIQUE : les catégories inconnues sont écartées, et celles qui ne se
    configurent pas ne peuvent pas être introduites par une configuration écrite à la
    main. La grille rendue ici est donc toujours sûre, quelle que soit la source.
    """
    defaults = default_grid(profile)
    explicit = (profile or {}).get("access")
    if not isinstance(explicit, dict):
        return defaults

    grid = {}
    for mode in (modes.HOMEWORK, modes.FREE):
        raw = explicit.get(mode)
        if not isinstance(raw, (list, tuple, set)):
            grid[mode] = defaults[mode]
            continue
        grid[mode] = {str(c).strip().lower() for c in raw} & set(GRID_CATEGORIES)
    return grid


def allowed_categories(profile: dict, mode) -> set:
    """Catégories autorisées pour ce profil dans ce mode, règles non configurables incluses.

    Un mode fermé n'autorise rien du tout, pas même l'infrastructure : couper veut dire
    couper. C'est le mode `off`, et le blocage y est porté par un joker DNS, pas par une
    liste de catégories.
    """
    canonical = modes.normalize(mode)
    if not modes.is_open(canonical):
        return set()
    return grid_of(profile).get(canonical, set()) | set(ALWAYS_ALLOWED)


def blocked_categories(profile: dict, mode) -> set:
    """Catégories bloquées pour ce profil dans ce mode.

    C'est le complément de l'autorisation sur l'ensemble des catégories connues, PLUS les
    deux catégories bloquées à tout âge. Calculer le blocage par complément et non par
    liste est ce qui garantit qu'une catégorie ajoutée au produit demain sera bloquée
    partout par défaut, et non ouverte chez tous les enfants.
    """
    autorisees = allowed_categories(profile, mode)
    connues = set(GRID_CATEGORIES) | set(ALWAYS_BLOCKED) | set(ALWAYS_ALLOWED)
    return (connues - autorisees) | set(ALWAYS_BLOCKED)


def sanitize(access) -> dict:
    """Grille reçue de l'interface, ramenée à ce qui est configurable.

    Écarte les modes qui ne se configurent pas, les catégories inconnues, et surtout les
    deux catégories bloquées à tout âge : un formulaire bricolé ne doit pas pouvoir
    autoriser `adult` pour un enfant.
    """
    out = {}
    if not isinstance(access, dict):
        return out
    for mode in (modes.HOMEWORK, modes.FREE):
        raw = access.get(mode)
        if not isinstance(raw, (list, tuple, set)):
            continue
        retenues = {str(c).strip().lower() for c in raw} & set(GRID_CATEGORIES)
        out[mode] = sorted(retenues)
    return out


def describe(profile: dict) -> dict:
    """Grille d'un profil sous une forme prête à afficher, avec l'origine de chaque ligne.

    `custom` dit si la ligne vient du parent ou de la tranche d'âge : l'interface doit
    pouvoir montrer « réglage par défaut pour un enfant de 8 ans » sans le deviner.
    """
    defaults = default_grid(profile)
    effective = grid_of(profile)
    return {
        "band": privacy.age_band(privacy.age_of(profile or {})),
        "categories": list(GRID_CATEGORIES),
        "always_blocked": sorted(ALWAYS_BLOCKED),
        "modes": {
            mode: {
                "allowed": sorted(effective.get(mode, set())),
                "default": sorted(defaults.get(mode, set())),
                "custom": effective.get(mode, set()) != defaults.get(mode, set()),
            }
            for mode in (modes.HOMEWORK, modes.FREE)
        },
    }
