from __future__ import annotations

import hashlib
import hmac
import html
import json
import re
import uuid
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
CITY_BOUNDARY_FILE = DATA_DIR / "T7_merged_city_boundary.gpkg"
MAX_FILE_SIZE = 50 * 1024 * 1024
DEFAULT_LATITUDE = 12.881703576462842
DEFAULT_LONGITUDE = 77.75966530609753
DISPLAY_TIMEZONE = ZoneInfo("Asia/Kolkata")
REFRESH_INTERVAL_SECONDS = 15 * 60
REFRESH_INTERVAL_LABEL = "15 minutes"
POSTGRES_CACHE_TTL = 14 * 60


class DraggableMarkerBridge(MacroElement):
    """Send a Leaflet marker's final drag position back through st_folium."""

    _template = Template(
        """
        {% macro script(this, kwargs) %}
        {{ this.marker_name }}.on('dragend', function(event) {
            {{ this.map_name }}.fire('draw:created', {
                layer: event.target,
                layerType: 'marker'
            });
        });
        {% endmacro %}
        """
    )

    def __init__(self, marker: folium.Marker, map_object: folium.Map):
        super().__init__()
        self._name = "DraggableMarkerBridge"
        self.marker_name = marker.get_name()
        self.map_name = map_object.get_name()


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


def get_google_sheets_settings() -> dict | None:
    """Return Google Sheets settings when submissions are explicitly enabled."""
    try:
        config = st.secrets["google_sheets"]
        credentials = st.secrets["google_service_account"]
    except (KeyError, StreamlitSecretNotFoundError):
        return None

    if not bool(config.get("enabled", False)):
        return None

    spreadsheet_id = str(config.get("spreadsheet_id", "")).strip()
    worksheet = str(config.get("worksheet", "Road closure reports")).strip()
    if not spreadsheet_id or not worksheet:
        raise ValueError(
            "Google Sheets is enabled, but spreadsheet_id or worksheet is missing."
        )

    return {
        "spreadsheet_id": spreadsheet_id,
        "worksheet": worksheet,
        "credentials": dict(credentials),
    }


def append_report_to_google_sheet(settings: dict, report: dict[str, str]) -> None:
    """Append one public report without changing the production database."""
    import gspread

    client = gspread.service_account_from_dict(settings["credentials"])
    spreadsheet = client.open_by_key(settings["spreadsheet_id"])
    try:
        worksheet = spreadsheet.worksheet(settings["worksheet"])
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(
            title=settings["worksheet"], rows=1000, cols=len(report)
        )

    headers = list(report.keys())
    if not worksheet.row_values(1):
        worksheet.append_row(headers, value_input_option="RAW")
    worksheet.append_row(list(report.values()), value_input_option="RAW")


def city_for_coordinate(
    city_boundaries: gpd.GeoDataFrame | None,
    latitude: float,
    longitude: float,
) -> str:
    """Return the local boundary's city name for a WGS84 coordinate."""
    if (
        city_boundaries is None
        or city_boundaries.empty
        or "CITY_NME" not in city_boundaries.columns
    ):
        return ""

    point = gpd.points_from_xy([longitude], [latitude], crs="EPSG:4326")[0]
    matches = city_boundaries.loc[city_boundaries.geometry.covers(point)]
    if matches.empty:
        return ""
    return str(matches.iloc[0]["CITY_NME"]).strip()


