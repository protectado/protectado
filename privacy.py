# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Arnaud Ortais
# Dual-licensed: AGPL-3.0 (open source) or Commercial License — see LICENSE and LICENSE-COMMERCIAL.
"""
privacy.py — proportionnalité et minimisation des données.

Deux responsabilités distinctes :

1. PSEUDONYMISATION DES SORTIES (§ 13). Rien de nominatif ne doit quitter le boîtier.
   Avant cette version, chaque appel OpenRouter transportait « Alice (12 ans) », les
   clés de profil (dérivées des prénoms), les adresses IP des appareils et les messages
   d'événements en clair. Le modèle n'a besoin d'aucune de ces informations : une
   étiquette stable et une TRANCHE d'âge suffisent à calibrer le ton. Les vrais prénoms
   sont ré-substitués à la RÉCEPTION, côté serveur, pour l'affichage uniquement.

2. NIVEAU DE VIE PRIVÉE PAR PROFIL (§ 11). Un enfant de 9 ans et un adolescent de 17 ans
   ne justifient pas la même reconstitution a posteriori. Le niveau ne change JAMAIS ce
   que le produit empêche sur le moment (blocage, planning, alertes) : il ne change que
   ce que le parent peut reconstituer après coup.
"""

import re
from datetime import datetime

# ------------------------------------------------------------------ #
#  Niveaux de vie privée                                              #
# ------------------------------------------------------------------ #

DETAILED = "detailed"   # fenêtres de 5 min — reconstitution fine
SUMMARY  = "summary"    # agrégats par demi-journée
MINIMAL  = "minimal"    # totaux quotidiens, sans aucun horaire

LEVELS = (DETAILED, SUMMARY, MINIMAL)

# Rapports autorisés par niveau. Le rapport quotidien disparaît dès `minimal` : un point
# quotidien sur un adolescent de 16 ans relève du suivi, pas de la protection.
REPORTS_BY_LEVEL = {
    DETAILED: ("daily", "weekly", "monthly"),
    SUMMARY:  ("daily", "weekly"),
    MINIMAL:  ("weekly",),
}


def default_level_for_age(age) -> str:
    """Niveau par défaut déduit de l'âge. L'âge n'est qu'un DÉFAUT : le parent peut
    changer le niveau ensuite, dans les deux sens."""
    try:
        age = int(age)
    except (TypeError, ValueError):
        return DETAILED          # âge inconnu : on ne suppose pas un adolescent
    if age >= 16:
        return MINIMAL
    if age >= 13:
        return SUMMARY
    return DETAILED


def level_of(profile: dict) -> str:
    """Niveau effectif d'un profil : explicite s'il est posé, sinon déduit de l'âge."""
    lvl = (profile or {}).get("privacy_level")
    return lvl if lvl in LEVELS else default_level_for_age((profile or {}).get("age"))


def reports_allowed(profile: dict) -> tuple:
    return REPORTS_BY_LEVEL[level_of(profile)]


def age_band(age) -> str:
    """Tranche d'âge — suffisante pour calibrer le ton, insuffisante pour identifier."""
    try:
        age = int(age)
    except (TypeError, ValueError):
        return "inconnu"
    if age >= 16:
        return "16+"
    if age >= 13:
        return "13-15"
    if age >= 10:
        return "10-12"
    return "moins de 10"


# ------------------------------------------------------------------ #
#  Pseudonymisation                                                   #
# ------------------------------------------------------------------ #

_IPV4 = re.compile(r'\b(?:\d{1,3}\.){3}\d{1,3}\b')
_IPV6 = re.compile(r'\b(?:[0-9A-Fa-f]{1,4}:){2,7}[0-9A-Fa-f]{1,4}\b')
_MAC  = re.compile(r'\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b')

REDACTED_DEVICE = "[appareil]"


