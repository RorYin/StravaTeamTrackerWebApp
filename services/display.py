"""Helpers for displaying athlete names and teams."""
from __future__ import annotations

from services.storage import load_settings, team_codes


def split_athlete_label(raw: str | None, known_teams: list[str] | None = None) -> tuple[str, str | None]:
    """
    Split 'Dhanush N_GS' -> ('Dhanush N', 'GS').
    If no known team suffix, return the full string and None.
    """
    if not raw:
        return "", None
    text = str(raw).strip()
    if "_" not in text:
        return text, None

    teams = known_teams if known_teams is not None else team_codes()
    base, suffix = text.rsplit("_", 1)
    if suffix in teams:
        return base.strip() or text, suffix
    return text, None


def display_name(raw: str | None) -> str:
    name, _ = split_athlete_label(raw)
    return name


def display_team(raw: str | None, fallback: str | None = None) -> str | None:
    _, team = split_athlete_label(raw)
    return team or fallback


def team_meta_map() -> dict[str, dict]:
    settings = load_settings()
    return {t["code"]: t for t in settings.get("teams", [])}
