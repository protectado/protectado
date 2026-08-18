# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Arnaud Ortais
# Dual-licensed: AGPL-3.0 (open source) or Commercial License — see LICENSE and LICENSE-COMMERCIAL.
"""
scheduler.py — Calcule le slot d'accès actuel par profil et jour.
Le planning est lu depuis config.json (profiles[key]["schedule"]).
"""

import json
import threading
from datetime import datetime, time, timedelta
from paths import CONFIG_PATH

MODE_LABELS = {
    "blocked":    "🔴 Bloqué",
    "work":       "📚 Travail",
    "permissive": "🟢 Libre",
}

_DAY_KEYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

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


def clear_temp_override(profile: str) -> None:
    import database as db
    if profile in _temp_timers:
        _temp_timers.pop(profile).cancel()
    db.clear_temp_override(profile)


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


def get_current_slot(profile: str, now: datetime = None) -> dict:
    if now is None:
        now = datetime.now()

    try:
        config = _load_config()
        profile_data = config.get("profiles", {}).get(profile, {})
    except Exception:
        profile_data = {}

    is_weekend = now.weekday() >= 5
    day_key  = _DAY_KEYS[now.weekday()]
    schedule = profile_data.get("schedule", {})
    schedule_list = schedule.get(day_key, [])
    # Rétrocompatibilité avec l'ancien format weekday/weekend
    if not schedule_list:
        legacy = "weekend" if is_weekend else "weekday"
        schedule_list = schedule.get(legacy, [])
    current_time = now.time()
    import database as db
    ext_minutes, ext_day = db.get_slot_extension(profile)
    extra_min = ext_minutes if ext_day == now.date().isoformat() else 0

    for slot in schedule_list:
        start = _parse_time(slot["start"])
        end   = _parse_time(slot["end"])
        if start <= current_time <= end:
            # Slot actif — appliquer l'extension uniquement à ce slot
            if extra_min:
                end_dt = datetime.combine(now.date(), end) + timedelta(minutes=extra_min)
                end = end_dt.time()
            return {
                "mode":                slot["mode"],
                "profile":             profile,
                "slot_start":          slot["start"],
                "slot_end":            end.strftime("%H:%M"),
                "next_change_minutes": _time_until(now, end),
                "day":                 day_key,
            }
        if extra_min and start <= current_time:
            end_dt = datetime.combine(now.date(), end) + timedelta(minutes=extra_min)
            if current_time <= end_dt.time():
                ext_end = end_dt.time()
                return {
                    "mode":                slot["mode"],
                    "profile":             profile,
                    "slot_start":          slot["start"],
                    "slot_end":            ext_end.strftime("%H:%M"),
                    "next_change_minutes": _time_until(now, ext_end),
                    "day":                 day_key,
                }

    return {
        "mode":                "blocked",
        "profile":             profile,
        "slot_start":          "00:00",
        "slot_end":            "23:59",
        "next_change_minutes": 0,
        "day":                 day_key,
    }


def extend_current_slot(profile: str, minutes: int) -> bool:
    """Rallonge cumulative de la plage en cours, remise à zéro chaque jour."""
    import database as db
    today = datetime.now().date().isoformat()
    current_minutes, current_day = db.get_slot_extension(profile)
    accumulated = current_minutes if current_day == today else 0
    db.set_slot_extension(profile, accumulated + minutes, today)
    return True


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
    if override and override["mode"] != "normal":
        raw_mode = override["mode"]
        mode = "permissive" if raw_mode == "free" else raw_mode
        return {
            "mode":           mode,
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
