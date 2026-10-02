# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Arnaud Ortais
# Dual-licensed: AGPL-3.0 (open source) or Commercial License — see LICENSE and LICENSE-COMMERCIAL.
"""
domain_classifier.py — Collecte et catégorisation des domaines DNS.

Deux rôles :
  1. Python : collecte les domaines vus, comptabilise hits + contexte horaire
  2. Claude  : catégorise une fois par jour (appelé par daily_report)

Pipeline de classification à 3 niveaux :
  Niveau 1 — Cache local DB          → coût zéro, instantané
  Niveau 2 — Cloudflare DoH (tri-resolver) → coût zéro, détecte adult/malware/extremism
  Niveau 3 — Claude via OpenRouter   → rare, uniquement pour les vraiment inconnus

Catégories :
  education    → jamais bloqué (khanacademy, wikipedia, schoolwork...)
  work         → jamais bloqué en mode devoirs (c'est une CATÉGORIE, pas le mode)
  entertainment→ bloqué en mode devoirs (youtube, netflix, twitch...)
  social       → bloqué en mode devoirs (instagram, tiktok, discord...)
  adult        → bloqué dans TOUS les modes
  extremism    → toujours bloqué
  cdn          → ignoré (cloudflare, fastly, akamai... — trop générique)
  unknown      → pas encore catégorisé
"""

import json
import urllib.request
from urllib.parse import quote as urlquote
from datetime import datetime, timedelta
from openai import OpenAI

import access_grid
import doh_catalog
from access_control import enforcement_mode
import modes
import services
import database as db

# REPRISE DES ÉCHECS CLOUDFLARE. Quand le résolveur ne conclut pas, le domaine est tout
# de même marqué « essayé par Cloudflare » pour ne pas le réinterroger à chaque passe.
# Cette marque était DÉFINITIVE : get_uncategorized ne reprenait que les domaines jamais
# marqués, donc un domaine inconnu de Cloudflare un jour ne l'était plus jamais réinterrogé,
# même devenu connu depuis. Les catégories de Cloudflare bougent, les nôtres non.
#
# La reprise se fait sans colonne supplémentaire, avec les deux champs qui existent :
# `categorized_at` donne l'ancienneté de la tentative, `hits_total` dit si le domaine
# compte. Un domaine très visité repasse vite, un domaine marginal attend. Mesurer « les
# visites depuis la dernière tentative » demanderait de mémoriser le compteur au moment de
# la tentative, donc une colonne : le volume ABSOLU suffit à décider qui mérite une
# seconde chance en priorité.
# Catégories que le produit sait exploiter. Une réponse du modèle hors de cet ensemble
# était écrite telle quelle en base : « Streaming » ou « Video » devenaient une catégorie
# que get_active_blacklist ne reconnaît pas, donc un domaine catégorisé mais jamais bloqué,
# et invisible dans les compteurs par catégorie. On refuse au lieu d'écrire.
VALID_CATEGORIES = frozenset({
    "education", "work", "entertainment", "social", "adult", "extremism", "cdn", "other",
})

# « Je ne sais pas » est une réponse DIFFÉRENTE de « ce site est banal ».
#
# Le prompt demandait `other` dans les deux cas, alors que `other` est une catégorie
# légitime (moteurs de recherche, météo, actualités générales). Les deux cas devenaient
# indiscernables, et comme la reprise ne cherche que `category = 'unknown'`, AUCUN des
# deux n'était jamais repris : un aveu d'ignorance était enregistré comme un verdict.
# La valeur d'ignorance est donc celle que la base utilise déjà pour « pas classé », ce
# qui rend la reprise automatique sans code supplémentaire.
UNKNOWN_CATEGORY = "unknown"

CLOUDFLARE_RETRY_DAYS      = 7    # toute marque plus vieille que ça est réessayable
CLOUDFLARE_RETRY_DAYS_BUSY = 1    # …ramené à un jour pour un domaine très visité
CLOUDFLARE_RETRY_BUSY_HITS = 100  # seuil de « très visité »