def _kid_keys(config: dict) -> list:
    """Clés des profils enfants, dans un ordre STABLE (tri alphabétique).

    L'ordre du dict de configuration dépend de l'ordre d'écriture : trier garantit que
    « Enfant 1 » désigne le même enfant d'un appel à l'autre, y compris après une
    réécriture de config.json. Le profil `monitoring` (appareil parent) est exclu.
    """
    return sorted(
        k for k, p in (config.get("profiles") or {}).items()
        if (p or {}).get("mode") != "monitoring"
    )


# Étiquette neutre : préfixe + numéro. Le préfixe est volontairement le même dans les
# quatre langues côté SORTANT (le modèle reçoit toujours « Enfant N ») ; c'est à la
# ré-identification qu'on tolère les variantes que le modèle peut produire.
ALIAS_PREFIX = "Enfant"


def assign_alias(config: dict, key: str) -> str:
    """Attribue une étiquette DÉFINITIVE à un profil, et la retourne.

    Le numéro vient d'un compteur qui ne redescend jamais : supprimer un enfant ne
    libère pas son numéro. Sans cela, l'étiquette était déduite du rang alphabétique
    de la clé, et ajouter un enfant dont la clé passe avant les autres décalait toutes
    les correspondances — « Enfant 1 » désignant alors quelqu'un d'autre d'un appel au
    suivant. Le décalage ne provoquait pas de fausse attribution (les rapports sont
    stockés en clair puis re-pseudonymisés avec la correspondance courante), mais une
    étiquette qui change de personne reste un défaut de conception.
    """
    profiles = config.setdefault("profiles", {})
    profile = profiles.setdefault(key, {})
    if profile.get("alias"):
        return profile["alias"]
    section = config.setdefault("privacy", {})
    used = {int(m.group(1))
            for p in profiles.values()
            for m in [re.match(rf'^{ALIAS_PREFIX}\s*(\d+)$', str((p or {}).get("alias") or ""))]
            if m}
    counter = max([int(section.get("alias_counter") or 0)] + list(used)) + 1
    section["alias_counter"] = counter
    profile["alias"] = f"{ALIAS_PREFIX} {counter}"
    return profile["alias"]


def alias_map(config: dict) -> dict:
    """clé de profil → étiquette neutre (« Enfant 1 », « Enfant 2 », …).

    L'étiquette posée dans le profil (`alias`) fait FOI. Le repli par rang alphabétique
    ne sert plus qu'aux profils créés avant l'introduction du champ : il est stable tant
    que l'ensemble des profils ne change pas, et assign_alias() le fige dès la première
    modification du profil.
    """
    out = {}
    profiles = config.get("profiles") or {}
    for i, key in enumerate(_kid_keys(config), start=1):
        explicit = (profiles.get(key) or {}).get("alias")
        out[key] = explicit or f"{ALIAS_PREFIX} {i}"
    # Le profil parent n'est jamais nommé non plus.
    for key, p in profiles.items():
        if (p or {}).get("mode") == "monitoring":
            out[key] = "Appareil parent"
    return out


def reverse_alias_map(config: dict) -> dict:
    """étiquette → vrai prénom (pour la RÉ-SUBSTITUTION à l'affichage)."""
    profiles = config.get("profiles") or {}
    out = {}
    for key, label in alias_map(config).items():
        name = (profiles.get(key) or {}).get("name") or key
        out[label] = name
    return out


def scrub_text(text, aliases: dict, config: dict = None) -> str:
    """Retire d'un texte libre tout ce qui identifie : prénoms, clés de profil,
    adresses IP et MAC. Utilisé sur tout ce qui part vers un tiers."""
    if not text:
        return text
    s = str(text)
    profiles = (config or {}).get("profiles") or {}
    # Prénoms d'abord (plus longs et plus spécifiques que les clés), puis clés.
    pairs = []
    for key, label in aliases.items():
        name = (profiles.get(key) or {}).get("name")
        if name:
            pairs.append((name, label))
        pairs.append((key, label))
    for needle, label in sorted(pairs, key=lambda t: -len(t[0])):
        if needle:
            s = re.sub(r'\b%s\b' % re.escape(needle), label, s, flags=re.IGNORECASE)
    s = _IPV4.sub(REDACTED_DEVICE, s)
    s = _IPV6.sub(REDACTED_DEVICE, s)
    s = _MAC.sub(REDACTED_DEVICE, s)
    return s


