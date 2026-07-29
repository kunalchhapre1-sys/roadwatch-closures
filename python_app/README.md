# RoadWatch Python dashboard

This is the local Python version of the RoadWatch active road-closure dashboard.
It runs independently from the hosted TypeScript application.

## Features

- OpenStreetMap background
- administrator-only GeoPackage upload and local file replacement
- automatic file refresh every 30 seconds
- dotted red road-closure symbology
- combined `Latitude, Longitude` search
- `endtz` date filtering with `=`, `<=`, and `>=` conditions
- clicked feature coordinates
- `city` and `endtz` attribute display
- multiple feature layers from one GeoPackage

## Start on Windows from VS Code

Open the repository folder in VS Code, open a terminal, and run:

```powershell
cd python_app
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m streamlit run app.py
```

The dashboard opens at `http://localhost:8501`.

## Configure administrator upload access

Create `python_app/.streamlit/secrets.toml` for local use:

```toml
admin_password = "replace-this-with-a-strong-private-password"
```

This file is excluded from Git and must never be committed. For Streamlit
Community Cloud, open the app's **Settings**, select **Secrets**, add the same
line, and click **Save**.

After signing in through the **Admin upload** tab, upload a GeoPackage to
replace:

```text
python_app/data/current.gpkg
```

Uploaded GeoPackages are intentionally excluded from GitHub.

## Stop the dashboard

Press `Ctrl+C` in the VS Code terminal.
