# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Arnaud Ortais
# Dual-licensed: AGPL-3.0 (open source) or Commercial License — see LICENSE and LICENSE-COMMERCIAL.
import json
import sqlite3
from datetime import datetime, date, timedelta
from contextlib import contextmanager
from paths import DB_PATH

# Fenêtre de session : si deux requêtes sont séparées de moins de N minutes
# on considère que c'est la même session de visionnage
SESSION_GAP_MINUTES = 10


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS acknowledged_reports (
                event_id    INTEGER PRIMARY KEY,
                acked_at    TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   TEXT    NOT NULL,
                profile     TEXT    NOT NULL,
                type        TEXT    NOT NULL,
                domain      TEXT    DEFAULT '',
                -- message : phrase française littérale. CONSERVÉE, pour deux raisons :
                --   1. les événements déjà en base n'ont pas de clé i18n ;
                --   2. les rapports IA relisent ces événements en texte libre.
                message     TEXT    DEFAULT '',
                -- message_key + params : la même information sous forme traduisible.
                -- On ne traduit PAS côté serveur : la langue du boîtier peut changer
                -- après coup, et un journal figé dans l'ancienne langue serait pire.
                message_key TEXT    DEFAULT '',
                params      TEXT    DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS daily_usage (
                date    TEXT NOT NULL,
                profile TEXT NOT NULL,
                domain  TEXT NOT NULL,
                queries INTEGER DEFAULT 0,
                PRIMARY KEY (date, profile, domain)
            );

            -- Historique horodaté des requêtes DNS par domaine/profil
            -- Utilisé pour estimer le temps de session
            CREATE TABLE IF NOT EXISTS dns_timeline (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT    NOT NULL,
                profile   TEXT    NOT NULL,
                domain    TEXT    NOT NULL
            );

            -- Index pour accélérer les requêtes par profil/domaine/date
            CREATE INDEX IF NOT EXISTS idx_dns_timeline_lookup
                ON dns_timeline(profile, domain, timestamp);

            CREATE TABLE IF NOT EXISTS schedule_overrides (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                profile     TEXT NOT NULL,
                date        TEXT NOT NULL,
                mode        TEXT NOT NULL,
                reason      TEXT DEFAULT '',
                created_at  TEXT NOT NULL,
                UNIQUE(profile, date)
            );

            CREATE TABLE IF NOT EXISTS device_overrides (
                ip          TEXT PRIMARY KEY,
                expires_at  TEXT NOT NULL,
                taken_by    TEXT DEFAULT 'parent',
                created_at  TEXT NOT NULL
            );

            -- État temporaire accordé par le parent (interface ou chat).
            -- PERSISTÉ : ces états ne vivaient qu'en mémoire et dans des threading.Timer,
            -- donc un simple redémarrage du service — l'auto-update nocturne en fait un
            -- chaque nuit — effaçait une dérogation accordée le soir sans jamais la
            -- restaurer. La base fait désormais autorité ; les timers ne sont plus qu'un
            -- raccourci pour l'immédiateté, le cycle du monitor rattrape tout.
            CREATE TABLE IF NOT EXISTS temp_overrides (
                profile     TEXT PRIMARY KEY,
                mode        TEXT NOT NULL,
                expires_at  TEXT NOT NULL,
                created_at  TEXT NOT NULL
            );

            -- Domaines débloqués temporairement (outil « allow_domain_temporarily »).
            -- Seule l'échéance importe : à l'expiration on resynchronise les blacklists.
            -- Une ligne par (domaine, enfant) : deux enfants peuvent obtenir le même
            -- domaine, chacun avec sa propre échéance.
            CREATE TABLE IF NOT EXISTS temp_domain_unblocks (
                domain      TEXT NOT NULL,
                profile     TEXT NOT NULL DEFAULT '',
                expires_at  TEXT NOT NULL,
                created_at  TEXT NOT NULL,
                PRIMARY KEY (domain, profile)
            );

            -- Rallonges de plage horaire accordées dans la journée (« encore 20 min »).
            -- day + slot_start désignent LA plage prolongée : une rallonge ne vaut
            -- que pour elle, jamais pour les autres plages de la journée.
            CREATE TABLE IF NOT EXISTS slot_extensions (
                profile     TEXT PRIMARY KEY,
                minutes     INTEGER NOT NULL,
                day         TEXT NOT NULL,
                updated_at  TEXT NOT NULL,
                slot_start  TEXT NOT NULL DEFAULT ''
            );

            -- Appareils déjà vus, par enfant. En passerelle, la liste d'appareils d'un
            -- profil n'est que l'état du moment : sans cette mémoire, l'écran ne pouvait
            -- pas distinguer « jamais connecté » de « connecté hier ».
            CREATE TABLE IF NOT EXISTS seen_devices (
                profile     TEXT NOT NULL,
                mac         TEXT NOT NULL,
                name        TEXT NOT NULL DEFAULT '',
                last_ip     TEXT NOT NULL DEFAULT '',
                first_seen  TEXT NOT NULL,
                last_seen   TEXT NOT NULL,
                PRIMARY KEY (profile, mac)
            );

            -- Sessions de tunnel probable (VPN) : début, dernière activité, fin.
            -- Une ligne sans ended_at est une session EN COURS.
            CREATE TABLE IF NOT EXISTS vpn_sessions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                profile     TEXT NOT NULL,
                ip          TEXT NOT NULL,
                dst         TEXT NOT NULL,
                started_at  TEXT NOT NULL,
                last_seen   TEXT NOT NULL,
                ended_at    TEXT,
                -- Le parent l'a vue (session terminée seulement) : elle quitte l'écran.
                acknowledged_at TEXT,
                bytes       INTEGER NOT NULL DEFAULT 0
            );

            -- Volume Internet par enfant et par jour, en octets (posture passerelle :
            -- trafic du Wi-Fi enfants vers l'extérieur, relevé par conntrack).
            CREATE TABLE IF NOT EXISTS daily_traffic (
                date        TEXT NOT NULL,
                profile     TEXT NOT NULL,
                bytes       INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (date, profile)
            );

            -- Métadonnées applicatives (date de dernière purge, etc.)
            CREATE TABLE IF NOT EXISTS app_meta (
                key   TEXT PRIMARY KEY,
                value TEXT DEFAULT ''
            );
        """)
        # Migration idempotente : ajoute message_key/params aux bases antérieures à
        # l'internationalisation du journal. ALTER TABLE ADD COLUMN n'est pas
        # « IF NOT EXISTS » en SQLite : on inspecte le schéma avant.
        _cols = {r[1] for r in conn.execute("PRAGMA table_info(events)")}
        for _col in ("message_key", "params"):
            if _col not in _cols:
                conn.execute(f"ALTER TABLE events ADD COLUMN {_col} TEXT DEFAULT ''")
        # Clé primaire (domain, profile) des déblocages temporaires. SQLite ne sait pas
        # modifier une clé primaire : on recrée la table en copiant les lignes.
        _pk = [r[1] for r in sorted(conn.execute("PRAGMA table_info(temp_domain_unblocks)"),
                                    key=lambda r: r[5]) if r[5]]
        if _pk == ["domain"]:
            conn.executescript("""
                ALTER TABLE temp_domain_unblocks RENAME TO temp_domain_unblocks_old;
                CREATE TABLE temp_domain_unblocks (
                    domain      TEXT NOT NULL,
                    profile     TEXT NOT NULL DEFAULT '',
                    expires_at  TEXT NOT NULL,
                    created_at  TEXT NOT NULL,
                    PRIMARY KEY (domain, profile)
                );
                INSERT INTO temp_domain_unblocks (domain, profile, expires_at, created_at)
                    SELECT domain, profile, expires_at, created_at FROM temp_domain_unblocks_old;
                DROP TABLE temp_domain_unblocks_old;
            """)
        # vpn_sessions est arrivée sans son volume sur les boîtiers déjà mis à jour.
        if "bytes" not in {r[1] for r in conn.execute("PRAGMA table_info(vpn_sessions)")}:
            conn.execute("ALTER TABLE vpn_sessions ADD COLUMN bytes INTEGER NOT NULL DEFAULT 0")
        # Même principe pour la plage visée par une rallonge. Une ligne antérieure garde
        # slot_start vide : elle ne désigne aucune plage, donc elle ne s'applique pas.
        if "slot_start" not in {r[1] for r in conn.execute("PRAGMA table_info(slot_extensions)")}:
            conn.execute("ALTER TABLE slot_extensions ADD COLUMN slot_start TEXT NOT NULL DEFAULT ''")
        conn.commit()
    init_domains_table()


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def log_event(profile: str, event_type: str, domain: str = "", message: str = "",
              message_key: str = "", params: dict | None = None):
    """Journalise un événement.

    message      — phrase française littérale (logs, rapports IA, événements anciens) ;
    message_key  — clé i18n (« event.mode_change ») rendue par le client dans SA langue ;
    params       — variables de la clé, sérialisées en JSON.

    Le rendu est fait côté CLIENT et non ici : le parent peut changer la langue du
    boîtier à tout moment, et un journal traduit à l'écriture resterait figé dans
    l'ancienne langue. Les deux formes coexistent — message reste le repli quand
    message_key est vide (événements écrits avant cette version).
    """
    with get_db() as conn:
        conn.execute(
            "INSERT INTO events (timestamp, profile, type, domain, message, message_key, params) "
            "VALUES (?,?,?,?,?,?,?)",
            (datetime.now().isoformat(), profile, event_type, domain, message,
             message_key, json.dumps(params, ensure_ascii=False) if params else "")
        )


def increment_usage(profile: str, domain: str, count: int = 1):
    today = date.today().isoformat()
    now = datetime.now().isoformat()
    with get_db() as conn:
        # Compteur de requêtes (existant)
        conn.execute("""
            INSERT INTO daily_usage (date, profile, domain, queries)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(date, profile, domain)
            DO UPDATE SET queries = queries + excluded.queries
        """, (today, profile, domain, count))

        # Timeline horodatée pour estimation du temps
        conn.execute(
            "INSERT INTO dns_timeline (timestamp, profile, domain) VALUES (?,?,?)",
            (now, profile, domain)
        )


def increment_usage_bulk(compteurs: dict):
    """Version groupée d'increment_usage pour un cycle du moniteur : {(profil, domaine):
    nombre de requêtes}, écrit en UNE transaction. L'unitaire ouvrait une connexion et
    validait deux écritures par requête DNS (2 000 requêtes : 32 s mesurées sur poste de
    dev). Une seule marque dns_timeline par (profil, domaine) et par cycle : les requêtes
    d'un même cycle tombent dans la même minute, l'estimation des sessions n'y perd rien.
    """
    if not compteurs:
        return
    today = date.today().isoformat()
    now = datetime.now().isoformat()
    with get_db() as conn:
        conn.executemany("""
            INSERT INTO daily_usage (date, profile, domain, queries)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(date, profile, domain)
            DO UPDATE SET queries = queries + excluded.queries
        """, [(today, p, d, n) for (p, d), n in compteurs.items()])
        conn.executemany(
            "INSERT INTO dns_timeline (timestamp, profile, domain) VALUES (?,?,?)",
            [(now, p, d) for (p, d) in compteurs])


def estimate_session_minutes(profile: str, domain: str, for_date: str = None) -> int:
    """
    Estime le temps passé sur un domaine en regroupant les requêtes DNS
    en sessions continues (gap < SESSION_GAP_MINUTES).

    Algo :
    - Trier les requêtes par timestamp
    - Si l'écart entre deux requêtes < SESSION_GAP_MINUTES → même session
    - Durée session = (dernière requête - première requête) + SESSION_GAP_MINUTES
    - Total = somme des durées de sessions
    """
    if for_date is None:
        for_date = date.today().isoformat()

    start = f"{for_date}T00:00:00"
    end   = f"{for_date}T23:59:59"

    # `domain` accepte UN domaine ou PLUSIEURS, et dans le second cas les horodatages sont
    # mis en COMMUN avant le découpage en sessions. C'est indispensable pour un service :
    # additionner les minutes de youtube.com et de googlevideo.com compterait deux fois la
    # même soirée, puisque les deux domaines sont sollicités en parallèle. Une durée ne
    # s'additionne pas, elle se recolle.
    domaines = [domain] if isinstance(domain, str) else list(domain or [])
    if not domaines:
        return 0
    marques = ",".join("?" * len(domaines))
    with get_db() as conn:
        rows = conn.execute(f"""
            SELECT timestamp FROM dns_timeline
            WHERE profile=? AND domain IN ({marques})
              AND timestamp BETWEEN ? AND ?
            ORDER BY timestamp ASC
        """, (profile, *domaines, start, end)).fetchall()

    if not rows:
        return 0

    timestamps = [datetime.fromisoformat(r["timestamp"]) for r in rows]
    gap = timedelta(minutes=SESSION_GAP_MINUTES)

    total_minutes = 0
    session_start = timestamps[0]
    session_last  = timestamps[0]

    for ts in timestamps[1:]:
        if ts - session_last < gap:
            # Même session
            session_last = ts
        else:
            # Nouvelle session — comptabiliser la précédente
            duration = (session_last - session_start) + gap
            total_minutes += int(duration.total_seconds() / 60)
            session_start = ts
            session_last  = ts

    # Dernière session
    duration = (session_last - session_start) + gap
    total_minutes += int(duration.total_seconds() / 60)

    return total_minutes


def get_time_spent_today(profile: str, for_date: str = None) -> dict:
    """Temps estimé en minutes, PAR SERVICE, pour aujourd'hui.

    Regroupé par service et non par domaine (cf. services.py), pour deux raisons.

    La première est de lisibilité : un parent demande « combien de temps sur YouTube », et
    recevait quatre lignes (youtube.com, ytimg.com, googlevideo.com, youtu.be) dont
    aucune ne répondait à sa question.

    La seconde est une CORRECTION. Les domaines d'un service sont sollicités en parallèle
    pendant la même séance : additionner leurs durées comptait donc la même soirée
    plusieurs fois, et le total affiché au parent pouvait dépasser la journée. Les
    horodatages sont désormais mis en commun avant le découpage en sessions.

    Un domaine sans service garde son propre nom : on ne perd rien.
    """
    import services

    for_date = for_date or date.today().isoformat()
    with get_db() as conn:
        rows = conn.execute("""
            SELECT DISTINCT domain FROM dns_timeline
            WHERE profile=? AND timestamp LIKE ?
        """, (profile, f"{for_date}%")).fetchall()

    # Regrouper les domaines VUS par service, sans convoquer ceux qu'on n'a pas vus :
    # interroger tous les domaines d'un service alourdirait la requête pour rien.
    par_libelle: dict = {}
    for row in rows:
        par_libelle.setdefault(services.label_for_domain(row["domain"]), []).append(
            row["domain"])

    return {libelle: estimate_session_minutes(profile, domaines, for_date)
            for libelle, domaines in par_libelle.items()}


def get_last_dns(profile: str) -> datetime | None:
    """Retourne le timestamp de la dernière requête DNS enregistrée pour un profil."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT timestamp FROM dns_timeline WHERE profile=? ORDER BY timestamp DESC LIMIT 1",
            (profile,)
        ).fetchone()
    return datetime.fromisoformat(row["timestamp"]) if row else None