def _looks_like_device(value) -> bool:
    v = str(value or "")
    return bool(_IPV4.fullmatch(v) or _IPV6.fullmatch(v) or _MAC.fullmatch(v))


def scrub_events(events, aliases: dict, config: dict = None) -> list:
    """Prépare des événements pour un envoi sortant.

    Retire : le champ `domain` quand il contient en réalité une IP (les événements de
    mode adulte et de contournement DNS y stockent l'adresse de l'appareil), les
    messages en clair, et les paramètres qui portent un identifiant d'appareil
    (`event.device_free_access` place un hostname ou une MAC dans `label`).
    """
    out = []
    for e in events or []:
        e = dict(e)
        domain = e.get("domain") or ""
        if _looks_like_device(domain):
            domain = REDACTED_DEVICE
        params = e.get("params")
        if isinstance(params, str) and params:
            try:
                import json as _json
                params = _json.loads(params)
            except Exception:
                params = {}
        if isinstance(params, dict):
            params = {k: (REDACTED_DEVICE if _looks_like_device(v)
                          else scrub_text(v, aliases, config) if isinstance(v, str) else v)
                      for k, v in params.items()}
        else:
            params = {}
        out.append({
            "time":    (e.get("timestamp") or "")[11:16],
            "profile": aliases.get(e.get("profile"), e.get("profile")),
            "type":    e.get("type"),
            "domain":  domain,
            # `message_key` + `params` remplacent le message français en clair : ils
            # décrivent l'événement sans transporter de texte libre potentiellement
            # nominatif, et sont plus compacts.
            "event":   e.get("message_key") or scrub_text(e.get("message"), aliases, config),
            "params":  params,
        })
    return out


def scrub_usage(usage_by_profile: dict, aliases: dict) -> dict:
    """Ré-indexe un dict {clé de profil: …} par étiquette neutre."""
    return {aliases.get(k, k): v for k, v in (usage_by_profile or {}).items()}


# Variantes d'étiquette que le modèle produit en pratique : il ne réécrit pas toujours
# « Enfant 1 » à la lettre. Il traduit (Child/Niño/Criança/Kind), change la casse, colle
# le numéro, ou intercale « n° ». Un simple remplacement littéral laissait passer ces
# formes et le parent lisait des rapports parlant d'« Enfant 2 ».
_ALIAS_WORDS = r'(?:enfants?|child|kid|ni[ñn]o|crian[çc]a|bambino|kind)'
# L'article élidé qui précède est absorbé : « L'enfant n°1 » doit donner « Alice »,
# pas « L'Alice ».
_ALIAS_RE = re.compile(
    rf"(?:\b[lL]['’]\s*)?\b{_ALIAS_WORDS}\s*(?:n[°ºo]\s*|#\s*|number\s*)?(\d{{1,2}})\b",
    re.IGNORECASE)


