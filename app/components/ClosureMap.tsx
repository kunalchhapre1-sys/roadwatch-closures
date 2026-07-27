"use client";

import { useEffect } from "react";
import { CircleMarker, GeoJSON, MapContainer, TileLayer, useMap } from "react-leaflet";
import type { Feature, FeatureCollection, GeoJsonObject } from "geojson";
import type { Layer, PathOptions } from "leaflet";
import L from "leaflet";
import "leaflet/dist/leaflet.css";

type Target = { lat: number; lng: number; nonce: number };

function FlyToTarget({ target }: { target: Target }) {
  const map = useMap();
  useEffect(() => {
    if (target.nonce) map.flyTo([target.lat, target.lng], 15, { duration: 1.2 });
  }, [map, target]);
  return null;
}

function FitClosures({ features }: { features: FeatureCollection }) {
  const map = useMap();
  useEffect(() => {
    if (!features.features.length) return;
    import("leaflet").then((L) => {
      const layer = L.geoJSON(features as GeoJsonObject);
      const bounds = layer.getBounds();
      if (bounds.isValid()) map.fitBounds(bounds.pad(0.12), { maxZoom: 14 });
    });
  }, [features, map]);
  return null;
}

function popupForFeature(feature: Feature, layer: Layer) {
  const props = feature.properties || {};
  const preferred = [
    "city",
    "endtz",
    "name",
    "road_name",
    "road",
    "status",
    "reason",
    "description",
    "start_date",
    "end_date",
  ];
  const labels: Record<string, string> = {
    city: "City",
    endtz: "End date",
    road_name: "Road name",
    start_date: "Start date",
    end_date: "End date",
  };
  const escapeHtml = (value: unknown) =>
    String(value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  const rows = preferred
    .filter((key) => props[key] !== undefined && props[key] !== null)
    .slice(0, 8)
    .map(
      (key) =>
        `<dt>${labels[key] || key.replaceAll("_", " ")}</dt><dd>${escapeHtml(props[key])}</dd>`,
    )
    .join("");
  if (rows && "bindPopup" in layer && typeof layer.bindPopup === "function") {
    layer.bindPopup(`<div class="closure-popup"><strong>Road closure</strong><dl>${rows}</dl></div>`);
  }
}

const closureStyle: PathOptions = {
  color: "#e63b2e",
  weight: 5,
  opacity: 0.94,
  fillColor: "#ef4b3e",
  fillOpacity: 0.28,
  lineCap: "round",
  lineJoin: "round",
};

export default function ClosureMap({
  features,
  target,
}: {
  features: FeatureCollection;
  target: Target;
}) {
  return (
    <MapContainer
      center={[12.881703576462842, 77.75966530609753]}
      zoom={14}
      minZoom={2}
      className="leaflet-map"
      zoomControl
    >
      <TileLayer
        attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
        url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
      />
      <GeoJSON
        key={`${features.features.length}-${target.nonce}`}
        data={features}
        style={closureStyle}
        pointToLayer={(_feature, latlng) => (
          new L.CircleMarker(latlng, {
            radius: 7,
            ...closureStyle,
          })
        )}
        onEachFeature={popupForFeature}
      />
      {target.nonce > 0 && (
        <CircleMarker
          center={[target.lat, target.lng]}
          radius={9}
          pathOptions={{ color: "#ffffff", weight: 3, fillColor: "#176b5b", fillOpacity: 1 }}
        />
      )}
      <FitClosures features={features} />
      <FlyToTarget target={target} />
    </MapContainer>
  );
}
