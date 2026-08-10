# Strava Team Tracker WebApp

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue?logo=python&logoColor=white)](https://www.python.org/)
[![Flask](https://img.shields.io/badge/Flask-3.x-black?logo=flask&logoColor=white)](https://flask.palletsprojects.com/)
[![Strava API](https://img.shields.io/badge/Strava-API-fc4c02?logo=strava&logoColor=white)](https://developers.strava.com/)
[![GitHub](https://img.shields.io/badge/GitHub-RorYin%2FStravaTeamTrackerWebApp-181717?logo=github)](https://github.com/RorYin/StravaTeamTrackerWebApp)
[![PythonAnywhere](https://img.shields.io/badge/Host-PythonAnywhere-1f6feb?logo=python&logoColor=white)](#deploy-on-pythonanywhere)

Flask web application for company and club walking challenges powered by Strava OAuth.

Athletes authorize once, join a team, and admins fetch activities to compute team scores, individual leaders, and SuperStepper results. Configuration, users, and scores are stored as JSON and Excel under `data/`.

---

## Features

| Area | Description |
|------|-------------|
| Public site | Strava authorize, team scores, SuperStepper board, leaders, query form |
| Profile flow | Required team and gender after first authorization |
| Auth limit | Maximum of two authorizations per athlete; further attempts are directed to Query |
| Admin panel | Fetch activities, compute scores, manage users, config, and queries |
| Configuration | Challenge dates, teams, logos, scoring caps, SuperStepper rules, Strava credentials |

---

## Repository layout

```
StravaTeamTrackerWebApp/
├── app.py                      # Flask application and routes
├── config/
│   └── settings.example.json   # Copy to settings.json and edit
├── data/                       # Empty seed JSON and Excel workbooks
├── services/                   # Strava, scoring, storage, fetch
├── static/                     # CSS and images
├── templates/                  # Jinja templates
├── requirements.txt
└── LICENSE
```

---

## Local development

```bash
git clone https://github.com/RorYin/StravaTeamTrackerWebApp.git
cd StravaTeamTrackerWebApp

python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
cp config/settings.example.json config/settings.json
```

Edit `config/settings.json`:

1. Set Strava `client_id` and `client_secret`
2. Set `redirect_uri` to `http://localhost:5000/authorized`
3. Set a strong `admin.password`

In [Strava API settings](https://www.strava.com/settings/api), set **Authorization Callback Domain** to `localhost`.

```bash
python app.py
```

| URL | Purpose |
|-----|---------|
| http://localhost:5000 | Public app |
| http://localhost:5000/admin/login | Admin login |

---

## Deploy on PythonAnywhere

Replace `YOUR_USERNAME` with your PythonAnywhere username throughout.

### 1. Clone the repository

Open a **Bash** console on PythonAnywhere and run:

```bash
cd ~
git clone https://github.com/RorYin/StravaTeamTrackerWebApp.git
cd StravaTeamTrackerWebApp
```

### 2. Create a virtualenv and install dependencies

```bash
mkvirtualenv --python=/usr/bin/python3.10 StravaTeamTrackerWebApp
pip install -r requirements.txt
```

If `mkvirtualenv` is unavailable:

```bash
python3.10 -m venv ~/.virtualenvs/StravaTeamTrackerWebApp
source ~/.virtualenvs/StravaTeamTrackerWebApp/bin/activate
pip install -r requirements.txt
```

### 3. Configure settings

```bash
cp config/settings.example.json config/settings.json
nano config/settings.json
```

Set at least:

| Key | Value |
|-----|--------|
| `strava.client_id` | Your Strava client ID |
| `strava.client_secret` | Your Strava client secret |
| `strava.redirect_uri` | `https://YOUR_USERNAME.pythonanywhere.com/authorized` |
| `admin.password` | A strong admin password |

In [Strava API settings](https://www.strava.com/settings/api), set **Authorization Callback Domain** to:

```text
YOUR_USERNAME.pythonanywhere.com
```

### 4. Create the web app

In the PythonAnywhere dashboard: **Web** → **Add a new web app** → **Manual configuration** → Python 3.10+.

Then set:

| Field | Value |
|-------|--------|
| Source code | `/home/YOUR_USERNAME/StravaTeamTrackerWebApp` |
| Working directory | `/home/YOUR_USERNAME/StravaTeamTrackerWebApp` |
| Virtualenv | `/home/YOUR_USERNAME/.virtualenvs/StravaTeamTrackerWebApp` |

### 5. Configure WSGI

Open the WSGI configuration file linked from the Web tab and replace its contents with:

```python
import os
import sys
from pathlib import Path

# Required on free PythonAnywhere accounts so Strava API calls can reach the internet
os.environ.setdefault("http_proxy", "http://proxy.server:3128")
os.environ.setdefault("https_proxy", "http://proxy.server:3128")

PROJECT_ROOT = Path("/home/YOUR_USERNAME/StravaTeamTrackerWebApp")

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app import app as application
```

Save the file, then click **Reload** on the Web tab.

If authorize fails with `Network is unreachable` / `Errno 101`, you are on a **free** account and outbound traffic must go through PythonAnywhere’s proxy. `www.strava.com` is allowlisted; set `pythonanywhere_free_proxy` to `true` in `settings.json` (default in the example), keep the WSGI proxy lines above, reload, and try again. Paid accounts have unrestricted internet and can set that flag to `false`.

### 6. Use the app

Open `https://YOUR_USERNAME.pythonanywhere.com`.

| Path | Purpose |
|------|---------|
| `/` | Public home |
| `/admin/login` | Admin panel |

Empty seed files under `data/` are already in the repository, so scoring and fetch flows work immediately after configuration.

### Updating later

```bash
cd ~/StravaTeamTrackerWebApp
git pull
# re-install only if requirements.txt changed
pip install -r requirements.txt
```

Then click **Reload** on the Web tab.

If you have live challenge data, avoid resetting `config/settings.json` or filled files under `data/` when pulling updates.

---

## Admin workflow

1. Athletes authorize with Strava and complete team and gender.
2. Admin opens **Fetch activities** (append for weekly updates, or clear and replace for a full reset).
3. Admin runs **Compute** for team scores, leaders, and SuperSteppers.
4. Public pages read the updated JSON and Excel outputs automatically.

---

## Scoring rules

| Rule | Behavior |
|------|----------|
| Team points | Daily km capped by `scoring.max_km_per_day` per user per day; top N athletes per team are summed |
| SuperStepper | Driven by `superstepper.min_km_per_day` and `superstepper.required_days` |
| Activity filter | Pace must be slower than `scoring.min_pace_min_per_km` |

---

## Configuration and secrets

| File | Tracked in Git | Notes |
|------|----------------|-------|
| `config/settings.example.json` | Yes | Template only |
| `config/settings.json` | No | Local secrets; create from the example |
| `data/*.json` / `data/*.xlsx` | Yes (empty seeds) | Do not commit filled live data with tokens or scores |

Never commit real Strava secrets, admin passwords, or OAuth tokens.

---

## License

Released under the [MIT License](LICENSE).
