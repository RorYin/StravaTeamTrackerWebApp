"""Batch fetch orchestration for all authorized athletes."""
from __future__ import annotations

from services.activities import get_all_activities, refresh_access_token
from services.scoring import clear_all_challenge_data, process_athlete_activities
from services.storage import (
    get_progress,
    get_users,
    load_settings,
    reset_progress,
    save_progress,
    save_users,
)


def init_or_sync_progress() -> list[dict]:
    users = get_users()
    progress = get_progress()
    known_ids = {p.get("athlete_id") for p in progress}
    changed = False

    for user in users:
        if user["athlete_id"] not in known_ids:
            progress.append(
                {
                    "athlete_id": user["athlete_id"],
                    "athlete_name": user["name"],
                    "status": "pending",
                }
            )
            changed = True

    user_ids = {u["athlete_id"] for u in users}
    filtered = [p for p in progress if p.get("athlete_id") in user_ids]
    if len(filtered) != len(progress):
        progress = filtered
        changed = True

    if changed:
        save_progress(progress)
    return progress


def _summary_from(progress: list[dict]) -> dict:
    summary = {"pending": 0, "completed": 0, "failed": 0, "fetching": 0, "total": len(progress)}
    for entry in progress:
        status = entry.get("status", "pending")
        if status in summary:
            summary[status] += 1
        else:
            summary["pending"] += 1
    return summary


def progress_summary() -> dict:
    progress = init_or_sync_progress()
    return {"summary": _summary_from(progress), "progress": progress}


def prepare_fetch_all(
    refetch_completed: bool = False,
    data_mode: str = "append",
    clear_backups: bool = False,
) -> dict:
    """
    Mark athletes pending for a sequential fetch.

    data_mode:
      - append: keep existing Excel rows (weekly incremental)
      - replace: wipe activity Excel + score outputs, then fetch fresh (full period)
        Also forces every athlete back to pending.
    """
    mode = (data_mode or "append").strip().lower()
    if mode not in {"append", "replace"}:
        mode = "append"

    cleared = None
    if mode == "replace":
        cleared = clear_all_challenge_data(clear_backups=clear_backups)
        refetch_completed = True

    progress = init_or_sync_progress()
    for entry in progress:
        status = entry.get("status")
        should_reset = refetch_completed or status in ("failed", "fetching")
        if should_reset:
            entry["status"] = "pending"
            entry.pop("error", None)
            if refetch_completed:
                entry.pop("activity_count", None)
    save_progress(progress)
    summary = progress_summary()
    summary["data_mode"] = mode
    summary["cleared"] = cleared
    return summary


def fetch_one(athlete_id: int | None = None, start_date: str | None = None, end_date: str | None = None) -> dict:
    users = get_users()
    progress = init_or_sync_progress()
    settings = load_settings()
    start_date = start_date or settings["start_date"]
    end_date = end_date or settings["end_date"]

    if athlete_id is not None:
        target = next((p for p in progress if p.get("athlete_id") == athlete_id), None)
    else:
        pending = [p for p in progress if p.get("status") == "pending"]
        target = pending[0] if pending else None

    if not target:
        return {
            "ok": False,
            "message": "No pending athletes to fetch.",
            "done": True,
            "summary": _summary_from(progress),
            "progress": progress,
        }

    user = next((u for u in users if u.get("athlete_id") == target["athlete_id"]), None)
    if not user:
        target["status"] = "failed"
        target["error"] = "User not found"
        save_progress(progress)
        return {
            "ok": False,
            "message": f"User not found for {target.get('athlete_name')}",
            "done": False,
            "summary": _summary_from(progress),
            "progress": progress,
        }

    target["status"] = "fetching"
    save_progress(progress)

    try:
        token = refresh_access_token(user, persist=True)
        if not token:
            err = user.get("last_token_error") or "Could not refresh access token"
            raise RuntimeError(f"Token refresh failed: {err}")

        activities = get_all_activities(token, start_date, end_date)
        if isinstance(activities, str):
            # Retry once with a forced token refresh on auth-ish failures
            if "401" in activities or "Unauthorized" in activities:
                token = refresh_access_token(user, persist=True, force=True)
                if not token:
                    raise RuntimeError(
                        f"Token refresh failed after 401: {user.get('last_token_error', 'unknown')}"
                    )
                activities = get_all_activities(token, start_date, end_date)
            if isinstance(activities, str):
                raise RuntimeError(activities)

        process_athlete_activities(activities, user["name"], user.get("athlete_id"))
        save_users(users)
        target["status"] = "completed"
        target["activity_count"] = len(activities)
        target.pop("error", None)
        save_progress(progress)

        summary = _summary_from(progress)
        return {
            "ok": True,
            "message": f"Fetched {user['name']} ({summary['completed']}/{summary['total']}) — {start_date} to {end_date}",
            "athlete": user["name"],
            "athlete_id": user.get("athlete_id"),
            "activity_count": len(activities),
            "done": summary["pending"] == 0 and summary["fetching"] == 0,
            "summary": summary,
            "progress": progress,
            "level": "ok",
        }
    except Exception as exc:
        target["status"] = "failed"
        target["error"] = str(exc)
        save_progress(progress)
        summary = _summary_from(progress)
        return {
            "ok": False,
            "message": f"Error processing {user['name']}: {exc}",
            "error": str(exc),
            "athlete": user["name"],
            "athlete_id": user.get("athlete_id"),
            "done": summary["pending"] == 0 and summary["fetching"] == 0,
            "summary": summary,
            "progress": progress,
            "level": "error",
        }


def fetch_all(start_date: str | None = None, end_date: str | None = None, max_users: int | None = None) -> dict:
    """Server-side sequential fetch of every pending athlete (one by one)."""
    progress = init_or_sync_progress()
    pending_ids = [p["athlete_id"] for p in progress if p.get("status") == "pending"]
    if max_users is not None:
        pending_ids = pending_ids[:max_users]

    results = []
    for aid in pending_ids:
        results.append(fetch_one(aid, start_date, end_date))

    summary = progress_summary()["summary"]
    return {"results": results, "summary": summary, "done": True}


def restart_fetch() -> dict:
    reset_progress()
    return progress_summary()
