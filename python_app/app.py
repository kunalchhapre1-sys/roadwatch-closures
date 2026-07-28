from __future__ import annotations

import hashlib
import html
import json
from datetime import datetime
from pathlib import Path

import folium
import geopandas as gpd
import pandas as pd
import pyogrio
import streamlit as st
from branca.element import MacroElement, Template
from streamlit_autorefresh import st_autorefresh
from streamlit_folium import st_folium


APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
ACTIVE_FILE = DATA_DIR / "current.gpkg"
MAX_FILE_SIZE = 50 * 1024 * 1024
DEFAULT_LATITUDE = 12.881703576462842
DEFAULT_LONGITUDE = 77.75966530609753


st.set_page_config(
    page_title="RoadWatch | Active road closures",
    page_icon="🚧",
    layout="wide",
    initial_sidebar_state="expanded",
)


st.html(f"<style>{(APP_DIR / 'styles.css').read_text(encoding='utf-8')}</style>")


def format_size(size: int) -> str:
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


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


@st.cache_data(show_spinner=False)
def load_geopackage(path: str, modified_ns: int) -> tuple[gpd.GeoDataFrame, dict]:
    del modified_ns  # Included only to invalidate the cache after file replacement.
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
    metadata = {"layers": layer_names, "feature_count": len(combined)}
    return combined, metadata


def geojson_data(frame: gpd.GeoDataFrame) -> dict:
    safe_frame = frame.copy()
    for column in safe_frame.columns:
        if column == safe_frame.geometry.name:
            continue
        safe_frame[column] = safe_frame[column].map(
            lambda value: value.isoformat()
            if isinstance(value, (datetime, pd.Timestamp))
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


frame: gpd.GeoDataFrame | None = None
metadata = {"layers": [], "feature_count": 0}
load_error: str | None = None
if ACTIVE_FILE.exists():
    try:
        frame, metadata = load_geopackage(
            str(ACTIVE_FILE),
            ACTIVE_FILE.stat().st_mtime_ns,
        )
    except Exception as error:
        load_error = str(error)


file_name = ACTIVE_FILE.name if ACTIVE_FILE.exists() else "No file loaded"
file_size = format_size(ACTIVE_FILE.stat().st_size) if ACTIVE_FILE.exists() else "—"
updated_time = (
    datetime.fromtimestamp(ACTIVE_FILE.stat().st_mtime).strftime("%d %b %Y, %I:%M %p")
    if ACTIVE_FILE.exists()
    else "Waiting for upload"
)

with st.sidebar:
    st.html(
        """
        <div class="rw-panel-heading">
          <p class="rw-eyebrow">Map controls</p>
          <h1>Road closure viewer</h1>
          <p>Navigate by coordinate or publish the latest closure file for everyone.</p>
        </div>
        """
    )

    location_tab, upload_tab = st.tabs(["Lat / Long", "Input file"])
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

    with upload_tab:
        st.html(
            """
            <div class="rw-tool-intro">
              <div class="rw-tool-icon">↑</div>
              <div>
                <strong>Publish GeoPackage</strong>
                <span>Uploading replaces the current closure layer.</span>
              </div>
            </div>
            """
        )
        uploaded_file = st.file_uploader(
            "Choose a GeoPackage",
            type=["gpkg"],
            help="Maximum file size: 50 MB.",
        )
        if uploaded_file is not None:
            contents = uploaded_file.getvalue()
            digest = hashlib.sha256(contents).hexdigest()
            if st.session_state.get("uploaded_digest") != digest:
                if len(contents) > MAX_FILE_SIZE:
                    st.error("The GeoPackage must be 50 MB or smaller.")
                else:
                    ACTIVE_FILE.write_bytes(contents)
                    st.session_state.uploaded_digest = digest
                    st.cache_data.clear()
                    st.success(f"{uploaded_file.name} is now active.")
                    st.rerun()
        st.html(
            f"""
            <div class="rw-file-card">
              <strong>{html.escape(file_name)}</strong>
              <span>{file_size} · {updated_time}</span>
            </div>
            """
        )
        st.caption(
            "New files are checked automatically every 30 seconds. "
            "Cloud deployments require persistent storage for durable uploads."
        )

    if load_error:
        st.error(f"The GeoPackage could not be read: {load_error}")


status_text = (
    f'{metadata["feature_count"]:,} closure features visible'
    if frame is not None and not load_error
    else "Waiting for a published GeoPackage"
)
status_class = "ready" if frame is not None and not load_error else ""

st.html(
    f"""
    <div class="rw-topbar">
      <div class="rw-brand-mark" aria-hidden="true"><span></span></div>
      <div class="rw-brand-copy">
        <strong>RoadWatch</strong>
        <span>Active closure dashboard</span>
      </div>
      <div class="rw-live-chip"><span class="rw-live-dot"></span>LIVE</div>
      <div class="rw-header-status">
        <span>Last update</span>
        <strong>{updated_time}</strong>
      </div>
    </div>
    <div class="rw-panel-footer">
      <span class="rw-status-dot {status_class}"></span>
      <span>{html.escape(status_text)}</span>
    </div>
    <div class="rw-map-title">
      <span>NETWORK VIEW</span>
      <strong>Active road closures</strong>
    </div>
    """
)

closure_map = build_map(
    frame=frame,
    latitude=st.session_state.target_latitude,
    longitude=st.session_state.target_longitude,
    target_selected=st.session_state.target_selected,
)
st_folium(
    closure_map,
    width=None,
    height=800,
    returned_objects=[],
    key=f"closure-map-{ACTIVE_FILE.stat().st_mtime_ns if ACTIVE_FILE.exists() else 0}",
)

st.html(
    f"""
    <div class="rw-feature-count">
      <span class="rw-closure-swatch"></span>
      <div class="rw-feature-copy">
        <strong>{metadata["feature_count"]:,}</strong>
        <span>Closure features</span>
      </div>
    </div>
    <div class="rw-creator-credit">
      Designed &amp; created by <strong>Kunal Chhapre</strong>
    </div>
    """
)