# Domaines d'INFRASTRUCTURE PARTAGÉE, à ignorer : ils servent n'importe quel site, donc
# leur nom ne dit rien de ce que l'enfant regardait.
#
# RÈGLE À NE PAS ENFREINDRE. Un domaine de LIVRAISON appartenant à un service identifiable
# n'a rien à faire ici, même si son nom crie « CDN ». googlevideo.com est la vidéo
# YouTube, nflxvideo.net est le flux Netflix, fbcdn.net est le média Instagram et
# Facebook : ce sont les domaines les plus bavards du produit et ils portent du VRAI
# usage. Les ranger en infrastructure les ferait disparaître du catalogue, donc des
# compteurs montrés au parent, de l'estimation du temps passé et de la détection
# d'anomalies. Leur place est dans OBVIOUS_CATEGORIES, avec la catégorie de leur service.
# Le test tests/test_classification.py fige cette séparation dans les deux sens.
CDN_PATTERNS = [
    "cloudflare.com", "cloudfront.net", "fastly.net", "akamai.net",
    "akamaized.net", "akamaitech.net", "cdn.net", "llnwd.net",
    "edgesuite.net", "edgekey.net", "footprint.net", "level3.net",
    "amazonaws.com", "googleusercontent.com", "gstatic.com",
    "doubleclick.net", "googlesyndication.com", "google-analytics.com",
    "googletagmanager.com", "googleapis.com",
]

# Catégorisation évidente sans IA, DÉRIVÉE des services (cf. services.py) : la catégorie
# d'un domaine est celle de son service, et elle n'est écrite qu'une fois.
#
# Ce dictionnaire était une copie de la même information, avec les groupes de services en
# COMMENTAIRE (« YouTube + satellites »). Deux listes des mêmes domaines finissent par
# diverger : c'est ainsi que l'entrée de fbcdn.net avait cessé d'avoir un effet sans que
# personne ne le voie. Un test vérifie que chaque domaine de service est bien ici.
OBVIOUS_CATEGORIES = services.categories_by_domain()


def reload_services() -> int:
    """Relit le catalogue des services et REFAIT la catégorisation qui en dérive.

    Point de passage unique du rechargement à chaud : le catalogue peut changer sans
    redémarrage (publication qui ne touche que catalog/, appoint local écrit par un
    parent), et OBVIOUS_CATEGORIES ci-dessus est un instantané construit à l'import.

    Recharger l'un sans l'autre serait pire que ne rien recharger : le domaine ajouté
    serait nommé correctement au parent dans les rapports tout en restant « non classé »
    pour le filtrage, donc AUTORISÉ. Un test verrouille cet accord.

    Renvoie le nombre de services chargés. Lève si le catalogue livré est illisible, et
    dans ce cas rien n'a bougé (cf. services.reload_catalog).
    """
    global OBVIOUS_CATEGORIES
    services.reload_catalog()
    OBVIOUS_CATEGORIES = services.categories_by_domain()
    return len(services.SERVICES)


# ------------------------------------------------------------------ #
#  Cloudflare DoH — niveau 2 de classification                      #
# ------------------------------------------------------------------ #

# Mapping catégories Cloudflare → catégories Protectado
# (utilisé si Cloudflare expose les métadonnées dans leur réponse DoH future)
CLOUDFLARE_MAPPING = {
    "Social Networks":           "social",
    "Adult Themes":              "adult",
    "Video Streaming":           "entertainment",
    "Gaming":                    "entertainment",
    "Educational Institutions":  "education",
    "Productivity":              "work",
    "Extremism":                 "extremism",
    "Weapons":                   "extremism",
    "Hate Speech":               "extremism",
    "Content Delivery Networks": "cdn",
}


# Résolveurs Cloudflare DoH par hostname (requis pour le whitelist nono)
# cloudflare-dns.com     → 1.1.1.1 (standard)
# security.cloudflare-dns.com → 1.1.1.2 (filtre malware/extremism)
# family.cloudflare-dns.com   → 1.1.1.3 (filtre malware + adulte)
_CF_STANDARD = "cloudflare-dns.com"
_CF_SECURITY  = "security.cloudflare-dns.com"
_CF_FAMILY    = "family.cloudflare-dns.com"


