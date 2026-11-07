# Ramp Hack

A small Flask app for our two-person hackathon. It serves both the web pages and a JSON API, and reads/writes files in Azure Blob Storage (`ramphackaffanandrj26` / container `data`).

**Live:** https://ramphack-affan.azurewebsites.net
(Free tier — the first request after idle can take 30+ seconds to wake up.)

| Route | What it does |
|---|---|
| `/` | Page that lists files in blob storage |
| `/api/files` | JSON list of blob names |

## Project layout

```
app.py              Flask app (routes)                 — owner: Affan
data.py             Blob helpers: list_files/upload/download — owner: Affan
templates/          HTML (Jinja)                       — owner: teammate
static/             style.css, app.js                  — owner: teammate
requirements.txt
.github/workflows/  Auto-deploy to Azure on push to main
```

## Local setup

```bash
git clone https://github.com/affansethi/ramp-hack.git
cd ramp-hack

python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

brew install azure-cli
az login --tenant 1eefd85e-b7f4-4150-9499-cbf14e7498bc

flask --app app run
```

Open http://127.0.0.1:5000. If `/api/files` returns a 403/auth error, ask Affan to give your Azure account the **Storage Blob Data Contributor** role on the storage account (role changes can take a few minutes to apply).

Auth uses `DefaultAzureCredential`: locally it uses your `az login`, and in Azure it uses the web app's managed identity. No keys or connection strings anywhere.

## Deployment

Every push to `main` runs [.github/workflows/deploy.yml](.github/workflows/deploy.yml), which deploys to Azure App Service. Watch it in the repo's **Actions** tab (or `gh run watch`). Dependencies are installed from `requirements.txt` on deploy and the app runs with gunicorn.

## Team rules

1. **Everyone works on `main`.** No branches needed.
2. **Ownership:** Affan owns `app.py` and `data.py`. Teammate owns `templates/` and `static/`. Ask before editing the other person's files.
3. **Sync before starting and before pushing:** `git pull --rebase`.
4. **Only push code that runs locally.** `main` auto-deploys to the live site.
5. **Prefix test blobs with your initials**, e.g. `affan/test.txt`, `rj/test.txt`.
6. **Secrets** go in a local `.env` (gitignored) and in App Service environment variables (Azure portal → web app → Settings → Environment variables). **Never in code.**
7. **Code freeze 2 hours before the demo.**
