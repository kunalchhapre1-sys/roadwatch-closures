from __future__ import annotations

import hashlib
import hmac
import html
import json
import re
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import folium
import geopandas as gpd
import pandas as pd
import pyogrio
import streamlit as st
from branca.element import MacroElement, Template
from streamlit.errors import StreamlitSecretNotFoundError
from streamlit_autorefresh import st_autorefresh
from streamlit_folium import st_folium


APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
ACTIVE_FILE = DATA_DIR / "current.gpkg"
MAX_FILE_SIZE = 50 * 1024 * 1024
DEFAULT_LATITUDE = 12.881703576462842
DEFAULT_LONGITUDE = 77.75966530609753
DISPLAY_TIMEZONE = ZoneInfo("Asia/Kolkata")
POSTGRES_CACHE_TTL = 25


st.set_page_config(
    page_title="Road Closure Monitor | Active road closures",
    page_icon="🚧",
    layout="wide",
    initial_sidebar_state="expanded",
)


st.html(f"<style>{(APP_DIR / 'styles.css').read_text(encoding='utf-8')}</style>")


def format_size(size: int) -> str:
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def format_modified_time(path: Path) -> str:
    modified_at = datetime.fromtimestamp(path.stat().st_mtime, tz=DISPLAY_TIMEZONE)
    return modified_at.strftime("%d %b %Y, %I:%M %p IST")


def get_admin_password() -> str | None:
    try:
        password = str(st.secrets["admin_password"])
    except (KeyError, StreamlitSecretNotFoundError):
        return None
    return password if password else None


def get_database_settings() -> dict[str, str] | None:
    try:
        config = st.secrets["road_closures_database"]
    except (KeyError, StreamlitSecretNotFoundError):
        return None

    if not bool(config.get("enabled", False)):
        return None

    settings = {
        "schema": str(config.get("schema", "public")),
        "table": str(config.get("table", "road_closures")),
        "geometry_column": str(config.get("geometry_column", "geom")),
    }
    for label, value in settings.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
            raise ValueError(
                f'Invalid PostgreSQL {label.replace("_", " ")}: "{value}".'
            )
    return settings


def parse_coordinates(value: str) -> tuple[float, float]:
    parts = [part.strip() for part in value.split(",")]
    if len(parts) != 2 or not all(parts):
        raise ValueError("Use this format: latitude, longitude")
    latitude, longitude = map(float, parts)
    if not -90 <= latitude <= 90:
        raise ValueError("Latitude must be between -90 and 90.")
    if not -180 <= longitude <= 180:
        raise ValueError("Longitude must be between -180 and 180.")
    return latitude, longitude


def parse_end_dates(frame: gpd.GeoDataFrame) -> pd.Series:
    """Return endtz values as local calendar dates, preserving naive dates."""

    def parse_value(value: object):
        if pd.isna(value):
            return None
        timestamp = pd.to_datetime(value, errors="coerce")
        if pd.isna(timestamp):
            return None
        if timestamp.tzinfo is not None:
            timestamp = timestamp.tz_convert(DISPLAY_TIMEZONE)
        return timestamp.date()

    return frame["endtz"].map(parse_value)


def filter_by_end_date(
    frame: gpd.GeoDataFrame,
    end_dates: pd.Series,
    operator: str,
    selected_date,
) -> gpd.GeoDataFrame:
    if operator == "<=":
        mask = end_dates.map(
            lambda value: value is not None and value <= selected_date
        )
    elif operator == ">=":
        mask = end_dates.map(
            lambda value: value is not None and value >= selected_date
        )
    else:
        mask = end_dates.map(
            lambda value: value is not None and value == selected_date
        )
    return frame.loc[mask].copy()


@st.cache_data(show_spinner=False)
def load_geopackage(path: str, modified_ns: int) -> tuple[gpd.GeoDataFrame, dict]:
    layer_rows = pyogrio.list_layers(path)
    if len(layer_rows) == 0:
        raise ValueError("This GeoPackage does not contain a feature layer.")

    frames: list[gpd.GeoDataFrame] = []
    layer_names: list[str] = []
    for row in layer_rows:
        layer_name = str(row[0])
        frame = gpd.read_file(path, layer=layer_name, engine="pyogrio")
        if frame.empty:
            continue
        if frame.crs is None:
            raise ValueError(f'Layer "{layer_name}" has no coordinate reference system.')
        frame = frame.to_crs(epsg=4326)
        frame["_layer"] = layer_name
        frames.append(frame)
        layer_names.append(layer_name)

    if not frames:
        raise ValueError("The GeoPackage does not contain any visible features.")

    combined = gpd.GeoDataFrame(
        pd.concat(frames, ignore_index=True),
        geometry="geometry",
        crs="EPSG:4326",
    )
    combined = combined[combined.geometry.notna() & ~combined.geometry.is_empty].copy()
    metadata = {
        "layers": layer_names,
        "feature_count": len(combined),
        "source_version": str(modified_ns),
    }
    return combined, metadata