def restore_names(text, config: dict) -> str:
    """Ré-substitue les vrais prénoms dans une réponse du modèle, à l'AFFICHAGE.

    Seule direction où le nominatif réapparaît, et elle est purement locale : le tiers
    n'a jamais vu que « Enfant 1 ». Tolérante aux variantes de forme (casse, langue,
    « n° », numéro collé) — mais JAMAIS devinette : un numéro qui ne correspond à aucune
    étiquette connue est laissé tel quel plutôt qu'attribué au hasard. Attribuer à Bruno
    un constat portant sur Alice serait un dommage bien pire qu'une phrase impersonnelle.
    """
    if not text:
        return text
    by_number = {}
    for label, name in reverse_alias_map(config).items():
        m = re.match(rf'^{ALIAS_PREFIX}\s*(\d+)$', str(label))
        if m:
            by_number[m.group(1)] = name
        else:
            by_number.setdefault(label, name)

    def _sub(m):
        return by_number.get(m.group(1), m.group(0))

    s = _ALIAS_RE.sub(_sub, str(text))
    # Étiquettes personnalisées ne suivant pas le motif « Enfant N ».
    for label, name in sorted(reverse_alias_map(config).items(), key=lambda t: -len(t[0])):
        if not re.match(rf'^{ALIAS_PREFIX}\s*\d+$', str(label)):
            s = re.sub(re.escape(label), name, s, flags=re.IGNORECASE)
    return s


def has_unresolved_reference(text, config: dict) -> bool:
    """Le texte vise-t-il UN enfant en particulier sans qu'on ait pu l'identifier ?

    Repli assumé : quand le modèle reformule complètement (« le premier enfant », « the
    older one »), aucune règle ne peut retrouver de qui il s'agit sans DEVINER. On ne
    devine pas — attribuer à Bruno un constat portant sur Alice serait bien pire qu'une
    phrase impersonnelle. On le signale au parent, qui sait de quels enfants il s'agit.

    Un constat COLLECTIF (« les enfants ont réduit leur temps d'écran ») ne vise personne
    en particulier : il est parfaitement lisible tel quel et ne déclenche rien.
    """
    if not text:
        return False
    s = str(text)
    names = {(p or {}).get("name") for p in (config.get("profiles") or {}).values()}
    if any(n and n in s for n in names):
        return False        # au moins un enfant est nommé : la réponse est exploitable

    # a) Une étiquette numérotée subsiste = numéro inconnu, volontairement non attribué.
    if re.search(rf'\b{_ALIAS_WORDS}\s*(?:n[°ºo]\s*|#\s*|number\s*)?\d', s, re.IGNORECASE):
        return True

    # b) Une désignation SINGULIÈRE et distinctive : le modèle vise un enfant précis
    #    mais par périphrase. C'est le cas irréductible.
    ordinals = (r"premi(?:er|ère)|deuxi[èe]me|second[e]?|troisi[èe]me|a[îi]n[ée]|cadet(?:te)?|"
                r"plus (?:jeune|[âa]g[ée])|first|second|third|older|younger|eldest|youngest|"
                r"mayor|menor|primer[oa]?|segund[oa]|mais (?:novo|velho)|primeir[oa]|segund[oa]")
    if re.search(rf'\b(?:{ordinals})\b[^.]{{0,30}}\b{_ALIAS_WORDS}\b', s, re.IGNORECASE):
        return True
    if re.search(rf'\b{_ALIAS_WORDS}\b[^.]{{0,30}}\b(?:{ordinals})\b', s, re.IGNORECASE):
        return True
    return False


# ------------------------------------------------------------------ #
#  Réglages globaux                                                   #
# ------------------------------------------------------------------ #

DEFAULT_RETENTION_DAYS = 90


def settings(config: dict) -> dict:
    return (config or {}).get("privacy") or {}


def retention_days(config: dict) -> int:
    """0 = conservation illimitée (l'interface l'affiche comme un avertissement)."""
    try:
        v = int(settings(config).get("retention_days", DEFAULT_RETENTION_DAYS))
    except (TypeError, ValueError):
        return DEFAULT_RETENTION_DAYS
    return max(0, v)


def share_with_ai(config: dict) -> bool:
    """Interrupteur explicite. Par défaut vrai SI une clé est configurée : installer une
    clé est déjà un consentement, mais le parent doit pouvoir couper sans la retirer."""
    s = settings(config)
    if "share_with_ai" in s:
        return bool(s["share_with_ai"])
    return bool(((config or {}).get("openrouter") or {}).get("api_key"))