def render_road_closure_report_form(
    google_sheets_settings: dict | None,
    city_boundaries: gpd.GeoDataFrame | None,
) -> None:
    """Render the public report form and its latest-submission preview."""
    st.html(
        """
        <div class="rw-tool-intro">
          <div class="rw-tool-icon">+</div>
          <div>
            <strong>Report a road closure</strong>
            <span>Send an observation for administrator review.</span>
          </div>
        </div>
        """
    )
    if google_sheets_settings is None:
        st.info("Preview mode: submissions are displayed below but are not saved.")
    else:
        st.success("Submissions are saved to the review Google Sheet.")

    detected_city = city_for_coordinate(
        city_boundaries,
        st.session_state.report_latitude,
        st.session_state.report_longitude,
    )
    if not st.session_state.report_city and detected_city:
        st.session_state.report_city = detected_city

    st.text_input(
        "Latitude, Longitude",
        key="report_coordinate_text",
        placeholder=f"{DEFAULT_LATITUDE}, {DEFAULT_LONGITUDE}",
    )
    go_column, pin_column = st.columns(2, gap="xsmall")
    with go_column:
        go_to_location = st.button(
            "Go to location",
            type="primary",
            width="stretch",
            key="report_go_to_location",
        )
    with pin_column:
        pin_clicked = st.button(
            "Pin",
            icon=":material/location_on:",
            type="secondary",
            help="Show or hide the movable pin on the main map.",
            width="stretch",
            key="report_pin_toggle",
        )

    if go_to_location:
        try:
            report_latitude, report_longitude = parse_coordinates(
                st.session_state.report_coordinate_text
            )
            st.session_state.report_latitude = report_latitude
            st.session_state.report_longitude = report_longitude
            st.session_state.report_city = city_for_coordinate(
                city_boundaries,
                report_latitude,
                report_longitude,
            )
            st.session_state.report_pin_active = True
            st.rerun()
        except ValueError as error:
            st.error(str(error))

    if pin_clicked:
        st.session_state.report_pin_active = not st.session_state.report_pin_active
        st.rerun()

    if st.session_state.report_pin_active:
        st.info("Drag the red pin on the large map, then release it at the location.")
        st.caption(
            "Selected: "
            f"{st.session_state.report_latitude:.8f}, "
            f"{st.session_state.report_longitude:.8f}"
        )

    now = datetime.now(DISPLAY_TIMEZONE)
    with st.form("road_closure_report_form", border=False):
        reporter_name = st.text_input("Reporter name", placeholder="Your full name")
        employee_id = st.text_input("Employee ID", placeholder="Optional")
        city = st.text_input(
            "City",
            key="report_city",
            placeholder="Filled automatically from the selected pin",
        )
        closure_type = st.selectbox(
            "Closure type",
            ["Full closure", "Partial closure", "Lane closure", "Other"],
        )
        observed_date = st.date_input("Observed date", value=now.date())
        observed_time = st.time_input(
            "Observed time", value=now.time().replace(second=0, microsecond=0)
        )
        expected_end = st.text_input(
            "Expected end date/time",
            placeholder="Optional, for example 05 Aug 2026, 06:00 PM",
        )
        reason = st.text_input(
            "Reason", placeholder="Construction, event, flooding, etc."
        )
        notes = st.text_area(
            "Additional details",
            placeholder="Describe the closure and any diversion information.",
        )
        evidence_link = st.text_input(
            "Photo or evidence link",
            placeholder="Optional Google Drive or approved shared link",
        )
        report_submitted = st.form_submit_button(
            "Submit for review",
            type="primary",
            width="stretch",
            icon=":material/send:",
        )

    if report_submitted:
        if not reporter_name.strip() or not city.strip():
            st.error("Reporter name and city are required.")
        else:
            observed_at = datetime.combine(
                observed_date, observed_time, tzinfo=DISPLAY_TIMEZONE
            )
            submitted_at = datetime.now(DISPLAY_TIMEZONE)
            report = {
                "report_id": (
                    f"RC-{submitted_at.strftime('%Y%m%d-%H%M%S')}-"
                    f"{uuid.uuid4().hex[:4].upper()}"
                ),
                "status": "Pending",
                "submitted_at": submitted_at.isoformat(timespec="seconds"),
                "reporter_name": reporter_name.strip(),
                "employee_id": employee_id.strip(),
                "city": city.strip(),
                "location": (
                    f"{st.session_state.report_latitude:.8f}, "
                    f"{st.session_state.report_longitude:.8f}"
                ),
                "latitude": f"{st.session_state.report_latitude:.8f}",
                "longitude": f"{st.session_state.report_longitude:.8f}",
                "closure_type": closure_type,
                "observed_at": observed_at.isoformat(timespec="minutes"),
                "expected_end": expected_end.strip(),
                "reason": reason.strip(),
                "notes": notes.strip(),
                "evidence_link": evidence_link.strip(),
            }
            st.session_state.report_preview = report
            if google_sheets_settings is None:
                st.success("Preview created. Nothing was saved or sent.")
            else:
                try:
                    append_report_to_google_sheet(google_sheets_settings, report)
                    st.success(
                        f"Report {report['report_id']} was submitted for review."
                    )
                except Exception:
                    st.error(
                        "The report could not be saved to Google Sheets. "
                        "Check the Sheet sharing and Streamlit secrets."
                    )

    if st.session_state.report_preview:
        report = st.session_state.report_preview
        with st.container(border=True):
            st.caption("Latest submission preview")
            st.write(f"**{report['report_id']} · {report['status']}**")
            st.write(report["city"])
            st.code(f"{report['latitude']}, {report['longitude']}")
            st.caption(
                f"{report['closure_type']} · Observed {report['observed_at']}"
            )


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