@st.cache_data(
    ttl=POSTGRES_CACHE_TTL,
    max_entries=4,
    show_spinner=False,
)
def load_postgis(
    schema: str,
    table: str,
    geometry_column: str,
) -> tuple[gpd.GeoDataFrame, dict]:
    connection = st.connection("postgresql", type="sql")
    quoted_schema = f'"{schema}"'
    quoted_table = f'"{table}"'
    quoted_geometry = f'"{geometry_column}"'
    dashboard_geometry = "__dashboard_geometry"
    query = f"""
        SELECT
            source.*,
            ST_Transform(source.{quoted_geometry}, 4326) AS "{dashboard_geometry}"
        FROM {quoted_schema}.{quoted_table} AS source
        WHERE source.{quoted_geometry} IS NOT NULL
    """
    frame = gpd.read_postgis(
        query,
        connection.engine,
        geom_col=dashboard_geometry,
        crs="EPSG:4326",
    )
    frame = frame.drop(columns=[geometry_column], errors="ignore")
    frame = frame.rename_geometry("geometry")
    frame = frame[
        frame.geometry.notna() & ~frame.geometry.is_empty
    ].copy()
    synced_at = datetime.now(DISPLAY_TIMEZONE)
    metadata = {
        "layers": [f"{schema}.{table}"],
        "feature_count": len(frame),
        "source_version": synced_at.isoformat(),
        "synced_at": synced_at,
    }
    return frame, metadata


def geojson_data(frame: gpd.GeoDataFrame) -> dict:
    safe_frame = frame.copy()
    for column in safe_frame.columns:
        if column == safe_frame.geometry.name:
            continue
        safe_frame[column] = safe_frame[column].map(
            lambda value: value.isoformat()
            if isinstance(value, (date, datetime, pd.Timestamp))
            else value
        )
    return json.loads(safe_frame.to_json(drop_id=True))


def add_feature_click_popup(
    map_object: folium.Map,
    geojson_layer: folium.GeoJson,
) -> None:
    layer_name = geojson_layer.get_name()
    map_name = map_object.get_name()
    popup = MacroElement()
    popup._template = Template(
        f"""
        {{% macro script(this, kwargs) %}}
        {layer_name}.eachLayer(function(featureLayer) {{
          featureLayer.on("click", function(event) {{
            var properties = (featureLayer.feature && featureLayer.feature.properties) || {{}};
            function escapeHtml(value) {{
              return String(value ?? "—")
                .replaceAll("&", "&amp;")
                .replaceAll("<", "&lt;")
                .replaceAll(">", "&gt;")
                .replaceAll('"', "&quot;")
                .replaceAll("'", "&#039;");
            }}
            var coordinate = event.latlng.lat + ", " + event.latlng.lng;
            var content =
              '<div style="min-width:230px;font-family:Arial,sans-serif">' +
              '<strong style="font-size:14px">Road closure</strong>' +
              '<div style="margin-top:8px"><b>Latitude, Longitude</b><br>' +
              escapeHtml(coordinate) + '</div>' +
              '<div style="margin-top:8px"><b>City</b><br>' +
              escapeHtml(properties.city) + '</div>' +
              '<div style="margin-top:8px"><b>End date</b><br>' +
              escapeHtml(properties.endtz) + '</div></div>';
            L.popup({{maxWidth: 340}})
              .setLatLng(event.latlng)
              .setContent(content)
              .openOn({map_name});
          }});
        }});
        {{% endmacro %}}
        """
    )
    map_object.add_child(popup)


