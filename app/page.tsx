"use client";

import dynamic from "next/dynamic";
import { useCallback, useEffect, useRef, useState } from "react";
import type { FeatureCollection } from "geojson";
import { parseGeoPackage } from "./lib/geopackage";

const ClosureMap = dynamic(() => import("./components/ClosureMap"), {
  ssr: false,
  loading: () => <div className="map-loading">Preparing the road map…</div>,
});

type FileStatus = {
  exists: boolean;
  canUpload?: boolean;
  etag?: string;
  updatedAt?: string;
  fileName?: string;
  size?: number;
};

type Coordinates = { lat: number; lng: number; nonce: number };

const EMPTY_COLLECTION: FeatureCollection = {
  type: "FeatureCollection",
  features: [],
};

function formatBytes(value?: number) {
  if (!value) return "—";
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

function formatTime(value?: string) {
  if (!value) return "No file published";
  return new Intl.DateTimeFormat("en", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

export default function Home() {
  const [features, setFeatures] = useState<FeatureCollection>(EMPTY_COLLECTION);
  const [status, setStatus] = useState<FileStatus>({ exists: false });
  const [activeTab, setActiveTab] = useState<"location" | "upload">("location");
  const [coordinates, setCoordinates] = useState<Coordinates>({
    lat: 20.5937,
    lng: 78.9629,
    nonce: 0,
  });
  const [lat, setLat] = useState("20.5937");
  const [lng, setLng] = useState("78.9629");
  const [message, setMessage] = useState("Waiting for a published GeoPackage");
  const [isBusy, setIsBusy] = useState(false);
  const [parseProgress, setParseProgress] = useState("");
  const etagRef = useRef<string | undefined>(undefined);

  const loadPublishedFile = useCallback(async (nextStatus?: FileStatus) => {
    try {
      setParseProgress("Reading published road closures…");
      const response = await fetch("/api/closures", { cache: "no-store" });
      if (response.status === 404) {
        setFeatures(EMPTY_COLLECTION);
        setMessage("Upload a .gpkg file to publish the first road closures");
        return;
      }
      if (!response.ok) throw new Error("The published file could not be downloaded.");

      const parsed = await parseGeoPackage(await response.arrayBuffer());
      setFeatures(parsed);
      const count = parsed.features.length;
      setMessage(`${count.toLocaleString()} closure feature${count === 1 ? "" : "s"} visible`);
      if (nextStatus) setStatus(nextStatus);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "The GeoPackage could not be read.");
    } finally {
      setParseProgress("");
    }
  }, []);

  const checkForUpdates = useCallback(async () => {
    try {
      const response = await fetch("/api/closures/status", { cache: "no-store" });
      if (!response.ok) return;
      const nextStatus = (await response.json()) as FileStatus;
      setStatus(nextStatus);
      if (nextStatus.exists && nextStatus.etag !== etagRef.current) {
        etagRef.current = nextStatus.etag;
        await loadPublishedFile(nextStatus);
      } else if (!nextStatus.exists) {
        setMessage("Upload a .gpkg file to publish the first road closures");
      }
    } catch {
      // A missed poll should not interrupt the map; the next poll will retry.
    }
  }, [loadPublishedFile]);

  useEffect(() => {
    void checkForUpdates();
    const timer = window.setInterval(() => void checkForUpdates(), 30_000);
    return () => window.clearInterval(timer);
  }, [checkForUpdates]);

  function goToLocation(event: React.FormEvent) {
    event.preventDefault();
    const nextLat = Number(lat);
    const nextLng = Number(lng);
    if (!Number.isFinite(nextLat) || nextLat < -90 || nextLat > 90) {
      setMessage("Latitude must be between −90 and 90.");
      return;
    }
    if (!Number.isFinite(nextLng) || nextLng < -180 || nextLng > 180) {
      setMessage("Longitude must be between −180 and 180.");
      return;
    }
    setCoordinates({ lat: nextLat, lng: nextLng, nonce: Date.now() });
    setMessage(`Map centred at ${nextLat.toFixed(5)}, ${nextLng.toFixed(5)}`);
  }

  async function uploadFile(event: React.ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    if (!file.name.toLowerCase().endsWith(".gpkg")) {
      setMessage("Choose a GeoPackage file ending in .gpkg.");
      return;
    }
    if (file.size > 50 * 1024 * 1024) {
      setMessage("The GeoPackage must be 50 MB or smaller.");
      return;
    }

    setIsBusy(true);
    setMessage(`Publishing ${file.name}…`);
    try {
      // Validate before replacing the shared file.
      const parsed = await parseGeoPackage(await file.arrayBuffer());
      const response = await fetch("/api/closures", {
        method: "PUT",
        headers: {
          "Content-Type": "application/geopackage+sqlite3",
          "X-File-Name": encodeURIComponent(file.name),
        },
        body: file,
      });
      if (!response.ok) {
        const body = (await response.json().catch(() => null)) as { error?: string } | null;
        throw new Error(body?.error || "The file could not be published.");
      }
      const nextStatus = (await response.json()) as FileStatus;
      etagRef.current = nextStatus.etag;
      setStatus(nextStatus);
      setFeatures(parsed);
      setMessage(
        `${parsed.features.length.toLocaleString()} closure feature${
          parsed.features.length === 1 ? "" : "s"
        } published`,
      );
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "The GeoPackage could not be published.");
    } finally {
      setIsBusy(false);
    }
  }

  return (
    <main className="dashboard-shell">
      <header className="topbar">
        <div className="brand-mark" aria-hidden="true"><span /></div>
        <div className="brand-copy">
          <strong>RoadWatch</strong>
          <span>Active closure dashboard</span>
        </div>
        <div className="live-chip">
          <span className="live-dot" aria-hidden="true" />
          LIVE
        </div>
        <div className="header-status">
          <span>Last update</span>
          <strong>{status.exists ? formatTime(status.updatedAt) : "Not published yet"}</strong>
        </div>
      </header>

      <section className="workspace">
        <aside className="control-panel">
          <div className="panel-heading">
            <p className="eyebrow">Map controls</p>
            <h1>Road closure viewer</h1>
            <p>Navigate by coordinate or publish the latest closure file for everyone.</p>
          </div>

          <div className="tabs" role="tablist" aria-label="Dashboard tools">
            <button
              role="tab"
              aria-selected={activeTab === "location"}
              className={activeTab === "location" ? "active" : ""}
              onClick={() => setActiveTab("location")}
            >
              Lat / Long
            </button>
            <button
              role="tab"
              aria-selected={activeTab === "upload"}
              className={activeTab === "upload" ? "active" : ""}
              onClick={() => setActiveTab("upload")}
            >
              Input file
            </button>
          </div>

          {activeTab === "location" ? (
            <form className="tool-content" onSubmit={goToLocation}>
              <div className="tool-intro">
                <span className="tool-icon location-icon" aria-hidden="true" />
                <div>
                  <h2>Go to a location</h2>
                  <p>Enter WGS84 decimal coordinates.</p>
                </div>
              </div>
              <label>
                Latitude
                <input
                  inputMode="decimal"
                  value={lat}
                  onChange={(event) => setLat(event.target.value)}
                  placeholder="e.g. 19.0760"
                  aria-label="Latitude"
                />
              </label>
              <label>
                Longitude
                <input
                  inputMode="decimal"
                  value={lng}
                  onChange={(event) => setLng(event.target.value)}
                  placeholder="e.g. 72.8777"
                  aria-label="Longitude"
                />
              </label>
              <button className="primary-button" type="submit">Go to location</button>
              <div className="coordinate-card">
                <span>Current target</span>
                <strong>{coordinates.lat.toFixed(5)}, {coordinates.lng.toFixed(5)}</strong>
              </div>
            </form>
          ) : (
            <div className="tool-content">
              <div className="tool-intro">
                <span className="tool-icon upload-icon" aria-hidden="true">↑</span>
                <div>
                  <h2>Publish GeoPackage</h2>
                  <p>Uploading replaces the current shared closure layer.</p>
                </div>
              </div>
              {status.canUpload ? (
                <label className={`drop-zone ${isBusy ? "disabled" : ""}`}>
                  <input
                    type="file"
                    accept=".gpkg,application/geopackage+sqlite3"
                    onChange={uploadFile}
                    disabled={isBusy}
                  />
                  <span className="drop-symbol" aria-hidden="true">＋</span>
                  <strong>{isBusy ? "Publishing…" : "Choose a .gpkg file"}</strong>
                  <small>Maximum file size: 50 MB</small>
                </label>
              ) : (
                <div className="drop-zone admin-lock">
                  <span className="drop-symbol lock-symbol" aria-hidden="true">•</span>
                  <strong>Administrator upload</strong>
                  <small>Sign in with the dashboard owner account to replace the shared file.</small>
                  <a className="secondary-button" href="/signin-with-chatgpt?return_to=%2F">
                    Sign in to upload
                  </a>
                </div>
              )}
              <div className="published-file">
                <span className="file-badge" aria-hidden="true">GPKG</span>
                <div>
                  <strong>{status.fileName || "No file published"}</strong>
                  <span>{status.exists ? `${formatBytes(status.size)} · ${formatTime(status.updatedAt)}` : "Your uploaded layer will appear here"}</span>
                </div>
              </div>
              <p className="upload-note">
                New uploads automatically appear for all viewers within 30 seconds.
              </p>
            </div>
          )}

          <div className="panel-footer">
            <span className={status.exists ? "status-ok" : "status-waiting"} aria-hidden="true" />
            <p>{parseProgress || message}</p>
          </div>
        </aside>

        <section className="map-stage" aria-label="Active road closure map">
          <ClosureMap features={features} target={coordinates} />
          <div className="map-overlay map-title">
            <span>NETWORK VIEW</span>
            <strong>Active road closures</strong>
          </div>
          <div className="map-overlay feature-count">
            <span className="closure-swatch" />
            <div>
              <strong>{features.features.length.toLocaleString()}</strong>
              <span>Closure features</span>
            </div>
          </div>
        </section>
      </section>
    </main>
  );
}