def get_usage_today(profile: str) -> dict:
    today = date.today().isoformat()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT domain, queries FROM daily_usage WHERE date=? AND profile=?",
            (today, profile)
        ).fetchall()
    return {row["domain"]: row["queries"] for row in rows}


def set_domain_rule(profile: str, domain: str, mode: str, allow: bool):
    """Pose une exception : ce domaine est autorisé (allow) ou bloqué dans ce mode.

    `profile` vide vaut pour tous les enfants. Idempotent : reposer la même règle la
    remplace, ce qui permet au parent de changer d'avis sans accumuler des lignes
    contradictoires.
    """
    with get_db() as conn:
        conn.execute("""
            INSERT INTO domain_rules (profile, domain, mode, allow, created_at)
            VALUES (?,?,?,?,?)
            ON CONFLICT(profile, domain, mode) DO UPDATE SET
                allow=excluded.allow, created_at=excluded.created_at
        """, (profile or "", domain, mode, 1 if allow else 0,
              datetime.now().isoformat()))


def clear_domain_rule(profile: str, domain: str, mode: str = ""):
    """Retire une exception, et donc rend ce domaine à la décision de la grille.

    Sans `mode`, retire l'exception dans tous les modes pour ce couple profil/domaine.
    """
    with get_db() as conn:
        if mode:
            conn.execute("DELETE FROM domain_rules WHERE profile=? AND domain=? AND mode=?",
                         (profile or "", domain, mode))
        else:
            conn.execute("DELETE FROM domain_rules WHERE profile=? AND domain=?",
                         (profile or "", domain))


