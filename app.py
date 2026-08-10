"""StepUp Challenge — modern Flask webapp."""
from __future__ import annotations

import os
from datetime import datetime
from functools import wraps

from flask import (
    Flask,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)

from services import activities as strava
from services import fetcher, scoring
from services.display import display_name, display_team, split_athlete_label
from services.storage import (
    ACTIVITIES_XLSX,
    POINTS_XLSX,
    ROOT,
    SUPERSTEPPERS_FILE,
    TEAM_SCORES_FILE,
    TOP_FEMALE_XLSX,
    TOP_MALE_XLSX,
    TOP_PER_TEAM_FILE,
    add_query,
    ensure_dirs,
    extract_team,
    get_queries,
    get_users,
    load_settings,
    profile_is_complete,
    read_json,
    register_authorization,
    remove_user,
    save_settings,
    team_codes,
    team_logo_path,
    update_user_profile,
    update_user_team,
    with_team_logos,
    write_json,
)

os.environ["http_proxy"] = ""
os.environ["https_proxy"] = ""

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "stepup-cursor-secret-change-me")

TEAM_LOGO_DIR = ROOT / "static" / "uploads" / "teams"
LAST_COMPUTE_FILE = ROOT / "data" / "last_compute.json"

ensure_dirs()
TEAM_LOGO_DIR.mkdir(parents=True, exist_ok=True)


@app.before_request
def force_incomplete_profile():
    """Users must finish team/gender selection before using the rest of the app."""
    if request.endpoint in {
        None,
        "static",
        "auth",
        "authorized",
        "complete_profile",
        "admin_login",
        "admin_logout",
    }:
        return None
    if request.endpoint and request.endpoint.startswith("admin_"):
        return None
    if not session.get("needs_profile") and not session.get("athlete_id"):
        return None

    athlete_id = session.get("athlete_id")
    if not athlete_id:
        return None
    user = next((u for u in get_users() if u.get("athlete_id") == athlete_id), None)
    if user and not profile_is_complete(user):
        session["needs_profile"] = True
        if request.endpoint != "complete_profile":
            flash("Please complete your team and gender to continue.", "warn")
            return redirect(url_for("complete_profile"))
    return None


@app.template_filter("athlete_name")
def athlete_name_filter(value):
    return display_name(value)


