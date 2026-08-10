"""Strava OAuth and activity fetching."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta

import requests

from services.storage import get_users, load_settings, save_users

ACTIVITIES_URL = "https://www.strava.com/api/v3/athlete/activities"

# Free PythonAnywhere accounts must use this allowlist proxy for outbound HTTPS.
# Paid accounts have unrestricted internet and normally do not need it.
_PA_FREE_PROXY = {
    "http": "http://proxy.server:3128",
    "https": "http://proxy.server:3128",
}


def _strava_cfg() -> dict:
    return load_settings()["strava"]


def _request_proxies() -> dict | None:
    """
    Ensure outbound Strava calls work on free PythonAnywhere.

    Free accounts get Errno 101 (Network is unreachable) if requests bypasses
    proxy.server:3128. Web workers sometimes miss http_proxy env vars, so we
    force the proxy when we detect PythonAnywhere and nothing is configured.
    """
    if any(os.environ.get(k) for k in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY")):
        return None

    flag = (os.environ.get("STRAVA_PA_PROXY") or "").strip().lower()
    if flag in {"0", "false", "no", "off"}:
        return None
    if flag in {"1", "true", "yes", "on"}:
        return _PA_FREE_PROXY

    try:
        cfg_flag = load_settings().get("pythonanywhere_free_proxy")
    except Exception:
        cfg_flag = None
    if cfg_flag is False:
        return None
    if cfg_flag is True:
        return _PA_FREE_PROXY

    home = os.environ.get("HOME", "")
    on_pa = home.startswith("/home/") and (
        os.path.isdir("/var/www")
        or any(k.upper().startswith("PYTHONANYWHERE") for k in os.environ)
    )
    return _PA_FREE_PROXY if on_pa else None


def _http(method: str, url: str, **kwargs) -> requests.Response:
    proxies = _request_proxies()
    if proxies is not None:
        kwargs.setdefault("proxies", proxies)
    return requests.request(method, url, **kwargs)


def authorize_url() -> str:
    cfg = _strava_cfg()
    return (
        f"{cfg['authorize_url']}?client_id={cfg['client_id']}"
        f"&response_type=code&redirect_uri={cfg['redirect_uri']}"
        f"&scope=read,activity:read_all&approval_prompt=force"
    )


def exchange_code(code: str) -> dict:
    cfg = _strava_cfg()
    response = _http(
        "POST",
        cfg["token_url"],
        data={
            "client_id": cfg["client_id"],
            "client_secret": cfg["client_secret"],
            "code": code,
            "grant_type": "authorization_code",
        },
        timeout=30,
    )
    return response.json()


def _format_strava_error(response: requests.Response) -> str:
    try:
        payload = response.json()
    except Exception:
        payload = None

    if isinstance(payload, dict):
        message = payload.get("message") or "Strava error"
        errors = payload.get("errors") or []
        details = []
        for err in errors:
            if isinstance(err, dict):
                details.append(
                    f"{err.get('resource', '?')}.{err.get('field', '?')}={err.get('code', '?')}"
                )
            else:
                details.append(str(err))
        detail_text = "; ".join(details)
        if detail_text:
            return f"HTTP {response.status_code} — {message} ({detail_text})"
        return f"HTTP {response.status_code} — {message}"

    body = (response.text or "").strip()
    if len(body) > 300:
        body = body[:300] + "…"
    return f"HTTP {response.status_code} — {body or 'Unknown Strava error'}"


def refresh_access_token(user: dict, persist: bool = True, force: bool = False) -> str | None:
    expired = time.time() > float(user.get("expires_at") or 0)
    if not force and not expired:
        return user.get("access_token")

    cfg = _strava_cfg()
    response = _http(
        "POST",
        cfg["token_url"],
        data={
            "client_id": cfg["client_id"],
            "client_secret": cfg["client_secret"],
            "grant_type": "refresh_token",
            "refresh_token": user.get("refresh_token"),
        },
        timeout=30,
    )
    if response.status_code != 200:
        user["last_token_error"] = _format_strava_error(response)
        if persist:
            users = get_users()
            for idx, existing in enumerate(users):
                if existing.get("athlete_id") == user.get("athlete_id"):
                    users[idx] = user
                    break
            save_users(users)
        return None

    token_data = response.json()
    user["access_token"] = token_data["access_token"]
    user["refresh_token"] = token_data.get("refresh_token", user.get("refresh_token"))
    user["expires_at"] = token_data["expires_at"]
    user.pop("last_token_error", None)

    if persist:
        users = get_users()
        for idx, existing in enumerate(users):
            if existing.get("athlete_id") == user.get("athlete_id"):
                users[idx] = user
                break
        save_users(users)

    return user["access_token"]


def deauthorize(access_token: str) -> bool:
    cfg = _strava_cfg()
    response = _http(
        "POST",
        cfg["deauthorize_url"],
        params={"access_token": access_token},
        timeout=30,
    )
    return response.status_code == 200


def get_all_activities(
    access_token: str,
    start_date: str | None = None,
    end_date: str | None = None,
) -> list | str:
    """
    List authenticated athlete activities.

    Current Strava API (still valid):
      GET https://www.strava.com/api/v3/athlete/activities
      Auth: Bearer access_token
      Query: after, before (unix seconds), page, per_page (max 200)
      Scope: activity:read or activity:read_all (for private)
    """
    settings = load_settings()
    start_date = start_date or settings["start_date"]
    end_date = end_date or settings["end_date"]
    cfg = settings["strava"]
    url = cfg.get("activities_url") or ACTIVITIES_URL

    after_ts = int(time.mktime(datetime.strptime(start_date, "%Y-%m-%d").timetuple()))
    # Include the full end day (before is exclusive-ish by start_date filter)
    before_ts = int(
        time.mktime((datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)).timetuple())
    )

    activities: list = []
    page = 1
    per_page = 200  # Strava max

    while True:
        response = _http(
            "GET",
            url,
            headers={"Authorization": f"Bearer {access_token}"},
            params={
                "page": page,
                "per_page": per_page,
                "after": after_ts,
                "before": before_ts,
            },
            timeout=60,
        )
        if response.status_code == 401:
            return (
                "Unauthorized (401). Access token rejected. "
                f"{_format_strava_error(response)}"
            )
        if response.status_code != 200:
            return f"Failed to fetch activities: {_format_strava_error(response)}"

        page_activities = response.json()
        if not isinstance(page_activities, list):
            return f"Unexpected Strava response: {json.dumps(page_activities)[:300]}"

        if not page_activities:
            break

        activities.extend(page_activities)
        if len(page_activities) < per_page:
            break
        page += 1
        if page > 100:
            break

    return activities


def refresh_all_tokens(force: bool = True) -> tuple[int, int, list[dict]]:
    users = get_users()
    ok = 0
    failed = 0
    details = []
    for user in users:
        token = refresh_access_token(user, persist=False, force=force)
        if token:
            ok += 1
            details.append({"name": user.get("name"), "ok": True})
        else:
            failed += 1
            details.append(
                {
                    "name": user.get("name"),
                    "ok": False,
                    "error": user.get("last_token_error", "Refresh failed"),
                }
            )
    save_users(users)
    return ok, failed, details