def get_domain_rules(profile: str, mode: str) -> dict:
    """{domaine: autorisé} applicable à ce profil dans ce mode.

    Les règles globales (profil vide) sont posées d'abord, puis ÉCRASÉES par les règles
    nominatives : une décision prise pour un enfant en particulier doit l'emporter sur
    une décision prise pour toute la maison, sinon la grille par enfant ne veut rien dire.
    """
    with get_db() as conn:
        rows = conn.execute("""
            SELECT profile, domain, allow FROM domain_rules
            WHERE mode=? AND (profile='' OR profile=?)
            ORDER BY CASE WHEN profile='' THEN 0 ELSE 1 END
        """, (mode, profile or "")).fetchall()
    return {r["domain"]: bool(r["allow"]) for r in rows}


def list_domain_rules(profile: str) -> list:
    """Les exceptions posées pour CET enfant (sans celles de toute la maison)."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT domain, mode, allow FROM domain_rules WHERE profile=? "
            "ORDER BY domain, mode", (profile,)).fetchall()
    return [{"domain": r["domain"], "mode": r["mode"], "allow": bool(r["allow"])} for r in rows]


def all_domain_rules() -> list:
    """Toutes les exceptions, pour l'affichage et le diagnostic."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT profile, domain, mode, allow, created_at FROM domain_rules "
            "ORDER BY domain, profile, mode").fetchall()
    return [dict(r) for r in rows]


def get_usage_history(days: int = 7, before_today: bool = True) -> dict:
    """Historique d'usage : {(profil, domaine): {date: compteur}}.

    UNE SEULE REQUÊTE pour tous les profils, appelée une fois par cycle. La version
    naïve de ce besoin lisait la table `domains` entière, toutes colonnes, une fois par
    profil et par cycle de 60 s.

    `before_today` exclut la journée en cours : la référence d'un comportement normal ne
    doit pas inclure le pic qu'on est en train de juger.
    """
    today = date.today().isoformat()
    start = (date.today() - timedelta(days=days)).isoformat()
    with get_db() as conn:
        rows = conn.execute("""
            SELECT profile, domain, date, queries FROM daily_usage
            WHERE date >= ? AND date < ?
        """ if before_today else """
            SELECT profile, domain, date, queries FROM daily_usage
            WHERE date >= ? AND date <= ?
        """, (start, today)).fetchall()
    # Indexé par DATE et non par liste anonyme : pour comparer l'habitude d'un SERVICE,
    # il faut additionner ses domaines JOUR PAR JOUR. Une liste de compteurs sans leur
    # date ne permet pas de les aligner entre domaines, et additionner deux listes non
    # alignées donnerait une moyenne qui ne correspond à aucune journée réelle.
    out: dict = {}
    for row in rows:
        out.setdefault((row["profile"], row["domain"]), {})[row["date"]] = row["queries"]
    return out