def build_map(
    frame: gpd.GeoDataFrame | None,
    latitude: float,
    longitude: float,
    target_selected: bool,
) -> folium.Map:
    map_object = folium.Map(
        location=[latitude, longitude],
        zoom_start=15 if target_selected else 14,
        tiles=None,
        control_scale=True,
        prefer_canvas=True,
        scroll_wheel_zoom=True,
    )
    folium.TileLayer(
        tiles="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
        attr="© OpenStreetMap contributors",
        name="OpenStreetMap",
        overlay=False,
        control=True,
    ).add_to(map_object)
    if frame is not None and not frame.empty:
        layer = folium.GeoJson(
            data=geojson_data(frame),
            name="Active road closures",
            style_function=lambda _feature: {
                "color": "#e63b2e",
                "weight": 5,
                "opacity": 0.94,
                "fillColor": "#ef4b3e",
                "fillOpacity": 0.14,
                "dashArray": "1 10",
                "lineCap": "round",
                "lineJoin": "round",
            },
            highlight_function=lambda _feature: {
                "color": "#b82017",
                "weight": 7,
                "opacity": 1,
            },
            marker=folium.CircleMarker(
                radius=7,
                color="#e63b2e",
                weight=4,
                fill=True,
                fill_color="#e63b2e",
                fill_opacity=0.9,
            ),
            tooltip=folium.GeoJsonTooltip(
                fields=[field for field in ("city", "endtz") if field in frame.columns],
                aliases=[
                    alias
                    for field, alias in (("city", "City:"), ("endtz", "End date:"))
                    if field in frame.columns
                ],
                sticky=False,
            ),
        )
        layer.add_to(map_object)
        add_feature_click_popup(map_object, layer)
        folium.LayerControl(collapsed=True).add_to(map_object)

        if not target_selected:
            min_x, min_y, max_x, max_y = frame.total_bounds
            map_object.fit_bounds([[min_y, min_x], [max_y, max_x]], padding=(28, 28))

    if target_selected:
        folium.CircleMarker(
            location=[latitude, longitude],
            radius=9,
            color="#ffffff",
            weight=3,
            fill=True,
            fill_color="#176b5b",
            fill_opacity=1,
            tooltip=f"{latitude}, {longitude}",
        ).add_to(map_object)

    return map_object


DATA_DIR.mkdir(parents=True, exist_ok=True)
st_autorefresh(interval=30_000, key="roadwatch-file-watch")

if "coordinate_text" not in st.session_state:
    st.session_state.coordinate_text = (
        f"{DEFAULT_LATITUDE}, {DEFAULT_LONGITUDE}"
    )
if "target_latitude" not in st.session_state:
    st.session_state.target_latitude = DEFAULT_LATITUDE
if "target_longitude" not in st.session_state:
    st.session_state.target_longitude = DEFAULT_LONGITUDE
if "target_selected" not in st.session_state:
    st.session_state.target_selected = False
if "date_filter_active" not in st.session_state:
    st.session_state.date_filter_active = False
if "date_filter_operator" not in st.session_state:
    st.session_state.date_filter_operator = "="


frame: gpd.GeoDataFrame | None = None
metadata = {"layers": [], "feature_count": 0, "source_version": "0"}
load_error: str | None = None
source_warning: str | None = None
source_mode = "geopackage"
database_settings: dict[str, str] | None = None
try:
    database_settings = get_database_settings()
except ValueError as error:
    source_warning = str(error)

if database_settings is not None:
    try:
        frame, metadata = load_postgis(
            database_settings["schema"],
            database_settings["table"],
            database_settings["geometry_column"],
        )
        source_mode = "postgresql"
    except Exception:
        source_warning = (
            "PostgreSQL could not be read. Check the database secrets, "
            "network access, table name, and geometry SRID. "
            "Showing the GeoPackage fallback."
        )
        source_mode = "geopackage_fallback"

if frame is None and ACTIVE_FILE.exists():
    try:
        frame, metadata = load_geopackage(
            str(ACTIVE_FILE),
            ACTIVE_FILE.stat().st_mtime_ns,
        )
    except Exception as error:
        load_error = str(error)

if frame is None and source_warning:
    load_error = source_warning

end_dates: pd.Series | None = None
valid_end_dates = pd.Series(dtype="object")
if frame is not None and "endtz" in frame.columns:
    end_dates = parse_end_dates(frame)
    valid_end_dates = end_dates.dropna()

if "date_filter_date" not in st.session_state:
    st.session_state.date_filter_date = (
        valid_end_dates.min()
        if not valid_end_dates.empty
        else datetime.now(DISPLAY_TIMEZONE).date()
    )


if source_mode == "postgresql" and database_settings is not None:
    source_name = (
        f'{database_settings["schema"]}.{database_settings["table"]}'
    )
    source_type = "PostgreSQL / PostGIS"
    updated_time = metadata["synced_at"].strftime(
        "%d %b %Y, %I:%M %p IST"
    )
    update_label = "Last sync"
elif ACTIVE_FILE.exists():
    source_name = ACTIVE_FILE.name
    if source_mode == "geopackage_fallback":
        source_name += " (fallback)"
    source_type = format_size(ACTIVE_FILE.stat().st_size)
    updated_time = format_modified_time(ACTIVE_FILE)
    update_label = "Last update"
else:
    source_name = "No data source available"
    source_type = "—"
    updated_time = "Waiting for data"
    update_label = "Last update"