def _doh_resolves(resolver_host: str, domain: str, timeout: int = 4) -> tuple[bool | None, list]:
    """
    Interroge un résolveur Cloudflare DoH par hostname.
    Retourne (résout: bool|None, catégories: list).
    None = erreur réseau.
    """
    try:
        url = f"https://{resolver_host}/dns-query?name={urlquote(domain)}&type=A"
        req = urllib.request.Request(url, headers={"Accept": "application/dns-json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())

        # RCODE 0 = NOERROR (résout), 3 = NXDOMAIN (bloqué ou inexistant)
        status = data.get("Status", -1)
        has_answer = bool(data.get("Answer"))

        # Métadonnées de catégorie si Cloudflare les expose à l'avenir
        categories = data.get("categories", [])
        if isinstance(categories, str):
            categories = [categories]

        return (status == 0 and has_answer), categories
    except Exception:
        return None, []


def cloudflare_lookup(domain: str) -> str | None:
    """
    Catégorise un domaine via les résolveurs DoH Cloudflare.

    Stratégie tri-resolver :
      - standard résout + security bloque → extremism/malware
      - standard résout + security résout + family bloque → adult
      - Métadonnées catégorie dans la réponse JSON (mapping direct si disponibles)

    Retourne une catégorie Protectado ou None si indéterminé.
    """
    std_ok, categories = _doh_resolves(_CF_STANDARD, domain)
    if std_ok is None:
        return None  # erreur réseau — ne pas bloquer la classification

    # Utiliser les métadonnées de catégorie si Cloudflare les expose
    for cat in categories:
        mapped = CLOUDFLARE_MAPPING.get(cat)
        if mapped:
            return mapped

    if not std_ok:
        return None  # domaine inexistant — pas de catégorie à attribuer

    # Niveau 2a : filtre malware/extremism
    sec_ok, _ = _doh_resolves(_CF_SECURITY, domain)
    if sec_ok is False:
        return "extremism"

    # Niveau 2b : filtre family/adulte
    fam_ok, _ = _doh_resolves(_CF_FAMILY, domain)
    if fam_ok is False:
        return "adult"

    return None  # domaine légal non catégorisable → escalade Claude


def is_cdn(domain: str) -> bool:
    """Le domaine est-il de l'infrastructure partagée, donc sans intérêt pour le parent ?

    LE CATALOGUE EXPLICITE L'EMPORTE SUR LE MOTIF GÉNÉRIQUE. Les motifs sont testés en
    suffixe, et « cdn.net » attrapait donc fbcdn.net (médias Instagram et Facebook) et
    sc-cdn.net (Snapchat), tous deux pourtant listés dans OBVIOUS_CATEGORIES : leur entrée
    était morte, record_domain les jetait, et des heures de réseaux sociaux ne laissaient
    aucune trace. Un domaine nommément catalogué est du VRAI usage, quelle que soit la
    tête de son nom.
    """
    if domain in OBVIOUS_CATEGORIES:
        return False
    return any(domain.endswith(cdn) for cdn in CDN_PATTERNS)


def record_domain(domain: str, mode: str):
    """
    Enregistre un accès à un domaine dans le catalogue global de classification.
    Appelé par monitor.py à chaque cycle.
    """
    if is_cdn(domain):
        return  # Ignorer les CDN

    # Catégorisation évidente sans IA
    category = OBVIOUS_CATEGORIES.get(domain, "unknown")

    with db.get_db() as conn:
        conn.execute("""
            INSERT INTO domains
                (domain, category, hits_total, first_seen, last_seen, last_mode)
            VALUES (?, ?, 1, ?, ?, ?)
            ON CONFLICT(domain) DO UPDATE SET
                hits_total = hits_total + 1,
                last_seen  = excluded.last_seen,
                last_mode  = excluded.last_mode,
                category   = CASE
                    WHEN category = 'unknown' AND excluded.category != 'unknown'
                    THEN excluded.category
                    ELSE category
                END
        """, (domain, category, datetime.now().isoformat(),
              datetime.now().isoformat(), mode))


def record_domains_bulk(acces: dict):
    """Version groupée de record_domain : {domaine: (nombre d'accès, mode courant)}, en UNE
    transaction (cf. database.increment_usage_bulk). Même règle que l'unitaire : CDN
    ignorés, catégorie évidente posée sans IA, jamais écrasée par « unknown »."""
    lignes = [(d, OBVIOUS_CATEGORIES.get(d, "unknown"), n, mode)
              for d, (n, mode) in acces.items() if not is_cdn(d)]
    if not lignes:
        return
    now = datetime.now().isoformat()
    with db.get_db() as conn:
        conn.executemany("""
            INSERT INTO domains
                (domain, category, hits_total, first_seen, last_seen, last_mode)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(domain) DO UPDATE SET
                hits_total = hits_total + excluded.hits_total,
                last_seen  = excluded.last_seen,
                last_mode  = excluded.last_mode,
                category   = CASE
                    WHEN category = 'unknown' AND excluded.category != 'unknown'
                    THEN excluded.category
                    ELSE category
                END
        """, [(d, cat, n, now, now, mode) for d, cat, n, mode in lignes])


def get_uncategorized(limit: int = 50, cloudflare_only: bool = False) -> list:
    """
    Domaines non encore catégorisés, triés par hits.
    cloudflare_only=True : la file Cloudflare, c'est-à-dire les domaines jamais essayés
                           PLUS ceux dont la tentative est assez vieille pour mériter une
                           seconde chance (cf. CLOUDFLARE_RETRY_*). Sans cette reprise, un
                           domaine que Cloudflare ne connaissait pas un jour n'était plus
                           jamais réinterrogé.
    """
    with db.get_db() as conn:
        if cloudflare_only:
            now = datetime.now()
            vieux = (now - timedelta(days=CLOUDFLARE_RETRY_DAYS)).isoformat()
            vieux_si_actif = (now - timedelta(days=CLOUDFLARE_RETRY_DAYS_BUSY)).isoformat()
            rows = conn.execute("""
                SELECT domain, hits_total, last_mode
                FROM domains
                WHERE category = 'unknown'
                  AND (
                        categorized_by IS NULL
                     OR categorized_by = ''
                     OR (categorized_by = 'cloudflare'
                         AND (categorized_at <= ?
                              OR (hits_total >= ? AND categorized_at <= ?)))
                  )
                ORDER BY hits_total DESC
                LIMIT ?
            """, (vieux, CLOUDFLARE_RETRY_BUSY_HITS, vieux_si_actif, limit)).fetchall()
        else:
            rows = conn.execute("""
                SELECT domain, hits_total, last_mode
                FROM domains
                WHERE category = 'unknown'
                ORDER BY hits_total DESC
                LIMIT ?
            """, (limit,)).fetchall()
    return [dict(r) for r in rows]


def categories_of(domains) -> dict:
    """{domaine: catégorie} pour les seuls domaines demandés, en UNE requête.

    Remplace, pour ce besoin, `get_all_domains()` qui renvoyait la table entière et
    toutes ses colonnes. Elle était appelée une fois par profil à chaque cycle de 60 s
    pour n'en tirer qu'un ensemble de noms : sur un catalogue qui grossit tous les jours,
    c'est le genre de lecture qui finit par coûter plus cher que ce qu'elle décide.

    Un domaine absent du catalogue n'apparaît pas dans le résultat : l'appelant distingue
    ainsi « jamais vu » de « vu et classé unknown ».
    """
    domains = [d for d in dict.fromkeys(domains) if d]
    if not domains:
        return {}
    out: dict = {}
    # Découpage : SQLite plafonne le nombre de paramètres d'une requête (999 par défaut).
    for start in range(0, len(domains), 500):
        lot = domains[start:start + 500]
        marks = ",".join("?" * len(lot))
        with db.get_db() as conn:
            rows = conn.execute(
                f"SELECT domain, category FROM domains WHERE domain IN ({marks})", lot
            ).fetchall()
        out.update({r["domain"]: r["category"] for r in rows})
    return out


def get_domains_by_category(category: str) -> list:
    with db.get_db() as conn:
        rows = conn.execute("""
            SELECT domain, hits_total, last_seen
            FROM domains WHERE category = ?
            ORDER BY hits_total DESC
        """, (category,)).fetchall()
    return [dict(r) for r in rows]


def get_all_domains() -> list:
    """Catalogue complet, avec les exceptions GLOBALES posées par le parent.

    `blocked_homework` et `blocked_free` remplacent les deux colonnes de la table :
    elles viennent maintenant de domain_rules, qui sait aussi AUTORISER un domaine et
    cibler un seul enfant, ce que deux colonnes booléennes ne pouvaient pas exprimer.
    Ici on ne rend que les règles globales, celles qu'affiche la liste des domaines.
    """
    with db.get_db() as conn:
        rows = conn.execute("""
            SELECT domain, category, hits_total,
                   first_seen, last_seen, categorized_by
            FROM domains
            ORDER BY hits_total DESC
        """).fetchall()
        regles = conn.execute(
            "SELECT domain, mode, allow FROM domain_rules WHERE profile=''").fetchall()
    par_domaine = {}
    for r in regles:
        par_domaine.setdefault(r["domain"], {})[r["mode"]] = bool(r["allow"])
    out = []
    for row in rows:
        d = dict(row)
        regle = par_domaine.get(d["domain"], {})
        # None = aucune exception, donc c'est la grille de l'enfant qui décide.
        d["blocked_homework"] = _etat_regle(regle.get(modes.HOMEWORK))
        d["blocked_free"] = _etat_regle(regle.get(modes.FREE))
        out.append(d)
    return out


def _etat_regle(autorise):
    """None si aucune exception, sinon True quand la règle BLOQUE."""
    return None if autorise is None else (not autorise)


def update_domain(domain: str, category: str = None, by: str = "parent"):
    """Recatégorise un domaine à la main.

    Ne gère plus les blocages : ils étaient portés par deux colonnes booléennes, donc
    globales et incapables d'exprimer une autorisation. Ils vivent maintenant dans
    domain_rules (cf. database.set_domain_rule), qui sait autoriser, bloquer, et cibler
    un enfant en particulier.
    """
    with db.get_db() as conn:
        # Créer le domaine s'il n'existe pas encore
        conn.execute("""
            INSERT OR IGNORE INTO domains (domain, category, first_seen, last_seen)
            VALUES (?, 'unknown', ?, ?)
        """, (domain, datetime.now().isoformat(), datetime.now().isoformat()))

        if category is not None:
            conn.execute(
                "UPDATE domains SET category=?, categorized_by=?, categorized_at=? WHERE domain=?",
                (category, by, datetime.now().isoformat(), domain)
            )


def get_active_blacklist(mode, profile: dict = None, profile_key: str = "") -> list:
    """Domaines à bloquer pour CE PROFIL dans CE MODE.

    Trois sources, dans cet ordre :

      1. la GRILLE de l'enfant (access_grid) dit quelles catégories sont bloquées. Elle
         dépend de sa tranche d'âge et des corrections du parent, d'où le besoin du
         profil : la même requête rendait auparavant le même résultat pour un enfant de
         6 ans et un adolescent de 16 ans ;
      2. les EXCEPTIONS du parent (table domain_rules) autorisent ou bloquent des domaines
         nommément, par enfant ou pour toute la maison. Elles l'emportent sur la grille,
         parce qu'un cas particulier est justement ce qu'une catégorie ne sait pas dire ;
      3. les DÉBLOCAGES TEMPORAIRES non échus de cet enfant (table temp_domain_unblocks) :
         sans eux, toute resynchronisation rebloquerait le domaine avant l'échéance ;
      4. les deux catégories bloquées à tout âge, qu'aucune des sources précédentes ne
         peut rouvrir. Une exception qui prétendrait autoriser un domaine classé `adult`
         est IGNORÉE, et c'est le seul endroit où le produit refuse un ordre du parent ;
      5. en passerelle, les résolveurs DNS-over-HTTPS connus (catalog/doh_resolvers.json),
         ajoutés en dernier : ni la grille ni une exception ne les rouvre, puisqu'ils
         permettraient d'échapper à tout le reste.

    Un mode fermé renvoie une liste vide : le blocage total y est porté par un joker DNS
    sur le groupe Pi-hole correspondant, pas par une énumération de domaines.
    """
    canonical = modes.normalize(mode)
    if not modes.is_open(canonical):
        return []

    bloquees = access_grid.blocked_categories(profile or {}, canonical)
    exceptions = db.get_domain_rules(profile_key, canonical)
    # Un déblocage temporaire est la décision la plus récente et la plus ciblée du parent :
    # il s'applique après les exceptions, sauf sur les catégories bloquées à tout âge.
    debloques = db.active_temp_unblocks(profile_key) if profile_key else set()

    with db.get_db() as conn:
        marques = ",".join("?" * len(bloquees)) or "''"
        rows = conn.execute(
            f"SELECT domain, category FROM domains WHERE category IN ({marques})",
            sorted(bloquees)).fetchall()
        par_categorie = {r["domain"] for r in rows}
        # Catégorie des domaines nommés par une exception : nécessaire pour refuser une
        # autorisation portant sur un domaine bloqué à tout âge.
        categories = {}
        if exceptions or debloques:
            noms = list(set(exceptions) | debloques)
            for debut in range(0, len(noms), 500):
                lot = noms[debut:debut + 500]
                m = ",".join("?" * len(lot))
                categories.update({r["domain"]: r["category"] for r in conn.execute(
                    f"SELECT domain, category FROM domains WHERE domain IN ({m})", lot)})

    bloques = set(par_categorie)
    for domaine, autorise in exceptions.items():
        if autorise:
            if categories.get(domaine) in access_grid.ALWAYS_BLOCKED:
                continue          # cf. règle 4 du docstring : pas de dérogation possible
            bloques.discard(domaine)
        else:
            bloques.add(domaine)
    for domaine in debloques:
        if categories.get(domaine) not in access_grid.ALWAYS_BLOCKED:
            bloques.discard(domaine)
    if enforcement_mode() == "gateway":
        bloques |= _doh_domains()
    return sorted(bloques)


# Domaines DoH du catalogue, relus seulement quand le fichier change (catalogue mis à
# jour sans redémarrage, cf. doh_catalog).
_doh_cache: tuple = (None, frozenset())


def _doh_domains() -> frozenset:
    global _doh_cache
    empreinte = doh_catalog.stamp(doh_catalog.PATH)
    if _doh_cache[0] != (doh_catalog.PATH, empreinte):
        _doh_cache = ((doh_catalog.PATH, empreinte), doh_catalog.load(doh_catalog.PATH)["domains"])
    return _doh_cache[1]


# ------------------------------------------------------------------ #
#  Niveau 2 — Classification Cloudflare (batch, 1x/jour)             #
# ------------------------------------------------------------------ #

def classify_now(domain: str) -> str | None:
    """Interroge Cloudflare pour UN domaine et écrit le résultat. Renvoie la catégorie.

    Sert au cas où l'attente n'a pas de sens : un domaine qui vient de bloquer quelqu'un
    doit être identifié tout de suite, pas à la passe des dix minutes. C'est une
    résolution DNS, donc gratuite et de l'ordre de quelques millisecondes.

    L'appelant est responsable de ne PAS appeler ceci dans le fil du cycle de
    surveillance : une résolution lente retarderait tout le cycle (cf. monitor).

    Comme la passe groupée, la tentative est marquée même quand elle ne conclut pas, et
    la date est rafraîchie : le domaine reste réessayable, mais pas à chaque cycle.
    """
    if not domain or is_cdn(domain):
        return None
    category = cloudflare_lookup(domain)
    now = datetime.now().isoformat()
    with db.get_db() as conn:
        if category:
            conn.execute("""
                UPDATE domains
                SET category=?, categorized_by='cloudflare', categorized_at=?
                WHERE domain=? AND category='unknown'
            """, (category, now, domain))
        else:
            conn.execute("""
                UPDATE domains
                SET categorized_by='cloudflare', categorized_at=?
                WHERE domain=? AND category='unknown'
            """, (now, domain))
    return category


def classify_with_cloudflare(limit: int = 200) -> int:
    """
    Passe Cloudflare sur tous les domaines inconnus.
    Appelé juste avant Claude pour réduire les appels IA.
    Retourne le nombre de domaines catégorisés.
    """
    uncategorized = get_uncategorized(limit=limit, cloudflare_only=True)
    if not uncategorized:
        return 0

    classified = 0
    for item in uncategorized:
        domain = item["domain"]
        category = cloudflare_lookup(domain)
        now = datetime.now().isoformat()
        with db.get_db() as conn:
            if category:
                conn.execute("""
                    UPDATE domains
                    SET category=?, categorized_by='cloudflare', categorized_at=?
                    WHERE domain=? AND category='unknown'
                """, (category, now, domain))
                classified += 1
            else:
                # Marquer comme "essayé par Cloudflare" pour ne pas réinterroger à la
                # passe suivante. La catégorie reste 'unknown', Claude le traitera ensuite.
                #
                # La date est RAFRAÎCHIE même si la marque existe déjà : la condition
                # « AND categorized_by IS NULL » qui figurait ici empêchait de mettre à
                # jour categorized_at lors d'une reprise, donc le domaine ressortait de la
                # file à CHAQUE passe pour l'éternité une fois son délai écoulé.
                conn.execute("""
                    UPDATE domains
                    SET categorized_by='cloudflare', categorized_at=?
                    WHERE domain=? AND category='unknown'
                """, (now, domain))

    if classified:
        print(f"[Classifier] {classified} domaines catégorisés par Cloudflare")
    return classified


# ------------------------------------------------------------------ #
#  Niveau 3 — Catégorisation Claude (1x/jour)                        #
# ------------------------------------------------------------------ #

def classify_with_claude(config: dict, batch_size: int = 60) -> dict:
    """
    Demande à Claude de catégoriser les domaines inconnus.
    Boucle jusqu'à épuisement (max 10 passes × batch_size domaines).
    Appelé depuis daily_report.py et depuis /api/report/generate.
    Retourne un dict {domain: category} de l'ensemble des passes.
    """
    # Niveau 2 : Cloudflare d'abord — réduit drastiquement la liste pour Claude
    classify_with_cloudflare(limit=500)

    # Interrupteur de partage : à false, la classification s'arrête aux résolveurs
    # Cloudflare et au catalogue local. Aucun domaine ne part vers OpenRouter.
    import privacy
    if not privacy.share_with_ai(config):
        print("[Classifier] Partage IA désactivé — classification Cloudflare/locale seule.")
        return {}

    # Même contrat que claude_agent._get_client : le modèle est FACULTATIF dans
    # config.json. Le tableau de bord n'y écrit que la clé (/api/ai/key), et exiger
    # « model » faisait échouer toutes les passes de classification avec KeyError.
    from claude_agent import DEFAULT_MODEL
    _openrouter = config.get("openrouter") or {}
    _model = _openrouter.get("model") or DEFAULT_MODEL
    client = OpenAI(
        api_key=_openrouter.get("api_key", ""),
        base_url="https://openrouter.ai/api/v1",
        default_headers={"HTTP-Referer": "http://localhost:8080",
                         "X-Title": "Protectado"}
    )

    all_classifications: dict = {}
    max_passes = 10

    for pass_num in range(1, max_passes + 1):
        uncategorized = get_uncategorized(limit=batch_size, cloudflare_only=False)
        if not uncategorized:
            break

        domains_list = [d["domain"] for d in uncategorized]
        print(f"[Classifier] Passe {pass_num} — {len(domains_list)} domaines → Claude")

        prompt = f"""Catégorise ces domaines DNS pour un contrôle parental.

Catégories possibles :
- education  : sites éducatifs, documentaires, apprentissage
- work       : outils de travail, productivité, recherche scolaire
- entertainment : streaming, gaming, divertissement
- social     : réseaux sociaux, messageries
- adult      : contenu adulte, inapproprié mineur
- extremism  : extrémisme, apologie de la violence, haine, armes
- cdn        : CDN, infrastructure, analytics (trop générique)
- other      : le site est BANAL et ne relève d'aucune des autres (moteur de recherche,
               météo, actualité générale, site institutionnel, commerce ordinaire)
- unknown    : tu ne sais pas ce qu'est ce site

Domaines à catégoriser :
{json.dumps(domains_list, indent=2)}

Réponds UNIQUEMENT en JSON valide, format :
{{"domain.com": "category", "autre.com": "education"}}

N'utilise "other" que pour un site que tu identifies et qui est sans enjeu. Si tu ne sais
pas de quoi il s'agit, réponds "unknown" : c'est une réponse utile, elle sera reposée plus
tard et signalée au parent. Mettre "other" par défaut fait passer une ignorance pour un
verdict, et le site ne sera plus jamais réexaminé.

IMPORTANT sur "extremism" : cette catégorie est bloquée à TOUT ÂGE, dans tous les modes,
et le parent ne peut pas l'autoriser. Une attribution erronée coupe donc un site pour
toute la maison sans aucun recours. N'y recours qu'en cas de certitude, sur un domaine
dont l'objet même est l'extrémisme, l'apologie de la violence, la haine ou le commerce
d'armes. Un site d'actualité qui en parle, un forum de débat ou un jeu vidéo violent
n'en relèvent pas."""

        try:
            response = client.chat.completions.create(
                model=_model,
                max_tokens=1200,
                messages=[
                    {"role": "system", "content": "Tu es un classificateur de domaines DNS. Réponds uniquement en JSON valide."},
                    {"role": "user", "content": prompt}
                ]
            )

            raw = response.choices[0].message.content.strip()
            raw = raw.replace("```json", "").replace("```", "").strip()
            classifications = json.loads(raw)

            now = datetime.now().isoformat()
            retenues = {}
            for domain, category in classifications.items():
                category = str(category or "").strip().lower()
                if category == UNKNOWN_CATEGORY:
                    # Aveu d'ignorance : on n'écrit RIEN. La ligne reste « unknown »,
                    # donc reprise à la prochaine passe et remontable au parent. L'écrire
                    # comme un verdict la sortirait définitivement de la file.
                    continue
                if category not in VALID_CATEGORIES:
                    print(f"[Classifier] catégorie refusée pour {domain} : {category!r}")
                    continue
                with db.get_db() as conn:
                    conn.execute("""
                        UPDATE domains
                        SET category=?, categorized_by='claude', categorized_at=?
                        WHERE domain=? AND category='unknown'
                    """, (category, now, domain))
                retenues[domain] = category

            classifications = retenues
            all_classifications.update(classifications)
            print(f"[Classifier] Passe {pass_num} : {len(classifications)} domaines catégorisés")

            # Une passe qui n'écrit RIEN laisse la file identique : la passe suivante
            # reposerait exactement les mêmes domaines, jusqu'à épuiser les dix passes
            # pour rien. Le cas est devenu possible en séparant l'aveu d'ignorance du
            # verdict « banal » : un lot dont le modèle ignore tout n'écrit plus rien.
            if not classifications:
                print("[Classifier] Passe sans progrès : arrêt, la file est inchangée")
                break

        except Exception as e:
            print(f"[Classifier] Erreur passe {pass_num} : {e}")
            break

    if all_classifications:
        print(f"[Classifier] Total : {len(all_classifications)} domaines catégorisés par Claude")
    else:
        print("[Classifier] Aucun domaine à catégoriser par Claude.")

    return all_classifications