def get_recent_events(limit: int = 50) -> list:
    _REPORT_TYPES = ('daily_report', 'weekly_report', 'monthly_report')
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM events WHERE type NOT IN (?,?,?) ORDER BY timestamp DESC LIMIT ?",
            (*_REPORT_TYPES, limit)
        ).fetchall()
    return [dict(r) for r in rows]


# Les événements qui veulent dire « le boîtier ne protège PAS quelqu'un », par
# opposition à tous les autres, qui disent qu'il a fait son travail.
#
# La colonne `type` ne peut pas servir de critère : elle vaut « warning » pour ces deux
# alertes, mais aussi pour chaque tentative d'accès bloquée. Or un blocage n'est pas une
# alerte, c'est le produit qui fonctionne, et il en arrive des dizaines par jour. Les
# mélanger remplirait l'écran d'un bruit permanent, et une carte toujours pleine ne se
# regarde plus. D'où une liste NOMMÉE, courte, et qui doit rester courte.
#
# Elles ont en commun de signaler un appareil sur lequel les règles ne s'appliquent pas :
# il contourne le DNS du boîtier, ou il s'est associé avec une clé qui n'appartient à
# aucun profil. Une tentative de contournement REFUSÉE n'en est pas une : le boîtier a
# fait son travail, et certains navigateurs le tentent d'eux-mêmes. Le tunnel (VPN) est
# montré dans la même carte, mais par ses SESSIONS (cf. get_vpn_sessions) : « en cours »
# et « de telle heure à telle heure » ne se lisent pas dans un événement ponctuel.
ALERT_KEYS = ("event.dns_bypass_suspected", "event.station_unknown_key")

# Le journal des réglages ne montre que les DÉCISIONS humaines (le parent, par l'écran ou
# par l'assistant). Les tentatives bloquées, souvent des vérifications automatiques des
# appareils, et la routine (changements de plage, échéances) le rendaient illisible ; le
# détail d'une journée reste dans l'Historique de l'enfant. Liste NOMMÉE, comme
# ALERT_KEYS : une décision nouvelle doit y être ajoutée explicitement.
DECISION_KEYS = (
    "event.profile_created", "event.profile_updated", "event.profile_deleted",
    "event.wifi_key_changed", "event.history_purged", "event.privacy_access",
    "event.schedule_override", "event.schedule_override_reason",
    "event.schedule_override_removed",
    "event.temp_override_started", "event.temp_override_until",
    "event.temp_override_cancelled",
    "event.access_extended", "event.extension_cancelled", "event.manual_block",
    "event.domain_rule_changed", "event.domain_allowed_temp",
    "event.domain_unblock_cancelled",
    "event.adult_mode_started", "event.adult_mode_cancelled",
    "event.vpn_acknowledged", "event.secours_exited_new_box",
)


def get_decisions(limit: int = 30) -> list:
    """Les décisions les plus récentes (cf. DECISION_KEYS)."""
    marques = ",".join("?" * len(DECISION_KEYS))
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT * FROM events WHERE message_key IN ({marques}) "
            "ORDER BY timestamp DESC, id DESC LIMIT ?", (*DECISION_KEYS, limit)).fetchall()
    return [dict(r) for r in rows]


# Ce que « tout le journal » ne montre PAS : la navigation de l'enfant. Une tentative
# d'accès bloquée porte le site et l'heure, un rapport résume la journée. Les montrer ici
# en ferait un accès sans mot de passe à ce que le détail d'une journée protège (mot de
# passe, accès journalisé, niveau de vie privée selon l'âge). Une ligne sans clé est un
# texte libre d'avant les clés de traduction : on ne sait pas ce qu'elle contient.
JOURNAL_PRIVATE_KEYS = ("event.blocked_attempt", "event.blocked_attempt_multi",
                        "event.daily_report", "event.weekly_review", "event.monthly_review")


def is_journal_event(event: dict) -> bool:
    cle = event.get("message_key") or ""
    return bool(cle) and cle not in JOURNAL_PRIVATE_KEYS


def get_journal(limit: int = 30) -> list:
    """Tout le journal, hors navigation (cf. JOURNAL_PRIVATE_KEYS), le plus récent
    d'abord. `decision` marque les décisions du parent (cf. DECISION_KEYS)."""
    marques = ",".join("?" * len(JOURNAL_PRIVATE_KEYS))
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT * FROM events WHERE message_key != '' AND message_key NOT IN ({marques}) "
            "ORDER BY timestamp DESC, id DESC LIMIT ?", (*JOURNAL_PRIVATE_KEYS, limit)).fetchall()
    return [{**dict(r), "decision": r["message_key"] in DECISION_KEYS} for r in rows]


# Fenêtre de l'écran d'alertes. Elle remplace un mécanisme d'acquittement : ces alertes
# sont réémises tant que la cause persiste (une par appareil et par jour pour le
# contournement, une par clé toutes les six heures pour l'autre), donc une alerte dont la
# cause a disparu sort d'elle-même de la fenêtre. Un bouton « vu » aurait au contraire
# permis de masquer une cause toujours active.
ALERT_WINDOW_HOURS = 48


def get_alerts(hours: int = ALERT_WINDOW_HOURS, limit: int = 10) -> list[dict]:
    """Alertes récentes, REGROUPÉES, la plus récente d'abord.

    Le regroupement par (clé, appareil) est ce qui rend l'écran lisible : un appareil qui
    contourne le DNS depuis une semaine a écrit sept lignes, et le parent n'a qu'un seul
    problème à traiter. `count` dit combien de fois l'alerte est revenue, ce qui est
    justement l'information qui manque quand on n'affiche que la dernière.
    """
    depuis = (datetime.now() - timedelta(hours=max(1, hours))).isoformat()
    with get_db() as conn:
        rows = conn.execute(f"""
            SELECT MAX(timestamp) AS timestamp, profile, type, domain,
                   message, message_key, params, COUNT(*) AS count
            FROM events
            WHERE message_key IN ({','.join('?' * len(ALERT_KEYS))})
              AND timestamp >= ?
            GROUP BY message_key, domain, profile
            ORDER BY timestamp DESC
            LIMIT ?
        """, (*ALERT_KEYS, depuis, limit)).fetchall()
    return [dict(r) for r in rows]


# Une session de tunnel terminée reste affichée tant de temps après sa fin.
VPN_SHOWN_HOURS = 48


def open_vpn_session(profile: str, ip: str, dst: str, started_at: str, last_seen: str,
                     octets: int = 0) -> int:
    with get_db() as conn:
        return conn.execute(
            "INSERT INTO vpn_sessions (profile, ip, dst, started_at, last_seen, bytes) "
            "VALUES (?,?,?,?,?,?)",
            (profile, ip, dst, started_at, last_seen, int(octets))).lastrowid