with st.sidebar:
    st.html(
        """
        <div class="rw-panel-heading">
          <p class="rw-eyebrow">Map controls</p>
          <h1>Road closure viewer</h1>
          <p>Navigate by coordinate and explore the latest road closure data.</p>
        </div>
        """
    )

    location_tab, date_filter_tab, upload_tab = st.tabs(
        ["Lat / Long", "Date filter", "Admin upload"]
    )
    with location_tab:
        st.html(
            """
            <div class="rw-tool-intro">
              <div class="rw-tool-icon">⌖</div>
              <div>
                <strong>Go to a location</strong>
                <span>Enter WGS84 decimal coordinates.</span>
              </div>
            </div>
            """
        )
        coordinate_text = st.text_input(
            "Latitude, Longitude",
            key="coordinate_text",
            placeholder=f"{DEFAULT_LATITUDE}, {DEFAULT_LONGITUDE}",
        )
        if st.button("Go to location", type="primary", width="stretch"):
            try:
                selected_latitude, selected_longitude = parse_coordinates(
                    coordinate_text
                )
                st.session_state.target_latitude = selected_latitude
                st.session_state.target_longitude = selected_longitude
                st.session_state.target_selected = True
                st.rerun()
            except ValueError as error:
                st.error(str(error))
        st.html(
            f"""
            <div class="rw-coordinate-card">
              <span>Current target</span>
              <strong>{st.session_state.target_latitude}, {st.session_state.target_longitude}</strong>
            </div>
            """
        )

    with date_filter_tab:
        st.html(
            """
            <div class="rw-tool-intro">
              <div class="rw-tool-icon">⌑</div>
              <div>
                <strong>Filter by end date</strong>
                <span>Uses the active data source endtz column.</span>
              </div>
            </div>
            """
        )
        if frame is None:
            st.info("Connect or publish a data source to use the date filter.")
        elif "endtz" not in frame.columns:
            st.warning("The active data source does not contain an endtz column.")
        elif valid_end_dates.empty:
            st.warning("No valid dates were found in the endtz column.")
        else:
            with st.form("date_filter_form", border=False):
                st.selectbox(
                    "Condition",
                    options=["=", "<=", ">="],
                    format_func={
                        "=": "= Exact date",
                        "<=": "≤ On or before",
                        ">=": "≥ On or after",
                    }.get,
                    key="date_filter_operator",
                )
                st.date_input("End date", key="date_filter_date")
                filter_submitted = st.form_submit_button(
                    "Apply filter",
                    type="primary",
                    width="stretch",
                )

            if filter_submitted:
                st.session_state.date_filter_active = True

            if st.session_state.date_filter_active:
                operator_labels = {
                    "=": "equal to",
                    "<=": "on or before",
                    ">=": "on or after",
                }
                active_operator = st.session_state.date_filter_operator
                active_date = st.session_state.date_filter_date
                st.html(
                    f"""
                    <div class="rw-filter-card">
                      <span>Active filter</span>
                      <strong>End date {operator_labels[active_operator]}
                      {active_date.strftime("%d %b %Y")}</strong>
                    </div>
                    """
                )
                if st.button(
                    "Clear date filter",
                    width="stretch",
                    key="clear_date_filter",
                ):
                    st.session_state.date_filter_active = False
                    st.rerun()

    with upload_tab:
        if database_settings is not None:
            if source_mode == "postgresql":
                st.success("PostgreSQL / PostGIS is connected.")
            else:
                st.warning("Database unavailable; GeoPackage fallback is active.")
            st.info(
                "Edit attributes in pgAdmin and commit the transaction. "
                "The dashboard checks for changes every 30 seconds."
            )
        st.html(
            """
            <div class="rw-tool-intro">
              <div class="rw-tool-icon">↑</div>
              <div>
                <strong>Restricted upload</strong>
                <span>Administrator access is required.</span>
              </div>
            </div>
            """
        )
        admin_password = get_admin_password()
        admin_signature = (
            hashlib.sha256(admin_password.encode("utf-8")).hexdigest()
            if admin_password is not None
            else None
        )
        admin_authenticated = bool(
            admin_signature
            and hmac.compare_digest(
                str(st.session_state.get("admin_signature", "")),
                admin_signature,
            )
        )
        if admin_authenticated:
            st.success("Administrator access enabled.")
            if st.button("Log out", width="stretch"):
                st.session_state.pop("admin_signature", None)
                st.rerun()

            uploaded_file = st.file_uploader(
                "Choose a GeoPackage",
                type=["gpkg"],
                help="Maximum file size: 50 MB.",
            )
            if uploaded_file is not None:
                contents = uploaded_file.getvalue()
                digest = hashlib.sha256(contents).hexdigest()
                upload_token = f"{getattr(uploaded_file, 'file_id', '')}:{digest}"
                if st.session_state.get("uploaded_token") != upload_token:
                    if len(contents) > MAX_FILE_SIZE:
                        st.error("The GeoPackage must be 50 MB or smaller.")
                    else:
                        ACTIVE_FILE.write_bytes(contents)
                        st.session_state.uploaded_token = upload_token
                        st.cache_data.clear()
                        st.success(f"{uploaded_file.name} is now active.")
                        st.rerun()
        elif admin_password is None:
            st.warning(
                "Admin upload is locked until `admin_password` is configured "
                "in Streamlit Secrets."
            )
        else:
            with st.form("admin_login", border=False):
                password_attempt = st.text_input(
                    "Administrator password",
                    type="password",
                    placeholder="Enter your password",
                )
                login_submitted = st.form_submit_button(
                    "Unlock upload",
                    type="primary",
                    width="stretch",
                )
            if login_submitted:
                if hmac.compare_digest(password_attempt, admin_password):
                    st.session_state.admin_signature = admin_signature
                    st.rerun()
                else:
                    st.error("Incorrect administrator password.")
        st.html(
            f"""
            <div class="rw-file-card">
              <strong>{html.escape(source_name)}</strong>
              <span>{html.escape(source_type)} · {updated_time}</span>
            </div>
            """
        )
        if database_settings is not None:
            st.caption(
                "The upload remains available as an administrator-only "
                "GeoPackage fallback."
            )
        else:
            st.caption(
                "New files are checked automatically every 30 seconds. "
                "Cloud deployments require persistent storage for durable uploads."
            )

    if source_warning:
        st.warning(source_warning)
    if load_error and load_error != source_warning:
        st.error(f"The road closure data could not be read: {load_error}")


