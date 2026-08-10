"""JSON / Excel storage helpers and settings access."""
from __future__ import annotations

import json
import os
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "settings.json"
CONFIG_EXAMPLE_PATH = ROOT / "config" / "settings.example.json"
DATA_DIR = ROOT / "data"
BACKUP_DIR = DATA_DIR / "backups"
STATIC_DIR = ROOT / "static"
TEAM_LOGO_DIR = STATIC_DIR / "uploads" / "teams"

USERS_FILE = DATA_DIR / "users.json"
QUERIES_FILE = DATA_DIR / "queries.json"
PROGRESS_FILE = DATA_DIR / "progress.json"
TEAM_SCORES_FILE = DATA_DIR / "team_scores.json"
TOP_PER_TEAM_FILE = DATA_DIR / "top_per_team.json"
SUPERSTEPPERS_FILE = DATA_DIR / "supersteppers.json"
ACTIVITIES_XLSX = DATA_DIR / "combined_athlete_activities.xlsx"
POINTS_XLSX = DATA_DIR / "athlete_total_points.xlsx"
INDIVIDUAL_POINTS_XLSX = DATA_DIR / "computed_total_points.xlsx"
TOP_MALE_XLSX = DATA_DIR / "max_distance_male.xlsx"
TOP_FEMALE_XLSX = DATA_DIR / "max_distance_female.xlsx"