def touch_vpn_session(session_id: int, last_seen: str, octets: int = 0) -> None:
    with get_db() as conn:
        conn.execute("UPDATE vpn_sessions SET last_seen=?, bytes=bytes+? WHERE id=?",
                     (last_seen, int(octets), session_id))


def get_vpn_sessions_for_date(profile: str, date: str) -> list[dict]:
    """Sessions de tunnel de cet enfant qui ont touché ce jour-là."""
    with get_db() as conn:
        rows = conn.execute("""
            SELECT started_at, last_seen, ended_at, bytes FROM vpn_sessions
            WHERE profile=? AND substr(started_at,1,10) <= ? AND substr(last_seen,1,10) >= ?
            ORDER BY started_at""", (profile, date, date)).fetchall()
    return [dict(r) for r in rows]


def add_daily_traffic(volumes: dict) -> None:
    """{(jour, enfant): octets} ajoutés au volume du jour, en une transaction."""
    if not volumes:
        return
    with get_db() as conn:
        conn.executemany("""
            INSERT INTO daily_traffic (date, profile, bytes) VALUES (?,?,?)
            ON CONFLICT(date, profile) DO UPDATE SET bytes = bytes + excluded.bytes
        """, [(d, p, int(n)) for (d, p), n in volumes.items()])


def get_daily_traffic(profile: str, date: str) -> int | None:
    """Octets du jour, ou None si rien n'a été relevé (mode DNS seul, boîtier éteint) :
    inconnu ne se lit pas comme zéro."""
    with get_db() as conn:
        r = conn.execute("SELECT bytes FROM daily_traffic WHERE profile=? AND date=?",
                         (profile, date)).fetchone()
    return r["bytes"] if r else None


def get_meta(key: str, default: str = "") -> str:
    with get_db() as conn:
        r = conn.execute("SELECT value FROM app_meta WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def set_meta(key: str, value: str) -> None:
    with get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO app_meta (key, value) VALUES (?, ?)", (key, value))


def close_vpn_session(session_id: int, ended_at: str) -> None:
    with get_db() as conn:
        conn.execute("UPDATE vpn_sessions SET ended_at=? WHERE id=?", (ended_at, session_id))


def get_open_vpn_sessions() -> list[dict]:
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM vpn_sessions WHERE ended_at IS NULL").fetchall()
    return [dict(r) for r in rows]


def get_vpn_sessions(hours: int = VPN_SHOWN_HOURS) -> list[dict]:
    """Sessions de tunnel à montrer : celles en cours, puis celles terminées depuis moins
    de `hours` heures, la plus récente d'abord. `active` dit laquelle est en cours."""
    depuis = (datetime.now() - timedelta(hours=hours)).isoformat()
    with get_db() as conn:
        rows = conn.execute("""
            SELECT * FROM vpn_sessions
            WHERE ended_at IS NULL OR (ended_at >= ? AND acknowledged_at IS NULL)
            ORDER BY (ended_at IS NULL) DESC, COALESCE(ended_at, last_seen) DESC
        """, (depuis,)).fetchall()
    return [{**dict(r), "active": r["ended_at"] is None} for r in rows]


def get_vpn_session(session_id: int) -> dict | None:
    with get_db() as conn:
        r = conn.execute("SELECT * FROM vpn_sessions WHERE id=?", (session_id,)).fetchone()
    return dict(r) if r else None


def acknowledge_vpn_session(session_id: int) -> None:
    """Le parent a vu une session TERMINÉE : elle quitte l'écran avant ses 48 h."""
    with get_db() as conn:
        conn.execute("UPDATE vpn_sessions SET acknowledged_at=? "
                     "WHERE id=? AND ended_at IS NOT NULL",
                     (datetime.now().isoformat(timespec="seconds"), session_id))


def get_pending_reports() -> list[dict]:
    """Rapports non encore acquittés (daily, weekly, monthly)."""
    with get_db() as conn:
        rows = conn.execute("""
            SELECT e.id, e.timestamp, e.type, e.message, e.message_key, e.params
            FROM events e
            LEFT JOIN acknowledged_reports a ON a.event_id = e.id
            WHERE e.profile = 'global'
              AND e.type IN ('daily_report', 'weekly_report', 'monthly_report')
              AND a.event_id IS NULL
            ORDER BY e.timestamp DESC
        """).fetchall()
    return [dict(r) for r in rows]


def acknowledge_report(event_id: int):
    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO acknowledged_reports (event_id, acked_at) VALUES (?,?)",
            (event_id, datetime.now().isoformat())
        )


def get_events_for_profile(profile: str, limit: int = 20) -> list:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM events WHERE profile=? ORDER BY timestamp DESC LIMIT ?",
            (profile, limit)
        ).fetchall()
    return [dict(r) for r in rows]


def purge_old_timeline(days: int = 7):
    """Nettoie les entrées de timeline de plus de N jours."""
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    with get_db() as conn:
        conn.execute("DELETE FROM dns_timeline WHERE timestamp < ?", (cutoff,))


# Rétention minimale au-dessous de laquelle les rapports hebdomadaires et mensuels
# perdent leur matière : le mensuel relit les revues hebdomadaires des 30 derniers
# jours, l'hebdomadaire relit les rapports quotidiens des 7 derniers.
RETENTION_WEEKLY_MIN  = 8
RETENTION_MONTHLY_MIN = 31


