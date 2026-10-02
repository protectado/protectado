# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Arnaud Ortais
# Dual-licensed: AGPL-3.0 (open source) or Commercial License — see LICENSE and LICENSE-COMMERCIAL.
"""
scheduler.py — Calcule le slot d'accès actuel par profil et jour.
Le planning est lu depuis config.json (profiles[key]["schedule"]).
"""

import json
import re
import threading
from datetime import datetime, time, timedelta
import modes
from paths import CONFIG_PATH

# Les libellés des modes ne sont PLUS ici. Ils étaient codés en dur en français, donc un
# boîtier en anglais lisait « 📚 Travail » dans ses rapports, alors que les mêmes libellés
# existaient déjà dans i18n/. Le vocabulaire des modes vit dans modes.py, leurs libellés
# dans i18n/, et ce module ne fait que des horaires.

_DAY_KEYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
# Clés acceptées dans un planning : les sept jours, plus l'ancien format encore lu par
# _slots_for_weekday (un planning restauré d'une sauvegarde peut le porter).
SCHEDULE_KEYS = (*_DAY_KEYS, "weekday", "weekend")
_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

# Plages illisibles déjà signalées, pour ne pas répéter le même message à chaque cycle.
_bad_slots_logged: set = set()


def validate_schedule(schedule) -> tuple[str, dict] | None:
    """(error_code, params) du premier défaut d'un planning, ou None s'il est valide.

    Refusé : jour inconnu, heure hors format HH:MM, début qui n'est pas avant la fin,
    mode qui n'est pas un mode de plage, deux plages qui se chevauchent le même jour.
    Deux plages bout à bout (10:00-12:00 puis 12:00-14:00) ne se chevauchent pas.
    """
    if not isinstance(schedule, dict):
        return "schedule_bad_day", {"day": ""}
    for day, slots in schedule.items():
        if day not in SCHEDULE_KEYS or not isinstance(slots, list):
            return "schedule_bad_day", {"day": str(day)}
        bornes = []
        for slot in slots:
            if not isinstance(slot, dict):
                return "schedule_bad_time", {"day": day}
            start, end = slot.get("start"), slot.get("end")
            if not (isinstance(start, str) and isinstance(end, str)
                    and _HHMM_RE.match(start) and _HHMM_RE.match(end)):
                return "schedule_bad_time", {"day": day}
            if start >= end:
                return "schedule_bad_order", {"day": day}
            if not modes.is_slot_mode(slot.get("mode")):
                return "schedule_bad_mode", {"day": day}
            bornes.append((start, end))
        bornes.sort()
        for (_, fin), (debut, _) in zip(bornes, bornes[1:]):
            if debut < fin:
                return "schedule_overlap", {"day": day}
    return None

# Overrides temporaires et rallonges de plage : la BASE fait autorité, plus la mémoire.
# Auparavant ces états ne vivaient que dans des dictionnaires de module et des
# threading.Timer : un redémarrage du service — l'auto-update en déclenche un chaque nuit —
# effaçait silencieusement une dérogation accordée par le parent, sans jamais restaurer le
# planning. Les timers subsistent chez les appelants pour la réactivité immédiate, mais la
# correction ne dépend plus d'eux : le cycle du monitor rattrape toute échéance.
_temp_timers: dict[str, threading.Timer] = {}


def set_temp_override(profile: str, mode: str, minutes: int) -> None:
    import database as db
    if profile in _temp_timers:
        _temp_timers.pop(profile).cancel()
    expires_at = datetime.now() + timedelta(minutes=minutes)
    db.set_temp_override(profile, mode, expires_at.isoformat())


def register_temp_timer(profile: str, timer: threading.Timer) -> None:
    if profile in _temp_timers:
        _temp_timers.pop(profile).cancel()
    _temp_timers[profile] = timer


def clear_temp_override(profile: str) -> bool:
    """Retire la dérogation et son minuteur. True si une dérogation existait encore —
    c'est ce qui autorise l'appelant à journaliser la fin (cf. database.clear_temp_override)."""
    import database as db
    if profile in _temp_timers:
        _temp_timers.pop(profile).cancel()
    return db.clear_temp_override(profile)


