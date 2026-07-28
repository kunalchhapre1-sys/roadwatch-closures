# RoadWatch Python dashboard

This is the local Python version of the RoadWatch active road-closure dashboard.
It runs independently from the hosted TypeScript application.

## Features

- OpenStreetMap background
- GeoPackage upload and local file replacement
- automatic file refresh every 30 seconds
- dotted red road-closure symbology
- combined `Latitude, Longitude` search
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

## Update the closure layer

Use the **Input file** tab in the dashboard, or replace:

```text
python_app/data/current.gpkg
```

Uploaded GeoPackages are intentionally excluded from GitHub.

## Stop the dashboard

Press `Ctrl+C` in the VS Code terminal.