def purge_old_data(retention_days: int = 90) -> dict:
    """Applique la politique de rétention à TOUTES les tables d'historique.

    Avant cette fonction, seule dns_timeline était purgée : daily_usage, events et
    domains grossissaient indéfiniment. Un enfant suivi de 12 à 17 ans laissait cinq ans
    de profil comportemental agrégé, enrichi d'analyses produites par un modèle — donc
    faillibles — sur une personne réelle.

    retention_days = 0 ⇒ conservation illimitée (choix explicite du parent, signalé
    comme tel dans l'interface). Retourne le détail des lignes supprimées.

    Les rapports IA sont soumis à la MÊME rétention que le reste : ce sont les données
    les plus sensibles du produit, il n'y a aucune raison de les conserver plus
    longtemps. En revanche, les domaines catégorisés à la main par le parent
    (categorized_by='parent') ne sont jamais purgés : c'est de la configuration, pas de
    l'historique — les supprimer reviendrait à défaire son travail de réglage.
    """
    result = {"retention_days": retention_days, "purged_at": datetime.now().isoformat()}
    if retention_days <= 0:
        result["skipped"] = "illimité"
        return result

    now = datetime.now()
    cutoff_ts   = (now - timedelta(days=retention_days)).isoformat()
    cutoff_date = (now - timedelta(days=retention_days)).date().isoformat()

    with get_db() as conn:
        cur = conn.execute("DELETE FROM dns_timeline WHERE timestamp < ?", (cutoff_ts,))
        result["dns_timeline"] = cur.rowcount
        cur = conn.execute("DELETE FROM daily_usage WHERE date < ?", (cutoff_date,))
        result["daily_usage"] = cur.rowcount
        cur = conn.execute("DELETE FROM events WHERE timestamp < ?", (cutoff_ts,))
        result["events"] = cur.rowcount
        cur = conn.execute("DELETE FROM vpn_sessions WHERE ended_at < ?", (cutoff_ts,))
        result["vpn_sessions"] = cur.rowcount
        cur = conn.execute("DELETE FROM daily_traffic WHERE date < ?", (cutoff_date,))
        result["daily_traffic"] = cur.rowcount
        # Les accusés de réception orphelins n'ont plus d'objet une fois l'événement parti.
        conn.execute("""
            DELETE FROM acknowledged_reports
            WHERE event_id NOT IN (SELECT id FROM events)
        """)
        # Dérogations de planning échues — ce sont des décisions datées, pas des règles.
        conn.execute("DELETE FROM schedule_overrides WHERE date < ?", (cutoff_date,))
        # Catalogue de domaines : seulement ceux jamais revus par le parent ET sans
        # consultation récente. Le `last_seen` est absent des bases anciennes : on ne
        # purge alors que sur l'absence de hits.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(domains)")}
        if "categorized_by" in cols and "last_seen" in cols:
            cur = conn.execute("""
                DELETE FROM domains
                WHERE COALESCE(categorized_by, '') != 'parent'
                  AND COALESCE(last_seen, '') < ?
            """, (cutoff_ts,))
            result["domains"] = cur.rowcount
        else:
            result["domains"] = 0
        conn.execute("INSERT OR REPLACE INTO app_meta (key, value) VALUES ('last_purge', ?)",
                     (result["purged_at"],))
    return result


def last_purge_at() -> str:
    """Horodatage de la dernière purge — affiché au parent, et à l'enfant sur sa page."""
    try:
        with get_db() as conn:
            row = conn.execute("SELECT value FROM app_meta WHERE key='last_purge'").fetchone()
        return row["value"] if row else ""
    except Exception:
        return ""


def clear_profile_state(profile: str) -> dict:
    """Efface l'état ACTIF d'un profil supprimé : dérogations, rallonge, exceptions de
    domaine qui lui sont propres, déblocages temporaires et appareils déjà vus. Son historique n'est pas
    touché (cf. purge_profile_history). Sans ce ménage, un futur profil créé sous la même
    clé hériterait de ces états.
    """
    out = {}
    with get_db() as conn:
        for table in ("schedule_overrides", "temp_overrides", "slot_extensions",
                      "domain_rules", "temp_domain_unblocks", "seen_devices",
                      "vpn_sessions"):
            cur = conn.execute(f"DELETE FROM {table} WHERE profile = ?", (profile,))
            out[table] = cur.rowcount
    return out


def record_seen_devices(profile: str, devices: list, quand: str = "") -> None:
    """Note que ces appareils (dicts avec mac, hostname, ip) sont vus maintenant.

    Le nom n'est remplacé que par un nom non vide : un appareil qui cesse d'annoncer son
    nom garde le dernier connu, c'est celui que le parent reconnaîtra.
    """
    quand = quand or datetime.now().isoformat()
    lignes = [(profile, d.get("mac", ""), d.get("hostname", "") or "", d.get("ip", "") or "",
               quand, quand) for d in devices or [] if d.get("mac")]
    if not lignes:
        return
    with get_db() as conn:
        conn.executemany("""
            INSERT INTO seen_devices (profile, mac, name, last_ip, first_seen, last_seen)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(profile, mac) DO UPDATE SET
                name = CASE WHEN excluded.name != '' THEN excluded.name ELSE name END,
                last_ip = CASE WHEN excluded.last_ip != '' THEN excluded.last_ip ELSE last_ip END,
                last_seen = excluded.last_seen
        """, lignes)


def get_seen_devices(profile: str, limit: int = 5) -> list[dict]:
    """Appareils déjà vus de cet enfant, le plus récemment vu d'abord."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT mac, name, last_ip, first_seen, last_seen FROM seen_devices "
            "WHERE profile=? ORDER BY last_seen DESC LIMIT ?", (profile, limit)).fetchall()
    return [dict(r) for r in rows]


def seen_profiles_by_mac() -> dict[str, str]:
    """{adresse matérielle: enfant} des appareils déjà vus ; pour une adresse vue chez
    deux enfants, le plus récent l'emporte."""
    with get_db() as conn:
        rows = conn.execute("SELECT mac, profile FROM seen_devices ORDER BY last_seen").fetchall()
    return {r["mac"]: r["profile"] for r in rows}


def purge_profile_history(profile: str) -> dict:
    """Efface tout l'historique d'UN enfant, sans toucher à sa configuration.

    Répond à deux besoins que rien ne couvrait : effacer les données d'un enfant qui a
    grandi ou qui le demande, et ne pas laisser de lignes orphelines quand un profil est
    supprimé (daily_usage, dns_timeline et events conservaient sa clé indéfiniment).
    """
    out = {}
    with get_db() as conn:
        # seen_devices : les dates de dernière visite de ses appareils sont aussi une
        # trace de présence de l'enfant.
        for table in ("dns_timeline", "daily_usage", "schedule_overrides", "seen_devices",
                      "vpn_sessions", "daily_traffic"):
            cur = conn.execute(f"DELETE FROM {table} WHERE profile = ?", (profile,))
            out[table] = cur.rowcount
        cur = conn.execute("DELETE FROM events WHERE profile = ?", (profile,))
        out["events"] = cur.rowcount
        conn.execute("""
            DELETE FROM acknowledged_reports
            WHERE event_id NOT IN (SELECT id FROM events)
        """)
    return out


def get_dns_hits(profile: str, date: str, domain: str = None) -> list:
    """
    Retourne les hits DNS horodatés pour un profil/date.
    Si domain est fourni, filtre sur ce domaine racine.
    """
    start = f"{date}T00:00:00"
    end   = f"{date}T23:59:59"
    with get_db() as conn:
        if domain:
            rows = conn.execute("""
                SELECT timestamp, domain FROM dns_timeline
                WHERE profile=? AND domain=? AND timestamp BETWEEN ? AND ?
                ORDER BY timestamp ASC
            """, (profile, domain, start, end)).fetchall()
        else:
            rows = conn.execute("""
                SELECT timestamp, domain FROM dns_timeline
                WHERE profile=? AND timestamp BETWEEN ? AND ?
                ORDER BY timestamp ASC
            """, (profile, start, end)).fetchall()
    return [dict(r) for r in rows]


