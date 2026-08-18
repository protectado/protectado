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
            CREATE TABLE IF NOT EXISTS temp_domain_unblocks (
                domain      TEXT PRIMARY KEY,
                profile     TEXT NOT NULL DEFAULT '',
                expires_at  TEXT NOT NULL,
                created_at  TEXT NOT NULL
            );

            -- Rallonges de plage horaire accordées dans la journée (« encore 20 min »).
            CREATE TABLE IF NOT EXISTS slot_extensions (
                profile     TEXT PRIMARY KEY,
                minutes     INTEGER NOT NULL,
                day         TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            );

            -- Délai de grâce des appareils du réseau ENFANTS (mode gateway).
            -- Un appareil qui rejoint le Wi-Fi du boîtier a un accès libre pendant
            -- network.new_device_grace_hours, puis doit être rattaché à un profil.
            -- Clé = MAC (l'IP change au gré des baux DHCP) ; l'IP est conservée pour
            -- l'affichage et pour retrouver l'appareil. first_seen n'est JAMAIS
            -- réécrit : le compte à rebours ne se remet pas à zéro à chaque connexion.
            -- Métadonnées applicatives (date de dernière purge, etc.)
            CREATE TABLE IF NOT EXISTS app_meta (
                key   TEXT PRIMARY KEY,
                value TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS device_grace (
                mac         TEXT PRIMARY KEY,
                ip          TEXT NOT NULL,
                hostname    TEXT DEFAULT '',
                first_seen  TEXT NOT NULL,
                notified_at TEXT DEFAULT ''
            );
        """)
        # Migration idempotente : ajoute message_key/params aux bases antérieures à
        # l'internationalisation du journal. ALTER TABLE ADD COLUMN n'est pas
        # « IF NOT EXISTS » en SQLite : on inspecte le schéma avant.
        _cols = {r[1] for r in conn.execute("PRAGMA table_info(events)")}
        for _col in ("message_key", "params"):
            if _col not in _cols:
                conn.execute(f"ALTER TABLE events ADD COLUMN {_col} TEXT DEFAULT ''")
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

    with get_db() as conn:
        rows = conn.execute("""
            SELECT timestamp FROM dns_timeline
            WHERE profile=? AND domain=?
              AND timestamp BETWEEN ? AND ?
            ORDER BY timestamp ASC
        """, (profile, domain, start, end)).fetchall()

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


def get_time_spent_today(profile: str) -> dict:
    """Retourne le temps estimé (en minutes) par domaine pour aujourd'hui."""
    today = date.today().isoformat()
    with get_db() as conn:
        domains = conn.execute("""
            SELECT DISTINCT domain FROM dns_timeline
            WHERE profile=? AND timestamp LIKE ?
        """, (profile, f"{today}%")).fetchall()

    return {
        row["domain"]: estimate_session_minutes(profile, row["domain"])
        for row in domains
    }


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


def get_recent_events(limit: int = 50) -> list:
    _REPORT_TYPES = ('daily_report', 'weekly_report', 'monthly_report')
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM events WHERE type NOT IN (?,?,?) ORDER BY timestamp DESC LIMIT ?",
            (*_REPORT_TYPES, limit)
        ).fetchall()
    return [dict(r) for r in rows]


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


def purge_profile_history(profile: str) -> dict:
    """Efface tout l'historique d'UN enfant, sans toucher à sa configuration.

    Répond à deux besoins que rien ne couvrait : effacer les données d'un enfant qui a
    grandi ou qui le demande, et ne pas laisser de lignes orphelines quand un profil est
    supprimé (daily_usage, dns_timeline et events conservaient sa clé indéfiniment).
    """
    out = {}
    with get_db() as conn:
        for table in ("dns_timeline", "daily_usage", "schedule_overrides"):
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
            SELECT timestamp, type, domain, message FROM events
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


def clear_temp_override(profile: str):
    with get_db() as conn:
        conn.execute("DELETE FROM temp_overrides WHERE profile=?", (profile,))


def set_temp_domain_unblock(domain: str, profile: str, expires_at: str):
    with get_db() as conn:
        conn.execute("""
            INSERT INTO temp_domain_unblocks (domain, profile, expires_at, created_at)
            VALUES (?,?,?,?)
            ON CONFLICT(domain) DO UPDATE SET
                profile=excluded.profile, expires_at=excluded.expires_at,
                created_at=excluded.created_at
        """, (domain, profile, expires_at, datetime.now().isoformat()))


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


def clear_temp_domain_unblock(domain: str):
    with get_db() as conn:
        conn.execute("DELETE FROM temp_domain_unblocks WHERE domain=?", (domain,))


def set_slot_extension(profile: str, minutes: int, day: str):
    with get_db() as conn:
        conn.execute("""
            INSERT INTO slot_extensions (profile, minutes, day, updated_at)
            VALUES (?,?,?,?)
            ON CONFLICT(profile) DO UPDATE SET
                minutes=excluded.minutes, day=excluded.day, updated_at=excluded.updated_at
        """, (profile, int(minutes), day, datetime.now().isoformat()))


def get_slot_extension(profile: str) -> tuple[int, str]:
    """(minutes, jour) de la rallonge en cours ; (0, '') si aucune."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT minutes, day FROM slot_extensions WHERE profile=?", (profile,)).fetchone()
    return (row["minutes"], row["day"]) if row else (0, "")


# ------------------------------------------------------------------ #
#  Délai de grâce des appareils du réseau enfants (mode gateway)      #
# ------------------------------------------------------------------ #

def record_device_seen(mac: str, ip: str, hostname: str = "") -> str:
    """Enregistre la PREMIÈRE apparition d'un appareil et renvoie son first_seen.

    Idempotent : un appareil déjà connu voit seulement son IP/nom rafraîchis —
    first_seen reste intact, sinon une reconnexion relancerait le délai à zéro.
    """
    now = datetime.now().isoformat()
    with get_db() as conn:
        conn.execute("""
            INSERT INTO device_grace (mac, ip, hostname, first_seen)
            VALUES (?,?,?,?)
            ON CONFLICT(mac) DO UPDATE SET
                ip=excluded.ip,
                hostname=CASE WHEN excluded.hostname != '' THEN excluded.hostname
                              ELSE device_grace.hostname END
        """, (mac, ip, hostname, now))
        row = conn.execute("SELECT first_seen FROM device_grace WHERE mac=?", (mac,)).fetchone()
    return row["first_seen"] if row else now


def get_device_grace() -> list[dict]:
    """Tous les appareils suivis, du plus ancien au plus récent."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT mac, ip, hostname, first_seen, notified_at FROM device_grace "
            "ORDER BY first_seen"
        ).fetchall()
    return [dict(r) for r in rows]


def mark_grace_notified(mac: str):
    """Marque l'échéance comme signalée — évite de reloguer l'événement à chaque cycle."""
    with get_db() as conn:
        conn.execute("UPDATE device_grace SET notified_at=? WHERE mac=?",
                     (datetime.now().isoformat(), mac))


def clear_device_grace(mac: str = "", ip: str = ""):
    """Sort un appareil du suivi (rattaché à un profil, ou ignoré par le parent)."""
    if not mac and not ip:
        return
    with get_db() as conn:
        if mac:
            conn.execute("DELETE FROM device_grace WHERE mac=?", (mac,))
        else:
            conn.execute("DELETE FROM device_grace WHERE ip=?", (ip,))


def get_override_for_date(profile: str, date: str) -> dict | None:
    """Retourne l'override de planning pour un profil/date, ou None."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT mode, reason FROM schedule_overrides WHERE profile=? AND date=?",
            (profile, date)
        ).fetchone()
    return dict(row) if row else None


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
    """
    if conn:
        conn.executescript(sql)
    else:
        with sqlite3.connect(DB_PATH) as c:
            c.executescript(sql)
            c.commit()