@app.template_filter("athlete_team")
def athlete_team_filter(value):
    return display_team(value)


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("is_admin"):
            wants_json = (
                request.path.startswith("/admin/fetch/api/")
                or request.accept_mimetypes.best == "application/json"
                or request.is_json
                or request.headers.get("X-Requested-With") == "XMLHttpRequest"
            )
            if wants_json:
                return jsonify(
                    {
                        "ok": False,
                        "error": "admin_login_required",
                        "message": "Please sign in as admin.",
                    }
                ), 401
            flash("Please sign in as admin.", "warn")
            return redirect(url_for("admin_login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


@app.context_processor
def inject_globals():
    settings = load_settings()
    teams = with_team_logos(settings.get("teams", []))
    return {
        "app_name": settings.get("app_name", "StepUp Challenge"),
        "event_year": settings.get("event_year"),
        "teams": teams,
        "team_meta": {t["code"]: t for t in teams},
        "is_admin": bool(session.get("is_admin")),
        "settings": settings,
        "split_athlete": split_athlete_label,
        "app_logo": "images/logo.png",
        "team_logo_path": team_logo_path,
    }


# ── Public pages ─────────────────────────────────────────────────────────────

@app.route("/")
def index():
    settings = load_settings()
    scores = read_json(TEAM_SCORES_FILE, {}) or {}
    supersteppers = read_json(SUPERSTEPPERS_FILE, {"supersteppers": []}) or {"supersteppers": []}
    counts = scoring.team_member_counts()
    return render_template(
        "index.html",
        scores=scores,
        superstepper_count=len(supersteppers.get("supersteppers", [])),
        user_count=len(get_users()),
        team_counts=counts,
        challenge_dates=f"{settings['start_date']} → {settings['end_date']}",
    )


@app.route("/auth")
def auth():
    return redirect(strava.authorize_url())


@app.route("/authorized")
def authorized():
    code = request.args.get("code")
    scope = request.args.get("scope", "")
    if not code:
        flash("Authorization code missing.", "error")
        return redirect(url_for("index"))

    if "activity:read_all" not in scope and scope.strip() == "read":
        flash("Please authorize again and enable 'View data about your private activities'.", "error")
        return redirect(url_for("index"))

    token_response = strava.exchange_code(code)
    if "access_token" not in token_response:
        flash(token_response.get("message", "Authorization failed."), "error")
        return redirect(url_for("index"))

    athlete = token_response["athlete"]
    full_name = f"{athlete.get('firstname', '')} {athlete.get('lastname', '')}".strip()
    teams = team_codes()
    team = extract_team(full_name, teams)

    entry = {
        "name": full_name,
        "gender": athlete.get("sex"),
        "refresh_token": token_response["refresh_token"],
        "expires_at": token_response["expires_at"],
        "athlete_id": athlete["id"],
        "access_token": token_response["access_token"],
        "team": team,
        "authorized_at": datetime.now().isoformat(timespec="seconds"),
    }

    status, user, message = register_authorization(entry, max_auths=2)

    if status == "blocked":
        flash(message, "warn")
        return redirect(url_for("query_page"))

    session["access_token"] = entry["access_token"]
    session["athlete_id"] = user["athlete_id"]
    session["name"] = user["name"]

    if not profile_is_complete(user):
        session["needs_profile"] = True
        flash(
            f"Authorized as {display_name(user['name'])}. Please complete your team and gender.",
            "success",
        )
        return redirect(url_for("complete_profile"))

    session.pop("needs_profile", None)
    flash(
        f"Welcome back {display_name(user['name'])}. Authorization saved "
        f"({user.get('auth_count', 1)}/2).",
        "success",
    )
    return redirect(url_for("index"))


@app.route("/complete-profile", methods=["GET", "POST"])
def complete_profile():
    athlete_id = session.get("athlete_id")
    if not athlete_id:
        flash("Please authorize with Strava first.", "warn")
        return redirect(url_for("index"))

    users = get_users()
    user = next((u for u in users if u.get("athlete_id") == athlete_id), None)
    if not user:
        flash("Athlete record not found. Please authorize again.", "error")
        return redirect(url_for("auth"))

    team_list = with_team_logos()

    if request.method == "POST":
        team = (request.form.get("team") or "").strip().upper()
        gender = (request.form.get("gender") or "").strip().upper()
        if not team:
            flash("Please select your team.", "error")
        elif not gender:
            flash("Please select Male or Female.", "error")
        else:
            try:
                updated = update_user_profile(athlete_id, team=team, gender=gender)
                if not updated:
                    flash("Could not update profile.", "error")
                else:
                    session["name"] = updated["name"]
                    session.pop("needs_profile", None)
                    flash(
                        f"Profile saved — {display_name(updated['name'])} · Team {team} · {gender}.",
                        "success",
                    )
                    return redirect(url_for("index"))
            except ValueError as exc:
                flash(str(exc), "error")

    return render_template(
        "complete_profile.html",
        user=user,
        team_list=team_list,
        display_user_name=display_name(user.get("name")),
    )


@app.route("/scores")
def team_scores():
    settings = load_settings()
    scores = read_json(TEAM_SCORES_FILE, {}) or {}
    top = read_json(TOP_PER_TEAM_FILE, {}) or {}
    teams = with_team_logos(settings.get("teams", []))
    team_meta = {t["code"]: t for t in teams}
    max_points = max(scores.values()) if scores else 0
    return render_template(
        "scores.html",
        scores=scores,
        top_per_team=top,
        team_meta=team_meta,
        max_points=max_points,
        top_n=settings["scoring"].get("top_athletes_per_team", 15),
    )


@app.route("/supersteppers")
def supersteppers_page():
    data = read_json(SUPERSTEPPERS_FILE, {"supersteppers": [], "details": []}) or {}
    settings = load_settings()
    return render_template(
        "supersteppers.html",
        data=data,
        rules=settings.get("superstepper", {}),
    )


@app.route("/leaders")
def leaders():
    male = female = []
    try:
        import pandas as pd

        if TOP_MALE_XLSX.exists():
            male = pd.read_excel(TOP_MALE_XLSX).to_dict("records")
        if TOP_FEMALE_XLSX.exists():
            female = pd.read_excel(TOP_FEMALE_XLSX).to_dict("records")
    except Exception:
        pass
    return render_template("leaders.html", male=male, female=female)


@app.route("/query", methods=["GET", "POST"])
def query_page():
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        email = (request.form.get("email") or "").strip()
        athlete_id = (request.form.get("athlete_id") or "").strip()
        message = (request.form.get("message") or "").strip()
        if not name or not message:
            flash("Name and message are required.", "error")
        else:
            add_query(
                {
                    "id": datetime.now().strftime("%Y%m%d%H%M%S%f"),
                    "name": name,
                    "email": email,
                    "athlete_id": athlete_id,
                    "message": message,
                    "created_at": datetime.now().isoformat(timespec="seconds"),
                    "status": "open",
                }
            )
            flash("Query submitted. Admin will review it soon.", "success")
            return redirect(url_for("query_page"))
    return render_template("query.html")


@app.route("/teams")
def teams_page():
    counts = scoring.team_member_counts()
    team_list = with_team_logos()
    selected = (request.args.get("team") or "").strip().upper()
    valid_codes = {t["code"] for t in team_list}
    if selected and selected not in valid_codes:
        selected = ""

    users = get_users()
    members = []
    for user in users:
        code = (user.get("team") or extract_team(user.get("name", "")) or "").upper()
        if selected and code != selected:
            continue
        if not selected:
            continue
        members.append(
            {
                "name": user.get("name", ""),
                "team": code,
                "gender": user.get("gender") or "—",
                "athlete_id": user.get("athlete_id"),
            }
        )
    members.sort(key=lambda m: display_name(m["name"]).lower())

    selected_meta = next((t for t in team_list if t["code"] == selected), None)
    return render_template(
        "teams.html",
        counts=counts,
        team_list=team_list,
        selected_team=selected,
        selected_meta=selected_meta,
        members=members,
    )


# ── Admin auth ───────────────────────────────────────────────────────────────

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if session.get("is_admin"):
        return redirect(url_for("admin_dashboard"))

    if request.method == "POST":
        password = request.form.get("password", "")
        settings = load_settings()
        expected = settings.get("admin", {}).get("password", "RorYin#1")
        if password == expected:
            session["is_admin"] = True
            flash("Admin access granted.", "success")
            nxt = request.args.get("next") or url_for("admin_dashboard")
            return redirect(nxt)
        flash("Incorrect password.", "error")

    return render_template("admin/login.html")


@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    flash("Logged out.", "success")
    return redirect(url_for("index"))


# ── Admin panel ──────────────────────────────────────────────────────────────

@app.route("/admin")
@admin_required
def admin_dashboard():
    progress = fetcher.progress_summary()
    queries = get_queries()
    open_queries = [q for q in queries if q.get("status") == "open"]
    last_compute = read_json(LAST_COMPUTE_FILE, {}) or {}
    return render_template(
        "admin/dashboard.html",
        user_count=len(get_users()),
        progress=progress,
        open_query_count=len(open_queries),
        scores=read_json(TEAM_SCORES_FILE, {}) or {},
        last_compute=last_compute,
    )


@app.route("/admin/users", methods=["GET", "POST"])
@admin_required
def admin_users():
    settings = load_settings()
    teams = team_codes(settings)

    if request.method == "POST":
        action = request.form.get("action")
        athlete_id = int(request.form.get("athlete_id"))
        if action == "set_team":
            team = request.form.get("team") or ""
            if team and team not in teams:
                flash("Invalid team.", "error")
            else:
                update_user_team(athlete_id, team)
                flash("Team updated.", "success")
        elif action == "remove":
            remove_user(athlete_id)
            flash("User removed locally.", "success")
        elif action == "deauth":
            users = get_users()
            user = next((u for u in users if u.get("athlete_id") == athlete_id), None)
            if user:
                token = strava.refresh_access_token(user)
                if token:
                    strava.deauthorize(token)
                remove_user(athlete_id)
                flash("User deauthorized and removed.", "success")
        return redirect(url_for("admin_users"))

    users = get_users()
    for u in users:
        if not u.get("team"):
            u["team"] = extract_team(u.get("name", ""), teams)
    return render_template("admin/users.html", users=users, teams=teams)


@app.route("/admin/config", methods=["GET", "POST"])
@admin_required
def admin_config():
    settings = load_settings()
    if request.method == "POST":
        settings["app_name"] = request.form.get("app_name", settings.get("app_name"))
        settings["event_year"] = int(request.form.get("event_year") or settings.get("event_year") or 2026)
        settings["start_date"] = request.form.get("start_date", settings["start_date"])
        settings["end_date"] = request.form.get("end_date", settings["end_date"])

        settings["scoring"]["max_km_per_day"] = float(request.form.get("max_km_per_day") or 15)
        settings["scoring"]["top_athletes_per_team"] = int(request.form.get("top_athletes_per_team") or 15)
        settings["scoring"]["min_pace_min_per_km"] = float(request.form.get("min_pace_min_per_km") or 4.5)
        settings["scoring"]["individual_max_km_per_day"] = float(
            request.form.get("individual_max_km_per_day") or 60
        )

        settings["superstepper"]["min_km_per_day"] = float(request.form.get("ss_min_km") or 3)
        settings["superstepper"]["required_days"] = int(request.form.get("ss_required_days") or 30)
        settings["superstepper"]["require_exact_days"] = request.form.get("ss_exact") == "on"

        settings["strava"]["client_id"] = request.form.get("client_id", settings["strava"]["client_id"])
        settings["strava"]["client_secret"] = request.form.get(
            "client_secret", settings["strava"]["client_secret"]
        )
        settings["strava"]["redirect_uri"] = request.form.get(
            "redirect_uri", settings["strava"]["redirect_uri"]
        )

        new_password = request.form.get("admin_password", "").strip()
        if new_password:
            settings["admin"]["password"] = new_password

        old_teams = {t["code"]: t for t in settings.get("teams", [])}
        teams = []
        for i in range(8):
            code = (request.form.get(f"team_code_{i}") or "").strip().upper()
            name = (request.form.get(f"team_name_{i}") or "").strip()
            color = (request.form.get(f"team_color_{i}") or "#64748b").strip()
            if not code:
                continue

            logo = old_teams.get(code, {}).get("logo")
            # Preserve logo when code renamed within same slot
            slot_old = settings.get("teams", [])
            if i < len(slot_old) and slot_old[i].get("logo") and not logo:
                logo = slot_old[i].get("logo")

            upload = request.files.get(f"team_logo_{i}")
            if upload and upload.filename:
                ext = os.path.splitext(upload.filename)[1].lower()
                if ext not in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"}:
                    flash(f"Unsupported logo type for {code}: {ext}", "error")
                else:
                    filename = f"{code.lower()}{ext}"
                    path = TEAM_LOGO_DIR / filename
                    upload.save(path)
                    logo = f"uploads/teams/{filename}"

            if request.form.get(f"team_logo_clear_{i}") == "on":
                logo = None

            teams.append(
                {
                    "code": code,
                    "name": name or code,
                    "color": color,
                    "logo": logo,
                }
            )

        if teams:
            settings["teams"] = teams

        save_settings(settings)
        flash("Configuration saved.", "success")
        return redirect(url_for("admin_config"))

    team_slots = list(settings.get("teams", []))
    while len(team_slots) < 8:
        team_slots.append({"code": "", "name": "", "color": "#64748b", "logo": None})

    return render_template("admin/config.html", cfg=settings, team_slots=team_slots)


@app.route("/admin/fetch", methods=["GET", "POST"])
@admin_required
def admin_fetch():
    settings = load_settings()
    message = None

    if request.method == "POST":
        action = request.form.get("action")
        start = request.form.get("start_date") or settings["start_date"]
        end = request.form.get("end_date") or settings["end_date"]

        if action == "reset":
            message = "Progress reset."
            data = fetcher.restart_fetch()
        elif action == "one":
            result = fetcher.fetch_one(start_date=start, end_date=end)
            message = result["message"]
            data = fetcher.progress_summary()
        elif action == "all":
            data_mode = request.form.get("data_mode") or "append"
            fetcher.prepare_fetch_all(refetch_completed=False, data_mode=data_mode)
            result = fetcher.fetch_all(start_date=start, end_date=end)
            message = (
                f"Fetched all users ({data_mode}). "
                f"Completed {result['summary']['completed']}/{result['summary']['total']} "
                f"(failed {result['summary']['failed']})."
            )
            data = fetcher.progress_summary()
        elif action == "all_refetch":
            data_mode = request.form.get("data_mode") or "replace"
            fetcher.prepare_fetch_all(refetch_completed=True, data_mode=data_mode)
            result = fetcher.fetch_all(start_date=start, end_date=end)
            message = (
                f"Re-fetched all users ({data_mode}). "
                f"Completed {result['summary']['completed']}/{result['summary']['total']} "
                f"(failed {result['summary']['failed']})."
            )
            data = fetcher.progress_summary()
        elif action == "athlete":
            athlete_id = int(request.form.get("athlete_id"))
            progress = fetcher.init_or_sync_progress()
            for p in progress:
                if p.get("athlete_id") == athlete_id:
                    p["status"] = "pending"
            from services.storage import save_progress

            save_progress(progress)
            result = fetcher.fetch_one(athlete_id, start, end)
            message = result["message"]
            data = fetcher.progress_summary()
        else:
            data = fetcher.progress_summary()

        if message:
            flash(message, "success" if "Error" not in message else "error")
    else:
        data = fetcher.progress_summary()

    return render_template(
        "admin/fetch.html",
        progress_data=data,
        start_date=settings["start_date"],
        end_date=settings["end_date"],
        users=get_users(),
        has_xlsx=ACTIVITIES_XLSX.exists(),
    )


@app.route("/admin/fetch/api/status")
@admin_required
def admin_fetch_status():
    return jsonify(fetcher.progress_summary())


@app.route("/admin/fetch/api/prepare", methods=["POST"])
@admin_required
def admin_fetch_prepare():
    payload = request.get_json(silent=True) or {}
    refetch = bool(payload.get("refetch_completed"))
    data_mode = (payload.get("data_mode") or "append").strip().lower()
    clear_backups = bool(payload.get("clear_backups"))
    result = fetcher.prepare_fetch_all(
        refetch_completed=refetch,
        data_mode=data_mode,
        clear_backups=clear_backups,
    )
    return jsonify(result)


@app.route("/admin/fetch/api/next", methods=["POST"])
@admin_required
def admin_fetch_next():
    """Fetch exactly one pending athlete; used by the live 'Fetch all' UI loop."""
    settings = load_settings()
    payload = request.get_json(silent=True) or {}
    start = payload.get("start_date") or settings["start_date"]
    end = payload.get("end_date") or settings["end_date"]
    athlete_id = payload.get("athlete_id")
    if athlete_id is not None:
        athlete_id = int(athlete_id)
    result = fetcher.fetch_one(athlete_id=athlete_id, start_date=start, end_date=end)
    return jsonify(result)


@app.route("/admin/compute", methods=["POST"])
@admin_required
def admin_compute():
    action = (request.form.get("action") or "").strip()
    data_mode = (request.form.get("data_mode") or "recompute").strip().lower()
    result_msg = None
    level = "success"

    try:
        if not action:
            raise ValueError("No compute action received from the form.")

        if action == "clear_scores":
            cleared = scoring.clear_score_outputs()
            result_msg = (
                "Cleared score outputs (team scores, leaders, SuperSteppers, points). "
                f"Activity Excel kept={cleared['kept_activities']}."
            )
            flash(result_msg, "success")
        elif action == "clear_all_data":
            cleared = scoring.clear_all_challenge_data(
                clear_backups=request.form.get("clear_backups") == "on"
            )
            result_msg = (
                "Cleared activity Excel + all score outputs"
                + (f" + {cleared['backups_removed']} backups." if cleared["backups_removed"] else ".")
            )
            flash(result_msg, "warn")
        elif action == "tokens":
            ok, failed, details = strava.refresh_all_tokens(force=True)
            failed_names = [d["name"] for d in details if not d.get("ok")][:5]
            extra = f" Examples: {', '.join(failed_names)}" if failed_names else ""
            result_msg = f"Tokens refreshed: {ok} ok, {failed} failed.{extra}"
            if failed and not ok:
                level = "error"
            elif failed:
                level = "warn"
            flash(result_msg, level)
        else:
            # Optional: wipe previous score JSON/Excel before recalculating from current activities
            if data_mode == "clear_then_compute":
                scoring.clear_score_outputs()

            if not ACTIVITIES_XLSX.exists():
                raise FileNotFoundError("No activity data yet. Fetch activities first.")

            if action == "team_scores":
                scoring.calculate_team_scores()
                result_msg = "Team scores calculated and saved."
            elif action == "leaders":
                scoring.compute_individual_leaders()
                result_msg = "Individual leaders calculated and saved."
            elif action == "supersteppers":
                result = scoring.calculate_supersteppers()
                required = load_settings()["superstepper"].get("required_days", 30)
                result_msg = (
                    f"SuperSteppers calculated using config ({required} days): "
                    f"{len(result['supersteppers'])} qualified."
                )
            elif action == "all":
                scoring.calculate_team_scores()
                scoring.compute_individual_leaders()
                result = scoring.calculate_supersteppers()
                required = load_settings()["superstepper"].get("required_days", 30)
                mode_label = (
                    "cleared old scores then recomputed"
                    if data_mode == "clear_then_compute"
                    else "recomputed from current Excel"
                )
                result_msg = (
                    f"All scores {mode_label}. SuperSteppers ({required}-day rule): "
                    f"{len(result['supersteppers'])}."
                )
            else:
                raise ValueError(f"Unknown action: {action}")

            flash(result_msg, level)

    except Exception as exc:
        level = "error"
        result_msg = f"Compute failed ({action or 'unknown'}): {exc}"
        flash(result_msg, "error")

    write_json(
        LAST_COMPUTE_FILE,
        {
            "action": action,
            "data_mode": data_mode,
            "message": result_msg,
            "level": level,
            "at": datetime.now().isoformat(timespec="seconds"),
        },
    )
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/queries", methods=["GET", "POST"])
@admin_required
def admin_queries():
    from services.storage import write_json, QUERIES_FILE

    queries = get_queries()
    if request.method == "POST":
        qid = request.form.get("id")
        action = request.form.get("action")
        for q in queries:
            if q.get("id") == qid:
                if action == "resolve":
                    q["status"] = "resolved"
                elif action == "reopen":
                    q["status"] = "open"
                elif action == "delete":
                    queries = [x for x in queries if x.get("id") != qid]
                break
        write_json(QUERIES_FILE, queries)
        flash("Query updated.", "success")
        return redirect(url_for("admin_queries"))

    return render_template("admin/queries.html", queries=queries)


@app.route("/admin/activities", methods=["GET", "POST"])
@admin_required
def admin_activities():
    """View and edit combined_athlete_activities.xlsx (fix misdated rows, etc.)."""
    import pandas as pd
    from datetime import timedelta

    columns = ["Athlete Name", "Athlete ID", "Date", "Total Distance", "Activities"]

    def _load_df() -> pd.DataFrame:
        if not ACTIVITIES_XLSX.exists():
            return pd.DataFrame(columns=columns)
        try:
            df = pd.read_excel(ACTIVITIES_XLSX)
        except Exception:
            return pd.DataFrame(columns=columns)
        for col in columns:
            if col not in df.columns:
                df[col] = "" if col != "Total Distance" else 0.0
        return df.reset_index(drop=True)

    def _save_df(df: pd.DataFrame) -> None:
        out = df.copy()
        # Normalize date to YYYY-MM-DD strings
        if "Date" in out.columns:
            out["Date"] = out["Date"].apply(_normalize_date_value)
        if "Total Distance" in out.columns:
            out["Total Distance"] = pd.to_numeric(out["Total Distance"], errors="coerce").fillna(0.0)
        if "Athlete ID" in out.columns:
            out["Athlete ID"] = pd.to_numeric(out["Athlete ID"], errors="coerce")
        ACTIVITIES_XLSX.parent.mkdir(parents=True, exist_ok=True)
        out.to_excel(ACTIVITIES_XLSX, index=False)

    def _normalize_date_value(value) -> str:
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return ""
        if hasattr(value, "strftime"):
            return value.strftime("%Y-%m-%d")
        text = str(value).strip()
        if not text or text.lower() == "nan":
            return ""
        # Excel sometimes yields "2026-09-01 00:00:00"
        if " " in text:
            text = text.split(" ")[0]
        if "T" in text:
            text = text.split("T")[0]
        return text

    def _selected_indices() -> list[int]:
        raw = request.form.getlist("selected")
        indices = []
        for item in raw:
            try:
                indices.append(int(item))
            except ValueError:
                continue
        return indices

    if request.method == "POST":
        action = request.form.get("action", "save")
        df = _load_df()

        if action == "save":
            row_ids = request.form.getlist("row_id")
            updated = 0
            for row_id in row_ids:
                try:
                    idx = int(row_id)
                except ValueError:
                    continue
                if idx < 0 or idx >= len(df):
                    continue
                prefix = f"row_{idx}_"
                if f"{prefix}name" in request.form:
                    df.at[idx, "Athlete Name"] = request.form.get(f"{prefix}name", "").strip()
                if f"{prefix}athlete_id" in request.form:
                    aid = request.form.get(f"{prefix}athlete_id", "").strip()
                    df.at[idx, "Athlete ID"] = int(aid) if aid.isdigit() else None
                if f"{prefix}date" in request.form:
                    df.at[idx, "Date"] = request.form.get(f"{prefix}date", "").strip()
                if f"{prefix}distance" in request.form:
                    try:
                        df.at[idx, "Total Distance"] = float(request.form.get(f"{prefix}distance") or 0)
                    except ValueError:
                        pass
                if f"{prefix}activities" in request.form:
                    df.at[idx, "Activities"] = request.form.get(f"{prefix}activities", "")
                updated += 1
            _save_df(df)
            flash(f"Saved {updated} row(s) to combined activities Excel.", "success")

        elif action in {"shift_minus_one", "shift_plus_one"}:
            selected = _selected_indices()
            if not selected:
                flash("Select at least one row to shift dates.", "warn")
            else:
                delta = -1 if action == "shift_minus_one" else 1
                changed = 0
                for idx in selected:
                    if idx < 0 or idx >= len(df):
                        continue
                    current = _normalize_date_value(df.at[idx, "Date"])
                    if not current:
                        continue
                    try:
                        day = datetime.strptime(current, "%Y-%m-%d")
                    except ValueError:
                        continue
                    df.at[idx, "Date"] = (day + timedelta(days=delta)).strftime("%Y-%m-%d")
                    changed += 1
                _save_df(df)
                direction = "back 1 day" if delta < 0 else "forward 1 day"
                flash(f"Shifted {changed} date(s) {direction} (e.g. Monday → Sunday).", "success")

        elif action == "delete":
            selected = sorted(set(_selected_indices()), reverse=True)
            if not selected:
                flash("Select at least one row to delete.", "warn")
            else:
                df = df.drop(index=[i for i in selected if 0 <= i < len(df)]).reset_index(drop=True)
                _save_df(df)
                flash(f"Deleted {len(selected)} row(s).", "success")

        elif action == "add_row":
            new_row = {
                "Athlete Name": request.form.get("new_name", "").strip(),
                "Athlete ID": request.form.get("new_athlete_id", "").strip() or None,
                "Date": request.form.get("new_date", "").strip(),
                "Total Distance": request.form.get("new_distance", "0").strip() or 0,
                "Activities": request.form.get("new_activities", "").strip(),
            }
            if not new_row["Athlete Name"] or not new_row["Date"]:
                flash("New row needs at least Athlete Name and Date.", "error")
            else:
                try:
                    new_row["Athlete ID"] = int(new_row["Athlete ID"]) if new_row["Athlete ID"] else None
                except ValueError:
                    new_row["Athlete ID"] = None
                try:
                    new_row["Total Distance"] = float(new_row["Total Distance"])
                except ValueError:
                    new_row["Total Distance"] = 0.0
                df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
                _save_df(df)
                flash("Added new activity row.", "success")

        # Preserve filters in redirect
        args = {
            "q": request.form.get("q", request.args.get("q", "")),
            "date_from": request.form.get("date_from", request.args.get("date_from", "")),
            "date_to": request.form.get("date_to", request.args.get("date_to", "")),
            "page": request.form.get("page", request.args.get("page", "1")),
        }
        return redirect(url_for("admin_activities", **{k: v for k, v in args.items() if v}))

    df = _load_df()
    q = (request.args.get("q") or "").strip().lower()
    date_from = (request.args.get("date_from") or "").strip()
    date_to = (request.args.get("date_to") or "").strip()
    try:
        page = max(1, int(request.args.get("page") or 1))
    except ValueError:
        page = 1
    per_page = 50

    # Attach original index before filtering
    df = df.copy()
    df["_row_id"] = df.index
    df["Date"] = df["Date"].apply(_normalize_date_value)

    filtered = df
    if q:
        filtered = filtered[
            filtered["Athlete Name"].astype(str).str.lower().str.contains(q, na=False)
            | filtered["Athlete ID"].astype(str).str.contains(q, na=False)
        ]
    if date_from:
        filtered = filtered[filtered["Date"] >= date_from]
    if date_to:
        filtered = filtered[filtered["Date"] <= date_to]

    total_rows = len(df)
    filtered_count = len(filtered)
    total_pages = max(1, (filtered_count + per_page - 1) // per_page)
    page = min(page, total_pages)
    start = (page - 1) * per_page
    page_df = filtered.iloc[start : start + per_page]

    rows = []
    for _, row in page_df.iterrows():
        aid = row.get("Athlete ID")
        if pd.isna(aid) or aid is None or str(aid).strip() == "":
            aid_text = ""
        else:
            try:
                aid_text = str(int(float(aid)))
            except (TypeError, ValueError):
                aid_text = str(aid)

        dist = row.get("Total Distance")
        if pd.isna(dist) or dist is None or str(dist).strip() == "":
            dist_text = ""
        else:
            try:
                dist_text = f"{float(dist):.3f}".rstrip("0").rstrip(".")
            except (TypeError, ValueError):
                dist_text = str(dist)

        rows.append(
            {
                "row_id": int(row["_row_id"]),
                "name": "" if pd.isna(row.get("Athlete Name")) else str(row.get("Athlete Name")),
                "athlete_id": aid_text,
                "date": _normalize_date_value(row.get("Date")),
                "distance": dist_text,
                "activities": "" if pd.isna(row.get("Activities")) else str(row.get("Activities")),
            }
        )

    return render_template(
        "admin/activities.html",
        rows=rows,
        total_rows=total_rows,
        filtered_count=filtered_count,
        page=page,
        total_pages=total_pages,
        q=q,
        date_from=date_from,
        date_to=date_to,
        file_exists=ACTIVITIES_XLSX.exists(),
    )


@app.route("/admin/download/<kind>")
@admin_required
def admin_download(kind: str):
    mapping = {
        "activities": ACTIVITIES_XLSX,
        "points": POINTS_XLSX,
    }
    path = mapping.get(kind)
    if not path or not path.exists():
        flash("File not found.", "error")
        return redirect(url_for("admin_dashboard"))
    return send_file(path, as_attachment=True)


@app.errorhandler(404)
def not_found(_err):
    return render_template("404.html"), 404


if __name__ == "__main__":
    app.run(debug=True, port=5000)