def get_events_for_date(profile: str, date: str) -> list:
    """Retourne les événements d'un profil pour une date donnée."""
    with get_db() as conn:
        rows = conn.execute("""
            SELECT timestamp, type, domain, message, message_key, params FROM events
            WHERE profile=? AND timestamp LIKE ?
            ORDER BY timestamp ASC
        """, (profile, f"{date}%")).fetchall()
    return [dict(r) for r in rows]


def set_device_override(ip: str, duration_minutes: int, taken_by: str = "parent"):
    now = datetime.now()
    expires_at = (now + timedelta(minutes=duration_minutes)).isoformat()
    with get_db() as conn:
        conn.execute("""
            INSERT INTO device_overrides (ip, expires_at, taken_by, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(ip) DO UPDATE SET
                expires_at=excluded.expires_at,
                taken_by=excluded.taken_by,
                created_at=excluded.created_at
        """, (ip, expires_at, taken_by, now.isoformat()))


def get_device_override(ip: str) -> dict | None:
    now = datetime.now().isoformat()
    with get_db() as conn:
        row = conn.execute(
            "SELECT ip, expires_at, taken_by, created_at FROM device_overrides WHERE ip=? AND expires_at > ?",
            (ip, now)
        ).fetchone()
    return dict(row) if row else None


def get_expired_override_ips() -> list[str]:
    now = datetime.now().isoformat()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT ip FROM device_overrides WHERE expires_at <= ?", (now,)
        ).fetchall()
    return [r["ip"] for r in rows]


def clear_device_override(ip: str):
    with get_db() as conn:
        conn.execute("DELETE FROM device_overrides WHERE ip=?", (ip,))


# ------------------------------------------------------------------ #
#  État temporaire persistant (survit au redémarrage du service)      #
# ------------------------------------------------------------------ #

def set_temp_override(profile: str, mode: str, expires_at: str):
    with get_db() as conn:
        conn.execute("""
            INSERT INTO temp_overrides (profile, mode, expires_at, created_at)
            VALUES (?,?,?,?)
            ON CONFLICT(profile) DO UPDATE SET
                mode=excluded.mode, expires_at=excluded.expires_at,
                created_at=excluded.created_at
        """, (profile, mode, expires_at, datetime.now().isoformat()))


def get_temp_overrides() -> list[dict]:
    """Tous les overrides temporaires, expirés compris (le tri revient à l'appelant)."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT profile, mode, expires_at FROM temp_overrides").fetchall()
    return [dict(r) for r in rows]


def clear_temp_override(profile: str) -> bool:
    """Retire la dérogation. True si une entrée existait VRAIMENT.

    Le retour compte : la fin d'une dérogation est réclamée par deux chemins — le
    minuteur posé par l'appelant et le cycle du monitor, qui rattrape les échéances
    quand un redémarrage a emporté le minuteur. Sans savoir qui a effacé, tous deux
    journalisaient la fin et le parent voyait l'événement en double.
    """
    with get_db() as conn:
        cur = conn.execute("DELETE FROM temp_overrides WHERE profile=?", (profile,))
        return cur.rowcount > 0


def set_temp_domain_unblock(domain: str, profile: str, expires_at: str):
    with get_db() as conn:
        conn.execute("""
            INSERT INTO temp_domain_unblocks (domain, profile, expires_at, created_at)
            VALUES (?,?,?,?)
            ON CONFLICT(domain, profile) DO UPDATE SET
                expires_at=excluded.expires_at, created_at=excluded.created_at
        """, (domain, profile, expires_at, datetime.now().isoformat()))


def clear_temp_domain_unblock(domain: str, profile: str | None = None) -> bool:
    """Retire le déblocage de ce domaine pour cet enfant, ou pour tous si profile est None.

    True si un déblocage existait — ce qui autorise l'appelant à journaliser une
    annulation sans l'inventer, comme pour les autres retraits d'exception."""
    with get_db() as conn:
        if profile is None:
            cur = conn.execute("DELETE FROM temp_domain_unblocks WHERE domain=?", (domain,))
        else:
            cur = conn.execute("DELETE FROM temp_domain_unblocks WHERE domain=? AND profile=?",
                               (domain, profile))
        return cur.rowcount > 0


def active_temp_unblocks(profile: str) -> set[str]:
    """Domaines débloqués et non échus pour cet enfant (une ligne sans enfant vaut pour
    tous)."""
    now = datetime.now().isoformat()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT domain FROM temp_domain_unblocks WHERE expires_at > ? "
            "AND (profile=? OR profile='')", (now, profile)).fetchall()
    return {r["domain"] for r in rows}


def set_slot_extension(profile: str, minutes: int, day: str, slot_start: str = ""):
    """Rallonge de la plage qui commence à `slot_start` le jour `day` (une par profil)."""
    with get_db() as conn:
        conn.execute("""
            INSERT INTO slot_extensions (profile, minutes, day, updated_at, slot_start)
            VALUES (?,?,?,?,?)
            ON CONFLICT(profile) DO UPDATE SET
                minutes=excluded.minutes, day=excluded.day, updated_at=excluded.updated_at,
                slot_start=excluded.slot_start
        """, (profile, int(minutes), day, datetime.now().isoformat(), slot_start))


def get_slot_extension(profile: str) -> tuple[int, str, str]:
    """(minutes, jour, début de la plage prolongée) ; (0, '', '') si aucune."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT minutes, day, slot_start FROM slot_extensions WHERE profile=?",
            (profile,)).fetchone()
    return (row["minutes"], row["day"], row["slot_start"]) if row else (0, "", "")


def get_temp_domain_unblocks() -> list[dict]:
    """Déblocages temporaires ENCORE actifs, le plus proche de l'échéance d'abord.

    Manquait au produit : ces déblocages étaient accordés par le chat, expiraient tout
    seuls, et n'étaient visibles nulle part entre les deux. Un parent ne pouvait donc ni
    savoir qu'un domaine était ouvert, ni refermer avant l'heure.
    """
    now = datetime.now().isoformat()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT domain, profile, expires_at, created_at FROM temp_domain_unblocks "
            "WHERE expires_at > ? ORDER BY expires_at ASC", (now,)).fetchall()
    return [dict(r) for r in rows]


