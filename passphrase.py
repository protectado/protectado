# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Arnaud Ortais
# Dual-licensed: AGPL-3.0 (open source) or Commercial License — see LICENSE and LICENSE-COMMERCIAL.
"""
passphrase.py — Génération de la clé du Wi-Fi des enfants.

Forme : trois mots prononçables et un groupe de trois chiffres, séparés par des tirets
(« bamito-kesuna-porelu-472 »). Le parent la dicte une fois par appareil : elle doit
s'entendre et se taper sans faute, d'où des syllabes consonne + voyelle, sans accent et
sans lettre dont la prononciation change d'une langue à l'autre.

POURQUOI DES MOTS FABRIQUÉS PLUTÔT QU'UNE LISTE
Une liste de vrais mots est un corpus : il faut l'écrire, le traduire dans chaque langue
de l'interface, et sa TAILLE plafonne la force de la clé. La précédente comptait 28 mots
français — 19 656 combinaisons, 14,3 bits, épuisables hors ligne en un instant à partir
d'une poignée de main WPA2, d'autant que le dépôt est public et la liste avec lui.
Fabriquer les mots supprime le corpus et rend la force explicite : elle ne dépend plus
de ce qu'on a pensé à écrire, mais d'un calcul.

FORCE, EXACTEMENT
14 consonnes × 5 voyelles = 70 syllabes ; 3 syllabes par mot = 343 000 mots possibles
(18,4 bits) ; 3 mots et 3 chiffres = 2^65,1 combinaisons. Connaître ce fichier n'aide en
rien : il n'y a rien à deviner d'autre que le tirage lui-même.

DEUX PROPRIÉTÉS À NE PAS PERDRE
1. Le tirage vient de secrets, jamais de random : le Mersenne Twister est un générateur
   de simulation, prévisible dès qu'on observe assez de sorties. Ici la sortie EST la
   clé du réseau.
2. Les syllabes restent en ASCII minuscule. Un accent ou une cédille se dicte mal, se
   tape mal sur un clavier de téléviseur, et n'est pas garanti par tous les clients WPA2.
"""

import re
import secrets

# Consonnes retenues : celles qui se prononcent de la même façon en français, anglais,
# espagnol et portugais. Écartées : c, g devant e/i, h, j, q, w, x, y — leur son change
# d'une langue à l'autre, donc la clé se dicterait différemment selon qui la lit.
_CONSONANTS = "bdfgklmnprstvz"
_VOWELS = "aeiou"
SYLLABLES = [c + v for c in _CONSONANTS for v in _VOWELS]   # 70

SYLLABLES_PER_WORD = 3      # 70^3 = 343 000 mots possibles (18,4 bits par mot)
WORDS_PER_KEY = 3
DIGITS = 3                  # groupe final, 000–999

# Filet contre les suites malheureuses. Un tirage de syllabes peut former, par accident,
# une séquence grossière ou blessante dans l'une des quatre langues — la clé est lue par
# des enfants et dictée en famille. On retire le tirage et on recommence plutôt que de
# livrer ça. Motifs recherchés n'importe où dans la clé, accents déjà exclus par
# construction. Liste volontairement courte : elle ne prétend pas être exhaustive, elle
# écarte les cas les plus probables sans devenir un corpus à son tour.
_REJECT = re.compile(
    r"cul|con|pute|bite|nique|zizi|kaka|pipi|puta|puto|mierda|cara?jo|caga|"
    r"fode|foda|merda|cona|piru|shit|fuck|dick|piss|tits|nazi|"
    r"suka|kill|dead",
    re.IGNORECASE,
)


def _word() -> str:
    return "".join(secrets.choice(SYLLABLES) for _ in range(SYLLABLES_PER_WORD))


def generate(lang: str = "") -> str:
    """Clé Wi-Fi lisible : « bamito-kesuna-porelu-472 ».

    `lang` est accepté et ignoré : les syllabes sont communes aux quatre langues de
    l'interface. Le paramètre reste dans la signature parce que les appelants le
    passaient à la version à listes de mots, et qu'une clé reste une clé.
    """
    for _ in range(64):                      # bornes : le rejet est rarissime
        words = [_word() for _ in range(WORDS_PER_KEY)]
        # Chiffre par chiffre : le groupe garde ses zéros de tête (« 042 » est un tirage
        # comme un autre), donc les 1000 valeurs sont équiprobables et la longueur
        # dictée au parent ne varie jamais.
        number = "".join(str(secrets.randbelow(10)) for _ in range(DIGITS))
        key = "-".join(words + [number])
        if not _REJECT.search(key):
            return key
    # Inatteignable en pratique ; on ne renvoie jamais une clé faible faute de mieux.
    raise RuntimeError("génération de clé impossible")
