# RoadWatch Python dashboard

This is the local Python version of the RoadWatch active road-closure dashboard.
It runs independently from the hosted TypeScript application.

## Features

- OpenStreetMap background
- optional live PostgreSQL/PostGIS data source with a 30-second refresh
- administrator-only GeoPackage upload and local file replacement
- automatic file refresh every 30 seconds
- dotted red road-closure symbology
- combined `Latitude, Longitude` search
- `endtz` date filtering with `=`, `<=`, and `>=` conditions
- clicked feature coordinates
- `city` and `endtz` attribute display
- multiple feature layers from one GeoPackage

## Connect PostgreSQL / PostGIS

The database table must use PostGIS and contain:

- a geometry column, normally `geom`, with a valid SRID
- an `endtz` column for the date filter and popup
- a `city` column for the popup

In pgAdmin, enable PostGIS in the target database:

```sql
CREATE EXTENSION IF NOT EXISTS postgis;
```

Import the GeoPackage as a PostGIS layer using QGIS **Database > DB Manager >
Import Layer/File**, and name the table `public.road_closures`. Create a
separate read-only login for the dashboard, then grant only the permissions it
needs:

```sql
GRANT CONNECT ON DATABASE your_database TO dashboard_reader;
GRANT USAGE ON SCHEMA public TO dashboard_reader;
GRANT SELECT ON TABLE public.road_closures TO dashboard_reader;
```

Copy `.streamlit/secrets.example.toml` to `.streamlit/secrets.toml`, replace
the placeholders, and keep:

```toml
[road_closures_database]
enabled = true
schema = "public"
table = "road_closures"
geometry_column = "geom"
```

The real `secrets.toml` is excluded from Git. For Streamlit Community Cloud,
paste the same contents into the app's **Settings > Secrets** page.

After saving and committing an attribute edit in pgAdmin, the dashboard
automatically rereads the table within 30 seconds. If PostgreSQL is unavailable,
the app shows the saved GeoPackage as a fallback.

For the public Streamlit site, the PostgreSQL server must be reachable from the
internet and allow encrypted inbound connections. A PostgreSQL server that is
available only as `localhost` on your computer cannot be reached by Streamlit
Community Cloud.

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