def _parse(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None


def get_all_temp_overrides() -> list[dict]:
    """Overrides temporaires ACTIFS (les entrées échues sont purgées au passage)."""
    import database as db
    now = datetime.now()
    active = []
    for row in db.get_temp_overrides():
        expires_at = _parse(row["expires_at"])
        if expires_at is None or now >= expires_at:
            clear_temp_override(row["profile"])
            continue
        active.append({
            "profile":      row["profile"],
            "mode":         row["mode"],
            "expires_at":   row["expires_at"],
            "minutes_left": max(1, int((expires_at - now).total_seconds() / 60)),
        })
    return active


def get_temp_override(profile: str) -> str | None:
    """Mode temporaire actif pour ce profil, ou None si absent/expiré."""
    import database as db
    now = datetime.now()
    for row in db.get_temp_overrides():
        if row["profile"] != profile:
            continue
        expires_at = _parse(row["expires_at"])
        if expires_at is None or now >= expires_at:
            clear_temp_override(profile)
            return None
        return row["mode"]
    return None


def _load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return json.load(f)


def _parse_time(s: str) -> time:
    return time.fromisoformat(s)


def _slots_for_weekday(profile_data: dict, weekday: int) -> list:
    """Plages déclarées pour ce jour de la semaine, format ancien compris.

    Extrait de get_current_slot pour que le calcul de la prochaine ouverture lise le
    planning EXACTEMENT comme le calcul du créneau courant : deux lectures divergentes
    annonceraient au parent une heure d'ouverture que le boîtier n'appliquerait pas.
    """
    schedule = profile_data.get("schedule", {}) or {}
    slots = schedule.get(_DAY_KEYS[weekday], [])
    if not slots:
        # Rétrocompatibilité avec l'ancien format weekday/weekend
        slots = schedule.get("weekend" if weekday >= 5 else "weekday", [])
    return slots or []


def _extension_window(profile: str, profile_data: dict) -> tuple[dict, datetime, datetime] | None:
    """(plage prolongée, début, fin rallongée) de la rallonge enregistrée, ou None.

    La rallonge désigne UNE plage : son jour et son heure de début. Elle ne vaut que si
    cette plage existe toujours au planning et qu'elle est ouverte ; sinon elle ne
    prolonge rien. Les bornes sont des datetime pour qu'une fin rallongée au-delà de
    minuit reste comparable.
    """
    import database as db
    minutes, day, slot_start = db.get_slot_extension(profile)
    if not minutes or not day or not slot_start:
        return None
    try:
        jour = datetime.fromisoformat(day).date()
    except ValueError:
        return None
    for slot in _slots_for_weekday(profile_data, jour.weekday()):
        if slot.get("start") != slot_start or not modes.is_open(slot.get("mode")):
            continue
        try:
            debut = datetime.combine(jour, _parse_time(slot["start"]))
            fin = datetime.combine(jour, _parse_time(slot["end"]))
        except (KeyError, TypeError, ValueError):
            return None
        return slot, debut, fin + timedelta(minutes=minutes)
    return None


def get_current_slot(profile: str, now: datetime = None) -> dict:
    if now is None:
        now = datetime.now()

    try:
        config = _load_config()
        profile_data = config.get("profiles", {}).get(profile, {})
    except Exception:
        profile_data = {}

    day_key  = _DAY_KEYS[now.weekday()]
    schedule_list = _slots_for_weekday(profile_data, now.weekday())
    current_time = now.time()

    # La rallonge passe en premier : elle prolonge l'accès par-dessus la plage qui suit,
    # et seulement jusqu'à sa fin rallongée.
    fenetre = _extension_window(profile, profile_data)
    if fenetre:
        slot, debut, fin = fenetre
        if debut <= now <= fin:
            return {
                "mode":                slot["mode"],
                "profile":             profile,
                "slot_start":          slot["start"],
                "slot_end":            fin.strftime("%H:%M"),
                "next_change_minutes": max(0, int((fin - now).total_seconds() / 60)),
                "day":                 day_key,
            }

    for slot in schedule_list:
        try:
            start = _parse_time(slot["start"])
            end   = _parse_time(slot["end"])
        except (KeyError, TypeError, ValueError):
            # Une plage illisible ne couvre rien : l'instant retombe sur les autres
            # plages, ou sur la fermeture par défaut.
            marque = (profile, repr(slot))
            if marque not in _bad_slots_logged:
                _bad_slots_logged.add(marque)
                print(f"[Scheduler] {profile} : plage illisible ignorée {slot!r}")
            continue
        if start <= current_time <= end:
            return {
                "mode":                slot["mode"],
                "profile":             profile,
                "slot_start":          slot["start"],
                "slot_end":            end.strftime("%H:%M"),
                "next_change_minutes": _time_until(now, end),
                "day":                 day_key,
            }

    # Aucune plage ne couvre cet instant : on ferme. C'est la règle affichée au parent
    # (« les heures non couvertes sont bloquées »), et le sens qui protège.
    return {
        "mode":                modes.OFF,
        "profile":             profile,
        "slot_start":          "00:00",
        "slot_end":            "23:59",
        "next_change_minutes": 0,
        "day":                 day_key,
    }


def _day_override_mode(profile: str, jour) -> str:
    """Mode imposé à ce jour par une dérogation de journée, ou chaîne vide.

    « normal » n'en est pas une : c'est la valeur qui rend la main au planning.
    """
    import database as db
    override = db.get_override_for_date(profile, jour.isoformat())
    mode = modes.normalize(override["mode"]) if override else ""
    return mode if mode and mode != modes.NORMAL else ""


def _scheduled_mode(profile: str, dt: datetime) -> str:
    """Mode que le PLANNING donne à cet instant, dérogation de journée comprise.

    Sans la dérogation TEMPORAIRE, volontairement : celle-ci ne se projette pas dans le
    futur, et get_slot_at la lit toujours à l'instant présent. L'interroger pour un
    instant à venir répondrait donc avec l'état d'aujourd'hui.
    """
    return _day_override_mode(profile, dt.date()) or get_current_slot(profile, dt)["mode"]


# Horizon de recherche de la prochaine ouverture : le jour courant entamé, plus une
# semaine complète. Au-delà, un planning qui n'ouvre jamais est un planning vide, et il
# vaut mieux ne rien annoncer qu'annoncer une date lointaine calculée par extrapolation.
LOOKAHEAD_DAYS = 8


def next_opening(profile: str, now: datetime = None) -> datetime | None:
    """Prochain instant, strictement après `now`, où l'accès s'OUVRE pour ce profil.

    POURQUOI. Quand aucune plage ne couvre l'instant présent, get_current_slot ferme et
    rapporte 00:00–23:59 : des bornes qui veulent dire « toute la journée est fermée »,
    pas « l'accès dure jusqu'à 23:59 ». Affichées telles quelles, elles annonçaient au
    parent « coupé jusqu'à 23h59 » à 21 h un mardi, alors que l'accès rouvre le mercredi
    à 7 h. L'information utile est l'heure de RÉOUVERTURE, et elle n'était calculée
    nulle part.

    Tient compte des dérogations de JOURNÉE : un jour mis en « coupé » par le parent ne
    peut pas offrir d'ouverture, même si son planning en déclare une. La dérogation
    temporaire, elle, n'entre pas ici : elle se compte en minutes restantes, et c'est ce
    que l'interface affiche déjà.

    Renvoie None si rien n'ouvre dans la semaine qui vient, ce qui est le cas d'un profil
    sans planning. Mieux vaut ne rien annoncer qu'une date extrapolée.
    """
    if now is None:
        now = datetime.now()
    try:
        profile_data = _load_config().get("profiles", {}).get(profile, {})
    except Exception:
        return None

    for delta in range(LOOKAHEAD_DAYS):
        jour = (now + timedelta(days=delta)).date()
        # Une dérogation de journée REMPLACE le planning du jour : si elle ferme, ce jour
        # n'ouvre pas, et s'il ouvre c'est dès 00:00 (elle couvre la journée entière).
        override_mode = _day_override_mode(profile, jour)
        if override_mode:
            if not modes.is_open(override_mode):
                continue
            debut = datetime.combine(jour, time(0, 0))
            # Pour un jour à venir, 00:00 est toujours dans le futur. Le cas contraire ne
            # se produit qu'aujourd'hui, et il signifie que l'accès est ouvert en ce
            # moment même : cette fonction n'a alors rien à annoncer.
            return debut if debut > now else now

        slots = _slots_for_weekday(profile_data, jour.weekday())
        for slot in sorted(slots, key=lambda x: str(x.get("start", ""))):
            if not modes.is_open(slot.get("mode")):
                continue
            try:
                debut = datetime.combine(jour, _parse_time(slot["start"]))
            except (KeyError, TypeError, ValueError):
                continue
            if debut > now:
                return debut
    return None


def reopening_at(profile: str, now: datetime = None) -> datetime | None:
    """Quand l'accès se rouvre pour ce profil, dérogation temporaire comprise.

    C'est la fonction que l'INTERFACE doit appeler. next_opening ne connaît que le
    planning ; ici on part de l'instant où l'état actuel cesse de s'appliquer.

    Une dérogation temporaire qui FERME repousse ce point de départ : afficher « encore
    45 min » laissait croire que l'accès rouvrirait dans 45 minutes, alors que le planning
    pouvait être fermé lui aussi à ce moment-là. À l'échéance, si le planning est ouvert,
    c'est là que l'accès rouvre ; sinon on cherche la prochaine ouverture au-delà.

    Renvoie None si l'accès est ouvert en ce moment (rien à annoncer) ou si rien n'ouvre
    dans la semaine qui vient.
    """
    if now is None:
        now = datetime.now()
    if modes.is_open(get_slot_at(profile, now).get("mode")):
        return None

    depart = now
    for row in get_all_temp_overrides():
        if row["profile"] != profile or modes.is_open(row["mode"]):
            continue
        echeance = _parse(row["expires_at"])
        if echeance and echeance > depart:
            depart = echeance

    if depart > now and modes.is_open(_scheduled_mode(profile, depart)):
        return depart
    return next_opening(profile, depart)


def extend_current_slot(profile: str, minutes: int, now: datetime = None) -> bool:
    """Rallonge la plage OUVERTE en cours. False si l'accès n'est pas ouvert.

    Les rallonges successives se cumulent sur la même plage ; une rallonge demandée sur
    une autre plage remplace la précédente. Une plage déjà rallongée reste « la plage en
    cours » pendant sa rallonge, pour que « encore 10 min » s'ajoute à ce qui court.
    """
    import database as db
    if now is None:
        now = datetime.now()
    if not modes.is_open(get_slot_at(profile, now).get("mode")):
        return False
    try:
        profile_data = _load_config().get("profiles", {}).get(profile, {})
    except Exception:
        return False

    fenetre = _extension_window(profile, profile_data)
    if fenetre and fenetre[1] <= now <= fenetre[2]:
        deja, jour, debut = db.get_slot_extension(profile)
        db.set_slot_extension(profile, deja + minutes, jour, debut)
        return True

    slot = get_current_slot(profile, now)
    if not modes.is_open(slot.get("mode")):
        return False
    db.set_slot_extension(profile, minutes, now.date().isoformat(), slot["slot_start"])
    return True


def active_extensions(now: datetime = None) -> list[dict]:
    """Rallonges pas encore échues, avec leur échéance, pour l'écran des exceptions.

    Une rallonge qui déborde après minuit est encore en cours le lendemain : filtrer sur
    la date du jour la ferait disparaître de l'écran alors qu'elle s'applique.
    """
    import database as db
    if now is None:
        now = datetime.now()
    try:
        profiles = _load_config().get("profiles", {}) or {}
    except Exception:
        return []
    actives = []
    for row in db.get_slot_extensions():
        fenetre = _extension_window(row["profile"], profiles.get(row["profile"]) or {})
        if fenetre and now <= fenetre[2]:
            actives.append({**row, "ends_at": fenetre[2].isoformat()})
    return actives


def get_slot_at(profile: str, dt: datetime) -> dict:
    temp_mode = get_temp_override(profile)
    if temp_mode:
        return {
            "mode":            temp_mode,
            "slot_start":      "00:00",
            "slot_end":        "23:59",
            "override":        True,
            "override_reason": "override_temporaire",
        }
    import database as db
    date_str = dt.strftime("%Y-%m-%d")
    override = db.get_override_for_date(profile, date_str)
    # `normalize` absorbe l'ancien vocabulaire, dont le doublon free/permissive qui
    # désignait le même mode selon qu'on regardait une dérogation ou un créneau.
    override_mode = modes.normalize(override["mode"]) if override else ""
    if override_mode and override_mode != modes.NORMAL:
        return {
            "mode":           override_mode,
            "slot_start":     "00:00",
            "slot_end":       "23:59",
            "override":       True,
            "override_reason": override.get("reason", ""),
        }
    slot = get_current_slot(profile, now=dt)
    slot["override"] = False
    return slot


def _time_until(now: datetime, target: time) -> int:
    target_dt = now.replace(
        hour=target.hour, minute=target.minute, second=0, microsecond=0
    )
    if target_dt <= now:
        return 0
    return int((target_dt - now).total_seconds() / 60)