def parse_end_timestamp(value: object) -> pd.Timestamp | None:
    if pd.isna(value):
        return None
    timestamp = pd.to_datetime(value, errors="coerce")
    if pd.isna(timestamp):
        return None
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize(DISPLAY_TIMEZONE)
    else:
        timestamp = timestamp.tz_convert(DISPLAY_TIMEZONE)
    return timestamp


def expiration_flags(frame: gpd.GeoDataFrame) -> pd.Series:
    """Return True for road closures whose end time has passed."""
    if "endtz" not in frame.columns:
        return pd.Series(False, index=frame.index, dtype=bool)

    current_time = pd.Timestamp.now(tz=DISPLAY_TIMEZONE)
    return frame["endtz"].map(
        lambda value: (
            (timestamp := parse_end_timestamp(value)) is not None
            and timestamp < current_time
        )
    ).astype(bool)


def parse_end_dates(frame: gpd.GeoDataFrame) -> pd.Series:
    """Return endtz values as local calendar dates, preserving naive dates."""

    def parse_value(value: object):
        timestamp = parse_end_timestamp(value)
        return timestamp.date() if timestamp is not None else None

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


def filter_by_city(
    frame: gpd.GeoDataFrame,
    selected_city: str,
) -> gpd.GeoDataFrame:
    """Return road closures matching one normalized city value."""
    city_values = frame["city"].fillna("").astype(str).str.strip()
    return frame.loc[city_values == selected_city].copy()


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