display_frame = frame
if (
    frame is not None
    and end_dates is not None
    and st.session_state.date_filter_active
):
    display_frame = filter_by_end_date(
        frame,
        end_dates,
        st.session_state.date_filter_operator,
        st.session_state.date_filter_date,
    )

visible_feature_count = len(display_frame) if display_frame is not None else 0
total_feature_count = metadata["feature_count"]
date_filter_active = bool(
    frame is not None
    and end_dates is not None
    and st.session_state.date_filter_active
)
status_text = (
    (
        f"{visible_feature_count:,} of {total_feature_count:,} "
        "closure features visible"
        if date_filter_active
        else f"{visible_feature_count:,} closure features visible"
    )
    if frame is not None and not load_error
    else "Waiting for road closure data"
)
status_class = "ready" if frame is not None and not load_error else ""

st.html(
    f"""
    <div class="rw-topbar">
      <div class="rw-brand-mark" aria-hidden="true"><span></span></div>
      <div class="rw-brand-copy">
        <strong>Road Closure Monitor</strong>
        <span>Active closure dashboard</span>
      </div>
      <div class="rw-live-chip"><span class="rw-live-dot"></span>LIVE</div>
      <div class="rw-header-status">
        <span>{update_label}</span>
        <strong>{updated_time}</strong>
      </div>
    </div>
    <div class="rw-panel-footer">
      <div class="rw-footer-status">
        <span class="rw-status-dot {status_class}"></span>
        <span>{html.escape(status_text)}</span>
      </div>
      <div class="rw-footer-credit">
        Designed &amp; created by <strong>Kunal Chhapre</strong>
      </div>
    </div>
    <div class="rw-map-title">
      <span>NETWORK VIEW</span>
      <strong>Active road closures</strong>
    </div>
    """
)

closure_map = build_map(
    frame=display_frame,
    latitude=st.session_state.target_latitude,
    longitude=st.session_state.target_longitude,
    target_selected=st.session_state.target_selected,
)
map_filter_key = (
    f"{st.session_state.date_filter_operator}-"
    f"{st.session_state.date_filter_date.isoformat()}"
    if date_filter_active
    else "all"
)
st_folium(
    closure_map,
    width=None,
    height=800,
    returned_objects=[],
    key=(
        "closure-map-"
        f'{metadata.get("source_version", "0")}-'
        f"{map_filter_key}"
    ),
)

st.html(
    f"""
    <div class="rw-feature-count">
      <span class="rw-closure-swatch"></span>
      <div class="rw-feature-copy">
        <strong>{visible_feature_count:,}</strong>
        <span>{"Filtered features" if date_filter_active else "Closure features"}</span>
      </div>
    </div>
    """
)
