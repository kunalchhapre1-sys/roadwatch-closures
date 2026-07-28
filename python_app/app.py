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
from folium.plugins import Fullscreen
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


st.markdown(
    """
    <style>
      :root {
        --ink: #15231f;
        --muted: #60706a;
        --green: #176b5b;
        --red: #e63b2e;
        --paper: #f5f7f3;
      }
      .stApp { background: var(--paper); color: var(--ink); }
      [data-testid="stHeader"] { background: transparent; }
      [data-testid="stSidebar"] {
        background: #ffffff;
        border-right: 1px solid #dfe6e1;
      }
      [data-testid="stSidebar"] h1 {
        color: var(--ink);
        letter-spacing: -0.03em;
      }
      .block-container {
        max-width: none;
        padding: 1.25rem 1.5rem 2.5rem;
      }
      .roadwatch-header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 1rem;
        padding: .9rem 1.15rem;
        margin-bottom: 1rem;
        border: 1px solid #dfe6e1;
        border-radius: 16px;
        background: rgba(255,255,255,.92);
        box-shadow: 0 10px 30px rgba(21,35,31,.06);
      }
      .roadwatch-brand { display: flex; align-items: center; gap: .75rem; }
      .roadwatch-mark {
        width: 38px;
        height: 38px;
        display: grid;
        place-items: center;
        border-radius: 12px;
        color: white;
        background: var(--green);
        font-size: 1.15rem;
      }
      .roadwatch-brand strong { display: block; font-size: 1.05rem; }
      .roadwatch-brand span { color: var(--muted); font-size: .78rem; }
      .live-chip {
        display: inline-flex;
        align-items: center;
        gap: .45rem;
        padding: .42rem .7rem;
        border-radius: 999px;
        background: #e9f6f1;
        color: var(--green);
        font-size: .75rem;
        font-weight: 800;
        letter-spacing: .08em;
      }
      .live-dot {
        width: 8px;
        height: 8px;
        border-radius: 50%;
        background: #1e9b73;
        box-shadow: 0 0 0 4px rgba(30,155,115,.12);
      }
      .metric-row {
        display: grid;
        grid-template-columns: repeat(3, minmax(0, 1fr));
        gap: .75rem;
        margin-bottom: 1rem;
      }
      .metric-card {
        padding: .85rem 1rem;
        border: 1px solid #dfe6e1;
        border-radius: 14px;
        background: #fff;
      }
      .metric-card span {
        display: block;
        color: var(--muted);
        font-size: .72rem;
        text-transform: uppercase;
        letter-spacing: .06em;
      }
      .metric-card strong {
        display: block;
        margin-top: .2rem;
        font-size: 1.1rem;
      }
      iframe { border-radius: 16px; }
      .creator-credit {
        position: fixed;
        right: 18px;
        bottom: 10px;
        z-index: 9999;
        padding: .45rem .7rem;
        border: 1px solid rgba(23,107,91,.18);
        border-radius: 10px;
        background: rgba(255,255,255,.92);
        color: var(--muted);
        font-size: .68rem;
        box-shadow: 0 8px 24px rgba(21,35,31,.1);
      }
      .creator-credit strong { color: var(--ink); }
      @media (max-width: 720px) {
        .metric-row { grid-template-columns: 1fr; }
        .block-container { padding: .75rem .6rem 2.5rem; }
      }
    </style>
    """,
    unsafe_allow_html=True,
)


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
    Fullscreen(position="topright").add_to(map_object)

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


with st.sidebar:
    st.caption("MAP CONTROLS")
    st.title("Road closure viewer")
    st.write(
        "Navigate by coordinate or replace the local GeoPackage displayed on the map."
    )

    location_tab, upload_tab = st.tabs(["Lat / Long", "Input file"])
    with location_tab:
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
        st.caption(
            "Current target: "
            f"{st.session_state.target_latitude}, "
            f"{st.session_state.target_longitude}"
        )

    with upload_tab:
        uploaded_file = st.file_uploader(
            "Choose a GeoPackage",
            type=["gpkg"],
            help="Uploading replaces python_app/data/current.gpkg.",
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
        st.caption(
            "You can also replace `python_app/data/current.gpkg` directly. "
            "The dashboard checks for changes every 30 seconds."
        )


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


st.markdown(
    """
    <div class="roadwatch-header">
      <div class="roadwatch-brand">
        <div class="roadwatch-mark">◆</div>
        <div><strong>RoadWatch</strong><span>Active closure dashboard</span></div>
      </div>
      <div class="live-chip"><span class="live-dot"></span>LIVE · LOCAL</div>
    </div>
    """,
    unsafe_allow_html=True,
)

file_name = ACTIVE_FILE.name if ACTIVE_FILE.exists() else "No file loaded"
file_size = format_size(ACTIVE_FILE.stat().st_size) if ACTIVE_FILE.exists() else "—"
updated_time = (
    datetime.fromtimestamp(ACTIVE_FILE.stat().st_mtime).strftime("%d %b %Y, %I:%M %p")
    if ACTIVE_FILE.exists()
    else "Waiting for upload"
)
st.markdown(
    f"""
    <div class="metric-row">
      <div class="metric-card"><span>Closure features</span>
        <strong>{metadata["feature_count"]:,}</strong></div>
      <div class="metric-card"><span>Published file</span>
        <strong>{html.escape(file_name)} · {file_size}</strong></div>
      <div class="metric-card"><span>Last update</span>
        <strong>{updated_time}</strong></div>
    </div>
    """,
    unsafe_allow_html=True,
)

if load_error:
    st.error(f"The GeoPackage could not be read: {load_error}")
elif frame is None:
    st.info("Upload a `.gpkg` file from the **Input file** tab to show closures.")
else:
    st.caption(
        "Click a red closure feature to see its Latitude, Longitude, City, and End date."
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
    height=690,
    returned_objects=[],
    key=f"closure-map-{ACTIVE_FILE.stat().st_mtime_ns if ACTIVE_FILE.exists() else 0}",
)

st.markdown(
    """
    <div class="creator-credit">
      Designed &amp; created by- <strong>Kunal Chhapre</strong>
    </div>
    """,
    unsafe_allow_html=True,
)