@st.cache_data(show_spinner=False)
def load_city_boundaries(path: str, modified_ns: int) -> gpd.GeoDataFrame:
    layer_rows = pyogrio.list_layers(path)
    if len(layer_rows) == 0:
        raise ValueError("The city boundary GeoPackage does not contain a layer.")

    layer_name = str(layer_rows[0][0])
    boundaries = gpd.read_file(path, layer=layer_name, engine="pyogrio")
    if boundaries.empty:
        raise ValueError("The city boundary layer is empty.")
    if boundaries.crs is None:
        raise ValueError("The city boundary layer has no coordinate reference system.")

    boundaries = boundaries.to_crs(epsg=4326)
    boundaries = boundaries[
        boundaries.geometry.notna() & ~boundaries.geometry.is_empty
    ].copy()
    boundaries = boundaries[
        boundaries.geometry.geom_type.isin(["Polygon", "MultiPolygon"])
    ].copy()
    if boundaries.empty:
        raise ValueError("The city boundary layer has no polygon features.")

    keep_columns = [
        column
        for column in ("CITY_NME", "STT_NME", boundaries.geometry.name)
        if column in boundaries.columns
    ]
    return boundaries[keep_columns]


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
    safe_frame["_dashboard_expired"] = expiration_flags(safe_frame)
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
    city_boundaries: gpd.GeoDataFrame | None,
    latitude: float,
    longitude: float,
    target_selected: bool,
    report_pin_active: bool,
) -> folium.Map:
    map_object = folium.Map(
        location=[latitude, longitude],
        zoom_start=17 if report_pin_active else (15 if target_selected else 14),
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

    if city_boundaries is not None and not city_boundaries.empty:
        boundary_geometry = city_boundaries[[city_boundaries.geometry.name]]
        folium.GeoJson(
            data=json.loads(boundary_geometry.to_json(drop_id=True)),
            name="City boundaries",
            style_function=lambda _feature: {
                "color": "#176b5b",
                "weight": 2.5,
                "opacity": 0.9,
                "fillColor": "#176b5b",
                "fillOpacity": 0.035,
            },
            highlight_function=lambda _feature: {
                "color": "#0d4f43",
                "weight": 4,
                "opacity": 1,
                "fillOpacity": 0.07,
            },
        ).add_to(map_object)

    if frame is not None and not frame.empty:
        def closure_style(feature: dict) -> dict:
            expired = bool(
                feature.get("properties", {}).get("_dashboard_expired", False)
            )
            color = "#7c8582" if expired else "#e63b2e"
            return {
                "color": color,
                "weight": 5,
                "opacity": 0.82 if expired else 0.94,
                "fillColor": color,
                "fillOpacity": 0.10 if expired else 0.14,
                "dashArray": "1 10",
                "lineCap": "round",
                "lineJoin": "round",
            }

        def closure_highlight_style(feature: dict) -> dict:
            expired = bool(
                feature.get("properties", {}).get("_dashboard_expired", False)
            )
            return {
                "color": "#545c59" if expired else "#b82017",
                "weight": 7,
                "opacity": 1,
            }

        layer = folium.GeoJson(
            data=geojson_data(frame),
            name="Active road closures",
            style_function=closure_style,
            highlight_function=closure_highlight_style,
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

        if not target_selected and not report_pin_active:
            min_x, min_y, max_x, max_y = frame.total_bounds
            map_object.fit_bounds([[min_y, min_x], [max_y, max_x]], padding=(28, 28))

    if (
        (city_boundaries is not None and not city_boundaries.empty)
        or (frame is not None and not frame.empty)
    ):
        folium.LayerControl(collapsed=True).add_to(map_object)

    if target_selected and not report_pin_active:
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

    if report_pin_active:
        report_marker = folium.Marker(
            location=[latitude, longitude],
            tooltip="Drag this pin to the exact road-closure location",
            icon=folium.Icon(color="red", icon="map-marker", prefix="fa"),
            draggable=True,
            auto_pan=True,
            z_index_offset=2000,
        )
        report_marker.add_to(map_object)
        DraggableMarkerBridge(report_marker, map_object).add_to(map_object)

    return map_object


DATA_DIR.mkdir(parents=True, exist_ok=True)
st_autorefresh(
    interval=REFRESH_INTERVAL_SECONDS * 1_000,
    key="roadwatch-file-watch",
)

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
if "city_filter_active" not in st.session_state:
    st.session_state.city_filter_active = False
if "city_filter_value" not in st.session_state:
    st.session_state.city_filter_value = "All cities"
if "report_preview" not in st.session_state:
    st.session_state.report_preview = None
if "report_pin_active" not in st.session_state:
    st.session_state.report_pin_active = False
if "report_coordinate_text" not in st.session_state:
    st.session_state.report_coordinate_text = (
        f"{DEFAULT_LATITUDE}, {DEFAULT_LONGITUDE}"
    )
if "report_latitude" not in st.session_state:
    st.session_state.report_latitude = DEFAULT_LATITUDE
if "report_longitude" not in st.session_state:
    st.session_state.report_longitude = DEFAULT_LONGITUDE
if "report_city" not in st.session_state:
    st.session_state.report_city = ""
if "pending_report_location" not in st.session_state:
    st.session_state.pending_report_location = None


frame: gpd.GeoDataFrame | None = None
metadata = {"layers": [], "feature_count": 0, "source_version": "0"}
load_error: str | None = None
source_warning: str | None = None
source_mode = "geopackage"
city_boundaries: gpd.GeoDataFrame | None = None
city_boundary_error: str | None = None
city_boundary_version = "missing"
database_settings: dict[str, str] | None = None
google_sheets_settings: dict | None = None
try:
    database_settings = get_database_settings()
except ValueError as error:
    source_warning = str(error)

try:
    google_sheets_settings = get_google_sheets_settings()
except ValueError as error:
    st.warning(str(error))

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

if CITY_BOUNDARY_FILE.exists():
    try:
        city_boundary_version = str(CITY_BOUNDARY_FILE.stat().st_mtime_ns)
        city_boundaries = load_city_boundaries(
            str(CITY_BOUNDARY_FILE),
            CITY_BOUNDARY_FILE.stat().st_mtime_ns,
        )
    except Exception as error:
        city_boundary_error = str(error)

pending_report_location = st.session_state.pop("pending_report_location", None)
if pending_report_location is not None:
    pending_latitude, pending_longitude = pending_report_location
    st.session_state.report_latitude = pending_latitude
    st.session_state.report_longitude = pending_longitude
    st.session_state.report_coordinate_text = (
        f"{pending_latitude:.8f}, {pending_longitude:.8f}"
    )
    st.session_state.report_city = city_for_coordinate(
        city_boundaries,
        pending_latitude,
        pending_longitude,
    )

end_dates: pd.Series | None = None
valid_end_dates = pd.Series(dtype="object")
if frame is not None and "endtz" in frame.columns:
    end_dates = parse_end_dates(frame)
    valid_end_dates = end_dates.dropna()

city_options = ["All cities"]
if frame is not None and "city" in frame.columns:
    city_names = sorted(
        {
            str(value).strip()
            for value in frame["city"].dropna()
            if str(value).strip()
        },
        key=str.casefold,
    )
    city_options.extend(city_names)

if st.session_state.city_filter_value not in city_options:
    st.session_state.city_filter_value = "All cities"
    st.session_state.city_filter_active = False

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

    location_tab, date_filter_tab, report_tab, upload_tab = st.tabs(
        ["Lat / Long", "Filters", "Report closure", "Admin upload"]
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
              <div class="rw-tool-icon">⌖</div>
              <div>
                <strong>Filter by city</strong>
                <span>Show road closures for one city.</span>
              </div>
            </div>
            """
        )
        if frame is None:
            st.info("Connect or publish a data source to use city filters.")
        elif "city" not in frame.columns:
            st.warning("The active data source does not contain a city column.")
        elif len(city_options) == 1:
            st.warning("No valid city names were found in the city column.")
        else:
            with st.form("city_filter_form", border=False):
                st.selectbox(
                    "City",
                    options=city_options,
                    key="city_filter_value",
                )
                city_filter_submitted = st.form_submit_button(
                    "Apply city filter",
                    type="primary",
                    width="stretch",
                )

            if city_filter_submitted:
                st.session_state.city_filter_active = (
                    st.session_state.city_filter_value != "All cities"
                )

            if st.session_state.city_filter_active:
                st.html(
                    f"""
                    <div class="rw-filter-card">
                      <span>Active city filter</span>
                      <strong>{html.escape(st.session_state.city_filter_value)}</strong>
                    </div>
                    """
                )
                if st.button(
                    "Clear city filter",
                    width="stretch",
                    key="clear_city_filter",
                ):
                    st.session_state.city_filter_active = False
                    st.session_state.city_filter_value = "All cities"
                    st.rerun()

        st.divider()
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

    with report_tab:
        render_road_closure_report_form(
            google_sheets_settings,
            city_boundaries,
        )

    with upload_tab:
        if database_settings is not None:
            if source_mode == "postgresql":
                st.success("PostgreSQL / PostGIS is connected.")
            else:
                st.warning("Database unavailable; GeoPackage fallback is active.")
            st.info(
                "Edit attributes in pgAdmin and commit the transaction. "
                f"The dashboard checks for changes every "
                f"{REFRESH_INTERVAL_LABEL}."
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
                f"New files are checked automatically every "
                f"{REFRESH_INTERVAL_LABEL}. "
                "Cloud deployments require persistent storage for durable uploads."
            )

    if source_warning:
        st.warning(source_warning)
    if load_error and load_error != source_warning:
        st.error(f"The road closure data could not be read: {load_error}")
    if city_boundary_error:
        st.warning(
            f"The city boundary layer could not be read: {city_boundary_error}"
        )


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

if (
    display_frame is not None
    and "city" in display_frame.columns
    and st.session_state.city_filter_active
):
    display_frame = filter_by_city(
        display_frame,
        st.session_state.city_filter_value,
    )

visible_feature_count = len(display_frame) if display_frame is not None else 0
inactive_feature_count = (
    int(expiration_flags(display_frame).sum())
    if display_frame is not None
    else 0
)
active_feature_count = visible_feature_count - inactive_feature_count
total_feature_count = metadata["feature_count"]
date_filter_active = bool(
    frame is not None
    and end_dates is not None
    and st.session_state.date_filter_active
)
city_filter_active = bool(
    frame is not None
    and "city" in frame.columns
    and st.session_state.city_filter_active
)
filters_active = date_filter_active or city_filter_active
status_text = (
    (
        f"{visible_feature_count:,} of {total_feature_count:,} "
        "closure features visible"
        if filters_active
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

map_latitude = (
    st.session_state.report_latitude
    if st.session_state.report_pin_active
    else st.session_state.target_latitude
)
map_longitude = (
    st.session_state.report_longitude
    if st.session_state.report_pin_active
    else st.session_state.target_longitude
)
closure_map = build_map(
    frame=display_frame,
    city_boundaries=city_boundaries,
    latitude=map_latitude,
    longitude=map_longitude,
    target_selected=st.session_state.target_selected,
    report_pin_active=st.session_state.report_pin_active,
)
map_filter_key = (
    f"{st.session_state.date_filter_operator}-"
    f"{st.session_state.date_filter_date.isoformat()}"
    if date_filter_active
    else "all"
)
city_filter_key = (
    hashlib.sha1(
        st.session_state.city_filter_value.encode("utf-8")
    ).hexdigest()[:10]
    if city_filter_active
    else "all-cities"
)
map_result = st_folium(
    closure_map,
    width=None,
    height=800,
    returned_objects=(
        ["last_active_drawing"]
        if st.session_state.report_pin_active
        else []
    ),
    key=(
        "closure-map-"
        f'{metadata.get("source_version", "0")}-'
        f"{city_boundary_version}-"
        f"{map_filter_key}-"
        f"{city_filter_key}-"
        f"pin-{int(st.session_state.report_pin_active)}"
    ),
)

if st.session_state.report_pin_active:
    moved_pin = map_result.get("last_active_drawing")
    moved_geometry = moved_pin.get("geometry", {}) if moved_pin else {}
    moved_coordinates = moved_geometry.get("coordinates", [])
    if moved_geometry.get("type") == "Point" and len(moved_coordinates) >= 2:
        moved_longitude = float(moved_coordinates[0])
        moved_latitude = float(moved_coordinates[1])
        coordinate_changed = (
            abs(moved_latitude - st.session_state.report_latitude) > 1e-9
            or abs(moved_longitude - st.session_state.report_longitude) > 1e-9
        )
        if coordinate_changed:
            st.session_state.pending_report_location = (
                moved_latitude,
                moved_longitude,
            )
            st.rerun()

st.html(
    f"""
    <div class="rw-feature-count">
      <div class="rw-feature-row">
        <span class="rw-closure-swatch active" aria-hidden="true"></span>
        <div class="rw-feature-copy">
          <strong>{active_feature_count:,}</strong>
          <span>Active road closures</span>
        </div>
      </div>
      <div class="rw-feature-row">
        <span class="rw-closure-swatch inactive" aria-hidden="true"></span>
        <div class="rw-feature-copy">
          <strong>{inactive_feature_count:,}</strong>
          <span>Inactive road closures</span>
        </div>
      </div>
    </div>
    """
)
