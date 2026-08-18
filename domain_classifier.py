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
  work         → jamais bloqué en mode work
  entertainment→ bloqué en mode work (youtube, netflix, twitch...)
  social       → bloqué en mode work (instagram, tiktok, discord...)
  adult        → bloqué en modes work ET permissive
  extremism    → toujours bloqué
  cdn          → ignoré (cloudflare, fastly, akamai... — trop générique)
  unknown      → pas encore catégorisé
"""

import json
import urllib.request
from urllib.parse import quote as urlquote
from datetime import datetime, date
from openai import OpenAI

import database as db

# Domaines CDN/infra à ignorer — trop génériques pour être catégorisés
CDN_PATTERNS = [
    "cloudflare.com", "cloudfront.net", "fastly.net", "akamai.net",
    "akamaized.net", "akamaitech.net", "cdn.net", "llnwd.net",
    "edgesuite.net", "edgekey.net", "footprint.net", "level3.net",
    "amazonaws.com", "googleusercontent.com", "gstatic.com",
    "doubleclick.net", "googlesyndication.com", "google-analytics.com",
    "googletagmanager.com", "googleapis.com",
]

# Catégorisation évidente sans IA — économise des appels
OBVIOUS_CATEGORIES = {
    # Education
    "khanacademy.org": "education",
    "wikipedia.org":   "education",
    "wikimedia.org":   "education",
    "wolframalpha.com":"education",
    "duolingo.com":    "education",
    "quizlet.com":     "education",
    "coursera.org":    "education",
    # Work — note : google.com couvre docs/drive/classroom (root domain = 2 parties)
    "google.com":       "work",
    "office.com":       "work",
    "microsoft.com":    "work",
    "notion.so":        "work",
    "sharepoint.com":   "work",
    "onedrive.com":     "work",
    # YouTube + satellites (CDN vidéo, thumbnails)
    "youtube.com":          "entertainment",
    "youtu.be":             "entertainment",
    "ytimg.com":            "entertainment",
    "googlevideo.com":      "entertainment",
    "youtube-nocookie.com": "entertainment",
    # Autres entertainment
    "netflix.com":    "entertainment",
    "nflxvideo.net":  "entertainment",
    "nflxso.net":     "entertainment",
    "twitch.tv":      "entertainment",
    "spotify.com":    "entertainment",
    "scdn.co":        "entertainment",
    "spotifycdn.com": "entertainment",
    # Discord + satellites
    "discord.com":    "social",
    "discordapp.com": "social",
    "discord.gg":     "social",
    "discordcdn.com": "social",
    # Snapchat + satellites
    "snapchat.com":  "social",
    "snap.com":      "social",
    "sc-cdn.net":    "social",
    "snapkit.com":   "social",
    "bitmoji.com":   "social",
    # Signal + satellites
    "signal.org":         "social",
    "whispersystems.org": "social",
    "signal.me":          "social",
    # TikTok + satellites
    "tiktok.com":      "social",
    "tiktokcdn.com":   "social",
    "byteoversea.com": "social",
    "musical.ly":      "social",
    # Instagram / Facebook + satellites
    "instagram.com":   "social",
    "cdninstagram.com":"social",
    "facebook.com":    "social",
    "fbcdn.net":       "social",
    # Twitter/X + satellites
    "twitter.com": "social",
    "x.com":       "social",
    "twimg.com":   "social",
    "t.co":        "social",
    # Autres social
    "reddit.com":  "social",
    # Adult
    "onlyfans.com": "adult",
}


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


def get_uncategorized(limit: int = 50, cloudflare_only: bool = False) -> list:
    """
    Domaines non encore catégorisés, triés par hits.
    cloudflare_only=True : exclut les domaines déjà essayés par Cloudflare
                           (categorized_by='cloudflare') pour éviter les re-requêtes.
    """
    with db.get_db() as conn:
        if cloudflare_only:
            rows = conn.execute("""
                SELECT domain, hits_total, last_mode
                FROM domains
                WHERE category = 'unknown'
                  AND categorized_by IS NULL
                ORDER BY hits_total DESC
                LIMIT ?
            """, (limit,)).fetchall()
        else:
            rows = conn.execute("""
                SELECT domain, hits_total, last_mode
                FROM domains
                WHERE category = 'unknown'
                ORDER BY hits_total DESC
                LIMIT ?
            """, (limit,)).fetchall()
    return [dict(r) for r in rows]


def get_domains_by_category(category: str) -> list:
    with db.get_db() as conn:
        rows = conn.execute("""
            SELECT domain, hits_total, last_seen
            FROM domains WHERE category = ?
            ORDER BY hits_total DESC
        """, (category,)).fetchall()
    return [dict(r) for r in rows]


def get_all_domains() -> list:
    with db.get_db() as conn:
        rows = conn.execute("""
            SELECT domain, category, hits_total,
                   blocked_work, blocked_permissive,
                   first_seen, last_seen, categorized_by
            FROM domains
            ORDER BY hits_total DESC
        """).fetchall()
    return [dict(r) for r in rows]


def update_domain(domain: str, category: str = None,
                  blocked_work: int = None, blocked_permissive: int = None,
                  by: str = "parent"):
    """Mise à jour manuelle par le parent via le chat."""
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
        if blocked_work is not None:
            conn.execute(
                "UPDATE domains SET blocked_work=? WHERE domain=?",
                (blocked_work, domain)
            )
        if blocked_permissive is not None:
            conn.execute(
                "UPDATE domains SET blocked_permissive=? WHERE domain=?",
                (blocked_permissive, domain)
            )


def get_active_blacklist(mode: str) -> list:
    """
    Retourne la liste des domaines à bloquer pour un mode donné.
    Utilisé par monitor.py pour configurer Pi-hole.
    """
    with db.get_db() as conn:
        if mode == "work":
            rows = conn.execute("""
                SELECT domain FROM domains
                WHERE blocked_work = 1
                   OR category IN ('entertainment', 'social', 'adult', 'extremism')
            """).fetchall()
        elif mode == "permissive":
            rows = conn.execute("""
                SELECT domain FROM domains
                WHERE blocked_permissive = 1
                   OR category IN ('adult', 'extremism')
            """).fetchall()
        else:
            return []
    return [r["domain"] for r in rows]


# ------------------------------------------------------------------ #
#  Niveau 2 — Classification Cloudflare (batch, 1x/jour)             #
# ------------------------------------------------------------------ #

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
                # Marquer comme "essayé par Cloudflare" pour ne pas re-interroger demain.
                # La catégorie reste 'unknown' — Claude le traitera ensuite.
                conn.execute("""
                    UPDATE domains
                    SET categorized_by='cloudflare', categorized_at=?
                    WHERE domain=? AND category='unknown' AND categorized_by IS NULL
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
- cdn        : CDN, infrastructure, analytics (trop générique)
- other      : autre (moteurs de recherche, météo, news générales)

Domaines à catégoriser :
{json.dumps(domains_list, indent=2)}

Réponds UNIQUEMENT en JSON valide, format :
{{"domain.com": "category", "autre.com": "education"}}

Ne catégorise que si tu es sûr. Si incertain, mets "other"."""

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
            for domain, category in classifications.items():
                with db.get_db() as conn:
                    conn.execute("""
                        UPDATE domains
                        SET category=?, categorized_by='claude', categorized_at=?
                        WHERE domain=? AND category='unknown'
                    """, (category, now, domain))

            all_classifications.update(classifications)
            print(f"[Classifier] Passe {pass_num} : {len(classifications)} domaines catégorisés")

        except Exception as e:
            print(f"[Classifier] Erreur passe {pass_num} : {e}")
            break

    if all_classifications:
        print(f"[Classifier] Total : {len(all_classifications)} domaines catégorisés par Claude")
    else:
        print("[Classifier] Aucun domaine à catégoriser par Claude.")

    return all_classifications
