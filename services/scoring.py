"""Activity processing, points, team scores, and superstepper logic."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd

from services.storage import (
    ACTIVITIES_XLSX,
    BACKUP_DIR,
    INDIVIDUAL_POINTS_XLSX,
    POINTS_XLSX,
    SUPERSTEPPERS_FILE,
    TEAM_SCORES_FILE,
    TOP_FEMALE_XLSX,
    TOP_MALE_XLSX,
    TOP_PER_TEAM_FILE,
    extract_team,
    get_users,
    load_settings,
    write_json,
)


SCORE_OUTPUT_FILES = [
    POINTS_XLSX,
    INDIVIDUAL_POINTS_XLSX,
    TOP_MALE_XLSX,
    TOP_FEMALE_XLSX,
    TEAM_SCORES_FILE,
    TOP_PER_TEAM_FILE,
    SUPERSTEPPERS_FILE,
]


def _delete_file(path: Path) -> bool:
    if path.exists() and path.is_file():
        path.unlink()
        return True
    return False


def clear_score_outputs() -> dict:
    """Clear computed score Excel/JSON files (keep activity workbook)."""
    removed = []
    for path in SCORE_OUTPUT_FILES:
        if _delete_file(path):
            removed.append(path.name)

    # Reset score JSON + empty Excel shells
    write_json(TEAM_SCORES_FILE, {})
    write_json(TOP_PER_TEAM_FILE, {})
    write_json(
        SUPERSTEPPERS_FILE,
        {"supersteppers": [], "details": [], "date_calculated": None},
    )
    _empty_points_df().to_excel(POINTS_XLSX, index=False)
    empty_ind = _empty_individual_df()
    empty_ind.to_excel(INDIVIDUAL_POINTS_XLSX, index=False)
    empty_ind.to_excel(TOP_MALE_XLSX, index=False)
    empty_ind.to_excel(TOP_FEMALE_XLSX, index=False)
    return {"removed": removed, "kept_activities": ACTIVITIES_XLSX.exists()}


def clear_activity_workbook() -> dict:
    """Reset the combined activities Excel to an empty header-only workbook."""
    removed = _delete_file(ACTIVITIES_XLSX)
    empty = pd.DataFrame(
        columns=["Athlete Name", "Athlete ID", "Date", "Total Distance", "Activities"]
    )
    ACTIVITIES_XLSX.parent.mkdir(parents=True, exist_ok=True)
    empty.to_excel(ACTIVITIES_XLSX, index=False)
    return {"removed_activities": removed, "reset_to_empty": True}


def clear_all_challenge_data(clear_backups: bool = False) -> dict:
    """
    Wipe activity workbook + all computed outputs.
    Optionally clear per-athlete JSON backups too.
    """
    activity = clear_activity_workbook()
    scores = clear_score_outputs()
    backups_removed = 0
    if clear_backups and BACKUP_DIR.exists():
        for path in BACKUP_DIR.glob("*.json"):
            path.unlink(missing_ok=True)
            backups_removed += 1
    return {
        **activity,
        "score_files_cleared": scores["removed"],
        "backups_removed": backups_removed,
    }


def _pace(entry: dict) -> float | None:
    try:
        moving = entry.get("moving_time") or 0
        distance = entry.get("distance") or 0
        if distance <= 0:
            return None
        return (moving / 60) / (distance / 1000)
    except Exception:
        return None


def process_athlete_activities(
    json_data: list,
    athlete_name: str,
    athlete_id: int | None = None,
    output_file: Path | None = None,
) -> pd.DataFrame:
    """Aggregate qualifying activities by day and merge into the combined Excel."""
    from services.storage import canonical_athlete_name, get_users

    settings = load_settings()
    min_pace = float(settings["scoring"].get("min_pace_min_per_km", 4.5))
    output_file = Path(output_file or ACTIVITIES_XLSX)

    # Always store Name_TEAM when team is known
    users = get_users()
    if athlete_id is not None:
        user = next((u for u in users if u.get("athlete_id") == athlete_id), None)
        if user:
            athlete_name = canonical_athlete_name(user.get("name", athlete_name), user.get("team"))
        else:
            athlete_name = canonical_athlete_name(athlete_name)
    else:
        athlete_name = canonical_athlete_name(athlete_name)

    try:
        existing_df = pd.read_excel(output_file)
    except FileNotFoundError:
        existing_df = pd.DataFrame()

    combined: dict[str, dict] = {}

    for entry in json_data:
        pace = _pace(entry)
        if pace is None or pace <= min_pace:
            continue

        date_raw = entry.get("start_date_local") or entry.get("start_date") or ""
        date_str = date_raw.split("T")[0]
        if not date_str:
            continue

        aid = athlete_id or entry.get("athlete", {}).get("id")
        distance_km = (entry.get("distance") or 0) / 1000

        if date_str not in combined:
            combined[date_str] = {
                "Athlete Name": athlete_name,
                "Athlete ID": aid,
                "Date": date_str,
                "Total Distance": 0.0,
                "Activities": [],
            }

        combined[date_str]["Total Distance"] += distance_km
        combined[date_str]["Activities"].append(
            {
                "Activity Name": entry.get("name"),
                "Distance": entry.get("distance"),
                "Pace": pace,
            }
        )

    rows = []
    for date_str, data in combined.items():
        rows.append(
            {
                "Athlete Name": data["Athlete Name"],
                "Athlete ID": data["Athlete ID"],
                "Date": data["Date"],
                "Total Distance": round(data["Total Distance"], 3),
                "Activities": "; ".join(
                    f"{a['Activity Name']} ({a['Distance']}m, Pace: {a['Pace']:.2f})"
                    for a in data["Activities"]
                ),
            }
        )

    new_df = pd.DataFrame(rows)
    if not existing_df.empty and not new_df.empty:
        combined_df = pd.concat([existing_df, new_df], ignore_index=True)
        combined_df.drop_duplicates(subset=["Athlete ID", "Date"], keep="last", inplace=True)
    elif not existing_df.empty:
        combined_df = existing_df
    else:
        combined_df = new_df

    output_file.parent.mkdir(parents=True, exist_ok=True)
    combined_df.to_excel(output_file, index=False)

    # Per-athlete JSON backup
    safe_name = "".join(c if c.isalnum() or c in (" ", "_", "-") else "_" for c in athlete_name)
    backup_path = BACKUP_DIR / f"{safe_name}.json"
    write_json(backup_path, json_data)

    return combined_df


def _empty_points_df() -> pd.DataFrame:
    return pd.DataFrame(columns=["Athlete Name", "Athlete ID", "Total points"])


def _empty_individual_df() -> pd.DataFrame:
    return pd.DataFrame(columns=["Athlete Name", "Athlete ID", "gender", "Total points"])


def _read_activities_df(path: Path) -> pd.DataFrame:
    """Load activities workbook; tolerate missing/empty seed files."""
    try:
        df = pd.read_excel(path)
    except FileNotFoundError:
        return pd.DataFrame(
            columns=["Athlete Name", "Athlete ID", "Date", "Total Distance", "Activities"]
        )
    if df is None or df.empty:
        return pd.DataFrame(
            columns=["Athlete Name", "Athlete ID", "Date", "Total Distance", "Activities"]
        )
    return df


def compute_athlete_points(input_file: Path | None = None, output_file: Path | None = None) -> bool:
    settings = load_settings()
    max_km = float(settings["scoring"].get("max_km_per_day", 15))
    input_file = Path(input_file or ACTIVITIES_XLSX)
    output_file = Path(output_file or POINTS_XLSX)

    df = _read_activities_df(input_file)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    if df.empty or "Total Distance" not in df.columns:
        _empty_points_df().to_excel(output_file, index=False)
        return True

    df["Points"] = df["Total Distance"].apply(lambda x: min(float(x), max_km))
    total = (
        df.groupby(["Athlete Name", "Athlete ID"])["Points"]
        .sum()
        .reset_index()
        .rename(columns={"Points": "Total points"})
    )
    total["Total points"] = total["Total points"].round(2)
    total.to_excel(output_file, index=False)
    return True


def resolve_team(athlete_name: str, athlete_id: int | None, users: list[dict], teams: list[str]) -> str | None:
    if athlete_id is not None:
        user = next((u for u in users if u.get("athlete_id") == athlete_id), None)
        if user and user.get("team") in teams:
            return user["team"]
    return extract_team(athlete_name, teams)


def calculate_team_scores() -> dict:
    settings = load_settings()
    teams = [t["code"] for t in settings.get("teams", [])]
    top_n = int(settings["scoring"].get("top_athletes_per_team", 15))
    users = get_users()

    compute_athlete_points()
    try:
        df = pd.read_excel(POINTS_XLSX)
    except FileNotFoundError:
        df = _empty_points_df()

    team_scores: dict[str, float] = {t: 0.0 for t in teams}
    top_per_team: dict[str, list] = {t: [] for t in teams}

    if not df.empty and "Total points" in df.columns:
        df["Team"] = df.apply(
            lambda r: resolve_team(
                r["Athlete Name"],
                int(r["Athlete ID"]) if pd.notna(r["Athlete ID"]) else None,
                users,
                teams,
            ),
            axis=1,
        )
        for team in teams:
            team_df = df[df["Team"] == team]
            top = team_df.nlargest(top_n, "Total points")[["Athlete Name", "Athlete ID", "Total points"]]
            team_scores[team] = round(float(top["Total points"].sum()), 2)
            top_per_team[team] = [
                {
                    "Athlete Name": row["Athlete Name"],
                    "Athlete ID": int(row["Athlete ID"]) if pd.notna(row["Athlete ID"]) else None,
                    "Total points": round(float(row["Total points"]), 2),
                }
                for _, row in top.iterrows()
            ]

    sorted_scores = dict(sorted(team_scores.items(), key=lambda item: item[1], reverse=True))
    write_json(TEAM_SCORES_FILE, sorted_scores)
    write_json(TOP_PER_TEAM_FILE, top_per_team)
    return {"scores": sorted_scores, "top_per_team": top_per_team}


def compute_individual_leaders() -> dict:
    settings = load_settings()
    max_km = float(settings["scoring"].get("individual_max_km_per_day", 60))
    users = get_users()
    gender_map = {u["athlete_id"]: u.get("gender") for u in users}
    name_gender = {u["name"]: u.get("gender") for u in users}

    df = _read_activities_df(ACTIVITIES_XLSX)
    if df.empty or "Total Distance" not in df.columns:
        empty = _empty_individual_df()
        empty.to_excel(INDIVIDUAL_POINTS_XLSX, index=False)
        empty.to_excel(TOP_MALE_XLSX, index=False)
        empty.to_excel(TOP_FEMALE_XLSX, index=False)
        return {"male": [], "female": []}

    df["Total Distance"] = df["Total Distance"].apply(lambda x: min(float(x), max_km))
    df["Points"] = df["Total Distance"]
    df["gender"] = df.apply(
        lambda r: gender_map.get(int(r["Athlete ID"])) if pd.notna(r["Athlete ID"]) else name_gender.get(r["Athlete Name"]),
        axis=1,
    )

    total = (
        df.groupby(["Athlete Name", "Athlete ID", "gender"])["Points"]
        .sum()
        .reset_index()
        .rename(columns={"Points": "Total points"})
    )
    total["Total points"] = total["Total points"].round(2)
    total.to_excel(INDIVIDUAL_POINTS_XLSX, index=False)

    top_male = total[total["gender"] == "M"].nlargest(10, "Total points")
    top_female = total[total["gender"] == "F"].nlargest(10, "Total points")
    top_male.to_excel(TOP_MALE_XLSX, index=False)
    top_female.to_excel(TOP_FEMALE_XLSX, index=False)

    return {
        "male": top_male.to_dict("records"),
        "female": top_female.to_dict("records"),
    }


def calculate_supersteppers() -> dict:
    settings = load_settings()
    ss = settings["superstepper"]
    min_km = float(ss.get("min_km_per_day", 3))
    required_days = int(ss.get("required_days", 30))
    require_exact = bool(ss.get("require_exact_days", False))

    from services.storage import canonical_athlete_name, team_codes

    df = _read_activities_df(ACTIVITIES_XLSX)
    users = get_users()
    teams = team_codes()

    # Prefer athlete_id matching so renamed users still resolve correctly
    winners = []
    details = []

    if users:
        iterable = users
    else:
        iterable = [
            {"name": name, "athlete_id": None, "team": extract_team(str(name), teams)}
            for name in sorted(df["Athlete Name"].dropna().unique().tolist())
        ]

    for user in iterable:
        athlete_id = user.get("athlete_id")
        display_name = canonical_athlete_name(user.get("name", ""), user.get("team"), teams)

        if athlete_id is not None and "Athlete ID" in df.columns:
            athlete_df = df[df["Athlete ID"] == athlete_id]
            # Fallback: also include rows under old/new name labels
            if athlete_df.empty:
                athlete_df = df[df["Athlete Name"] == user.get("name")]
            if athlete_df.empty:
                athlete_df = df[df["Athlete Name"] == display_name]
        else:
            athlete_df = df[
                (df["Athlete Name"] == display_name) | (df["Athlete Name"] == user.get("name"))
            ]

        day_count = len(athlete_df)
        qualifying_days = int((athlete_df["Total Distance"] >= min_km).sum()) if day_count else 0
        total_km = round(float(athlete_df["Total Distance"].sum()), 2) if day_count else 0

        if require_exact:
            ok = day_count == required_days and qualifying_days == required_days
        else:
            ok = qualifying_days >= required_days

        details.append(
            {
                "name": display_name,
                "athlete_id": athlete_id,
                "team": user.get("team") or extract_team(display_name, teams),
                "days_logged": day_count,
                "qualifying_days": qualifying_days,
                "total_km": total_km,
                "is_superstepper": ok,
            }
        )
        if ok:
            winners.append(display_name)

    result = {
        "supersteppers": winners,
        "details": details,
        "date_calculated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "rules": {
            "min_km_per_day": min_km,
            "required_days": required_days,
            "require_exact_days": require_exact,
        },
    }
    write_json(SUPERSTEPPERS_FILE, result)
    return result


def team_member_counts() -> dict[str, int]:
    settings = load_settings()
    teams = [t["code"] for t in settings.get("teams", [])]
    users = get_users()
    counts = {t: 0 for t in teams}
    for user in users:
        team = user.get("team") or extract_team(user.get("name", ""), teams)
        if team in counts:
            counts[team] += 1
    return dict(sorted(counts.items(), key=lambda item: item[1], reverse=True))