DEFAULT_SETTINGS = {
    "app_name": "StepUp Challenge",
    "event_year": 2026,
    "start_date": "2026-09-01",
    "end_date": "2026-09-30",
    "teams": [
        {"code": "GS", "name": "Green Steppers", "color": "#22c55e"},
        {"code": "SS", "name": "Swift Steppers", "color": "#3b82f6"},
        {"code": "TT", "name": "Trail Trekers", "color": "#f59e0b"},
        {"code": "AE", "name": "Aero Elite", "color": "#ef4444"},
    ],
    "scoring": {
        "max_km_per_day": 15,
        "top_athletes_per_team": 15,
        "min_pace_min_per_km": 4.5,
        "individual_max_km_per_day": 60,
    },
    "superstepper": {
        "min_km_per_day": 3,
        "required_days": 30,
        "require_exact_days": False,
    },
    "strava": {
        "client_id": "",
        "client_secret": "",
        "redirect_uri": "http://localhost:5000/authorized",
        "authorize_url": "https://www.strava.com/oauth/authorize",
        "token_url": "https://www.strava.com/oauth/token",
        "deauthorize_url": "https://www.strava.com/oauth/deauthorize",
        "activities_url": "https://www.strava.com/api/v3/athlete/activities",
    },
    "admin": {"password": "RorYin#1"},
}


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    TEAM_LOGO_DIR.mkdir(parents=True, exist_ok=True)
    (ROOT / "config").mkdir(parents=True, exist_ok=True)


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return deepcopy(default) if default is not None else None
    with open(path, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return deepcopy(default) if default is not None else None


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def load_settings() -> dict:
    ensure_dirs()
    settings = read_json(CONFIG_PATH, None)
    if not settings:
        example = read_json(CONFIG_EXAMPLE_PATH, DEFAULT_SETTINGS) or deepcopy(DEFAULT_SETTINGS)
        settings = deepcopy(example)
        write_json(CONFIG_PATH, settings)
    return settings


def save_settings(settings: dict) -> None:
    write_json(CONFIG_PATH, settings)


def team_codes(settings: dict | None = None) -> list[str]:
    settings = settings or load_settings()
    return [t["code"] for t in settings.get("teams", [])]


def extract_team(name: str, teams: list[str] | None = None) -> str | None:
    """Support Name_TEAM suffix used by the legacy app."""
    if not name or "_" not in name:
        return None
    suffix = name.rsplit("_", 1)[-1]
    teams = teams or team_codes()
    return suffix if suffix in teams else None


def base_athlete_name(name: str, teams: list[str] | None = None) -> str:
    """Strip a known team suffix from an athlete label."""
    if not name:
        return ""
    teams = teams or team_codes()
    if "_" in name:
        base, suffix = name.rsplit("_", 1)
        if suffix in teams:
            return base.strip()
    return str(name).strip()


def canonical_athlete_name(name: str, team: str | None = None, teams: list[str] | None = None) -> str:
    """
    Normalize to 'Display Name_TEAM' when a team is known.
    Example: ('Dhanush N', 'GS') -> 'Dhanush N_GS'
    """
    teams = teams or team_codes()
    base = base_athlete_name(name, teams)
    team_code = team or extract_team(name, teams)
    if team_code and team_code in teams:
        return f"{base}_{team_code}"
    return base


def get_users() -> list[dict]:
    return read_json(USERS_FILE, []) or []


def save_users(users: list[dict]) -> None:
    write_json(USERS_FILE, users)


def register_authorization(entry: dict, max_auths: int = 2) -> tuple[str, dict | None, str]:
    """
    Store or refresh an authorized athlete, limited to max_auths times.

    Returns (status, user, message) where status is:
      - created
      - updated
      - blocked
    """
    users = get_users()
    teams = team_codes()
    existing = next((u for u in users if u.get("athlete_id") == entry["athlete_id"]), None)

    if existing:
        count = int(existing.get("auth_count") or 1)
        if count >= max_auths:
            return (
                "blocked",
                existing,
                (
                    "You have already authorized twice before. "
                    "If you want any changes in profile, please contact admin through Query."
                ),
            )

        incoming_name = entry.get("name") or existing.get("name", "")
        if not existing.get("team"):
            existing["team"] = extract_team(incoming_name, teams) or entry.get("team")

        existing["name"] = canonical_athlete_name(
            incoming_name or existing.get("name", ""),
            existing.get("team"),
            teams,
        )
        # Refresh tokens / metadata on allowed re-auth
        for key in ("gender", "refresh_token", "expires_at", "access_token", "authorized_at"):
            if key in entry and entry[key] is not None:
                # Don't overwrite an already chosen gender with empty Strava value
                if key == "gender" and existing.get("gender") in {"M", "F"} and not entry.get("gender"):
                    continue
                existing[key] = entry[key]

        existing["auth_count"] = count + 1
        existing["last_authorized_at"] = entry.get("authorized_at")
        save_users(users)
        return "updated", existing, existing.get("name", "Athlete")

    team = entry.get("team") or extract_team(entry.get("name", ""), teams)
    entry["team"] = team
    entry["name"] = canonical_athlete_name(entry.get("name", ""), team, teams)
    entry["auth_count"] = 1
    entry["last_authorized_at"] = entry.get("authorized_at")
    users.append(entry)
    save_users(users)
    return "created", entry, entry.get("name", "Athlete")


def upsert_user(entry: dict) -> tuple[bool, str]:
    """Backward-compatible wrapper around register_authorization. """
    status, user, message = register_authorization(entry)
    if status == "blocked":
        return False, message
    return status == "created", (user or {}).get("name", message)


def remove_user(athlete_id: int) -> bool:
    users = get_users()
    new_users = [u for u in users if u.get("athlete_id") != athlete_id]
    if len(new_users) == len(users):
        return False
    save_users(new_users)
    return True


def update_user_profile(athlete_id: int, *, team: str | None = None, gender: str | None = None) -> dict | None:
    """Update team/gender for an athlete and keep Name_TEAM in sync."""
    users = get_users()
    teams = team_codes()
    for user in users:
        if user.get("athlete_id") != athlete_id:
            continue
        if team is not None:
            cleaned_team = team.strip().upper() if team else None
            if cleaned_team and cleaned_team not in teams:
                raise ValueError(f"Invalid team: {cleaned_team}")
            user["team"] = cleaned_team or None
            user["name"] = canonical_athlete_name(user.get("name", ""), user.get("team"), teams)
        if gender is not None:
            cleaned_gender = gender.strip().upper() if gender else None
            if cleaned_gender and cleaned_gender not in {"M", "F"}:
                raise ValueError("Gender must be M or F")
            user["gender"] = cleaned_gender
        user["profile_completed_at"] = datetime.now().isoformat(timespec="seconds")
        save_users(users)
        return user
    return None


def update_user_team(athlete_id: int, team: str) -> bool:
    return update_user_profile(athlete_id, team=team) is not None


def sync_user_display_names() -> int:
    """Ensure every user name includes _TEAM when team is set. Returns updated count."""
    users = get_users()
    teams = team_codes()
    changed = 0
    for user in users:
        team = user.get("team") or extract_team(user.get("name", ""), teams)
        user["team"] = team
        new_name = canonical_athlete_name(user.get("name", ""), team, teams)
        if new_name != user.get("name"):
            user["name"] = new_name
            changed += 1
    if changed:
        save_users(users)
    return changed


def get_queries() -> list[dict]:
    return read_json(QUERIES_FILE, []) or []


def add_query(entry: dict) -> None:
    queries = get_queries()
    queries.insert(0, entry)
    write_json(QUERIES_FILE, queries)


def get_progress() -> list[dict]:
    return read_json(PROGRESS_FILE, []) or []


def save_progress(progress: list[dict]) -> None:
    write_json(PROGRESS_FILE, progress)


def reset_progress(users: list[dict] | None = None) -> list[dict]:
    users = users if users is not None else get_users()
    progress = [
        {"athlete_id": u["athlete_id"], "athlete_name": u["name"], "status": "pending"}
        for u in users
    ]
    save_progress(progress)
    return progress


def team_logo_path(code: str | None) -> str | None:
    """Return static-relative logo path for a team code if the file exists."""
    if not code:
        return None
    path = STATIC_DIR / "images" / f"{str(code).upper()}.png"
    if path.exists():
        return f"images/{str(code).upper()}.png"
    return None


def with_team_logos(teams: list[dict] | None = None) -> list[dict]:
    """Attach logo paths from static/images/{CODE}.png onto team configs."""
    settings_teams = teams if teams is not None else load_settings().get("teams", [])
    enriched = []
    for team in settings_teams:
        item = dict(team)
        auto = team_logo_path(item.get("code"))
        if auto:
            item["logo"] = auto
        enriched.append(item)
    return enriched


def profile_is_complete(user: dict | None) -> bool:
    if not user:
        return False
    return bool(user.get("team")) and user.get("gender") in {"M", "F"}