def get_slot_extensions(day: str = "") -> list[dict]:
    """Rallonges de plage enregistrées, limitées à ce jour s'il est fourni.

    La table garde une ligne par profil, remise à zéro par comparaison de date et non
    par suppression : une rallonge d'hier y traîne donc, et seule la date dit si elle
    s'applique encore (cf. scheduler.get_current_slot, qui fait la même comparaison).
    """
    with get_db() as conn:
        if day:
            rows = conn.execute(
                "SELECT profile, minutes, day, updated_at, slot_start FROM slot_extensions "
                "WHERE day=? AND minutes > 0", (day,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT profile, minutes, day, updated_at, slot_start FROM slot_extensions "
                "WHERE minutes > 0").fetchall()
    return [dict(r) for r in rows]


def clear_slot_extension(profile: str) -> bool:
    """Annule la rallonge d'un profil. True si elle existait et valait quelque chose."""
    with get_db() as conn:
        cur = conn.execute(
            "DELETE FROM slot_extensions WHERE profile=? AND minutes > 0", (profile,))
        return cur.rowcount > 0


def pop_expired_domain_unblocks() -> list[dict]:
    """Retourne les déblocages temporaires échus ET les supprime, en une seule passe.

    Retrait immédiat pour qu'un cycle qui échoue ensuite ne reste pas à les resignaler
    indéfiniment — la resynchronisation des blacklists est de toute façon idempotente.
    """
    now = datetime.now().isoformat()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT domain, profile FROM temp_domain_unblocks WHERE expires_at <= ?",
            (now,)).fetchall()
        if rows:
            conn.execute("DELETE FROM temp_domain_unblocks WHERE expires_at <= ?", (now,))
    return [dict(r) for r in rows]


def get_override_for_date(profile: str, date: str) -> dict | None:
    """Retourne l'override de planning pour un profil/date, ou None."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT mode, reason FROM schedule_overrides WHERE profile=? AND date=?",
            (profile, date)
        ).fetchone()
    return dict(row) if row else None


# Dérogations de JOURNÉE. Le même INSERT était recopié dans dashboard.py et dans
# claude_agent.py, et la lecture n'existait que sous forme de SELECT inline : trois
# écritures de la même table, dont une seule journalisait. Ces trois accesseurs sont
# désormais le seul chemin, comme pour domain_rules.

def set_schedule_override(profile: str, date: str, mode: str, reason: str = "") -> None:
    """Pose ou remplace la dérogation d'un jour. Le mode est supposé DÉJÀ normalisé :
    valider l'ancien vocabulaire chez l'appelant puis écrire la valeur brute ici ferait
    cohabiter deux noms du même mode dans la table."""
    with get_db() as conn:
        conn.execute("""
            INSERT INTO schedule_overrides (profile, date, mode, reason, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(profile, date) DO UPDATE SET
                mode=excluded.mode, reason=excluded.reason,
                created_at=excluded.created_at
        """, (profile, date, mode, reason, datetime.now().isoformat()))


def get_schedule_overrides(since: str = "") -> list[dict]:
    """Dérogations de journée, la plus proche d'abord.

    `since` (date ISO) ne garde que les jours à partir de cette date : c'est ce que
    l'écran des exceptions demande, parce qu'une dérogation passée n'est plus une
    exception en cours mais une ligne d'historique.
    """
    with get_db() as conn:
        if since:
            rows = conn.execute(
                "SELECT profile, date, mode, reason, created_at FROM schedule_overrides "
                "WHERE date >= ? ORDER BY date ASC", (since,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT profile, date, mode, reason, created_at FROM schedule_overrides "
                "ORDER BY date DESC").fetchall()
    return [dict(r) for r in rows]


def clear_schedule_override(profile: str, date: str) -> bool:
    """Retire la dérogation d'un jour. True si elle existait — c'est ce qui autorise
    l'appelant à journaliser une annulation sans l'inventer."""
    with get_db() as conn:
        cur = conn.execute(
            "DELETE FROM schedule_overrides WHERE profile=? AND date=?", (profile, date))
        return cur.rowcount > 0


def get_usage_range(profile: str, days: int) -> list[dict]:
    """Usage DNS par domaine pour les N derniers jours."""
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    with get_db() as conn:
        rows = conn.execute("""
            SELECT date, domain, queries FROM daily_usage
            WHERE profile=? AND date >= ?
            ORDER BY date ASC, queries DESC
        """, (profile, cutoff)).fetchall()
    return [dict(r) for r in rows]


def get_events_range(profile: str, days: int) -> list[dict]:
    """Événements pour les N derniers jours."""
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    with get_db() as conn:
        rows = conn.execute("""
            SELECT timestamp, type, domain, message FROM events
            WHERE profile=? AND timestamp >= ?
            ORDER BY timestamp ASC
        """, (profile, cutoff)).fetchall()
    return [dict(r) for r in rows]


def init_domains_table(conn=None):
    """Ajoute la table domains si absente — appelé par init_db()."""
    sql = """
        -- Exceptions par domaine, POSÉES PAR LE PARENT, au-dessus de la grille d'accès.
        --
        -- La grille décide par CATÉGORIE ; cette table traite les cas particuliers, qui
        -- sont la moitié de l'usage réel : autoriser nommément une chaîne éducative
        -- classée « divertissement », ou couper un site précis qu'une catégorie autorise.
        --
        -- `profile` vide signifie « tous les enfants ». Sans cette distinction, autoriser
        -- YouTube pour l'aîné l'ouvrirait aussi au plus jeune, ce qui contredirait la
        -- grille par enfant. Une règle nominative l'emporte sur une règle globale.
        --
        -- Remplace les colonnes blocked_work et blocked_permissive de la table domains,
        -- qui ne pouvaient être que globales et ne connaissaient que le blocage.
        CREATE TABLE IF NOT EXISTS domain_rules (
            profile     TEXT NOT NULL DEFAULT '',
            domain      TEXT NOT NULL,
            mode        TEXT NOT NULL,
            allow       INTEGER NOT NULL,
            created_at  TEXT NOT NULL,
            PRIMARY KEY (profile, domain, mode)
        );

        CREATE TABLE IF NOT EXISTS domains (
            domain              TEXT PRIMARY KEY,
            category            TEXT DEFAULT 'unknown',
            hits_total          INTEGER DEFAULT 0,
            blocked_work        INTEGER DEFAULT 0,
            blocked_permissive  INTEGER DEFAULT 0,
            first_seen          TEXT,
            last_seen           TEXT,
            last_mode           TEXT,
            categorized_at      TEXT,
            categorized_by      TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_domains_category
            ON domains(category);
        CREATE INDEX IF NOT EXISTS idx_domain_rules_lookup
            ON domain_rules(mode, profile);
    """
    if conn:
        conn.executescript(sql)
    else:
        with sqlite3.connect(DB_PATH) as c:
            c.executescript(sql)
            c.commit()
