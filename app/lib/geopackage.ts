import initSqlJs from "sql.js";
import type { Database } from "sql.js";
import type { Feature, FeatureCollection, Geometry, Position } from "geojson";

type GeometryWithCursor = { geometry: Geometry; offset: number };

function readPoint(view: DataView, offset: number, littleEndian: boolean, dimensions: number) {
  const coordinates: number[] = [];
  for (let index = 0; index < dimensions; index += 1) {
    coordinates.push(view.getFloat64(offset + index * 8, littleEndian));
  }
  return { coordinates: coordinates.slice(0, Math.min(dimensions, 3)), offset: offset + dimensions * 8 };
}

function decodeWkb(view: DataView, offset = 0): GeometryWithCursor {
  const littleEndian = view.getUint8(offset) === 1;
  const rawType = view.getUint32(offset + 1, littleEndian);
  const isoDimension = Math.floor(rawType / 1000);
  const hasZ = Boolean(rawType & 0x80000000) || isoDimension === 1 || isoDimension === 3;
  const hasM = Boolean(rawType & 0x40000000) || isoDimension === 2 || isoDimension === 3;
  const baseType = rawType & 0xff;
  const dimensions = 2 + Number(hasZ) + Number(hasM);
  let cursor = offset + 5;

  if (rawType & 0x20000000) cursor += 4;

  if (baseType === 1) {
    const point = readPoint(view, cursor, littleEndian, dimensions);
    return { geometry: { type: "Point", coordinates: point.coordinates }, offset: point.offset };
  }

  if (baseType === 2) {
    const count = view.getUint32(cursor, littleEndian);
    cursor += 4;
    const coordinates: Position[] = [];
    for (let index = 0; index < count; index += 1) {
      const point = readPoint(view, cursor, littleEndian, dimensions);
      coordinates.push(point.coordinates);
      cursor = point.offset;
    }
    return { geometry: { type: "LineString", coordinates }, offset: cursor };
  }

  if (baseType === 3) {
    const ringCount = view.getUint32(cursor, littleEndian);
    cursor += 4;
    const coordinates: Position[][] = [];
    for (let ringIndex = 0; ringIndex < ringCount; ringIndex += 1) {
      const pointCount = view.getUint32(cursor, littleEndian);
      cursor += 4;
      const ring: Position[] = [];
      for (let pointIndex = 0; pointIndex < pointCount; pointIndex += 1) {
        const point = readPoint(view, cursor, littleEndian, dimensions);
        ring.push(point.coordinates);
        cursor = point.offset;
      }
      coordinates.push(ring);
    }
    return { geometry: { type: "Polygon", coordinates }, offset: cursor };
  }

  if ([4, 5, 6, 7].includes(baseType)) {
    const count = view.getUint32(cursor, littleEndian);
    cursor += 4;
    const children: Geometry[] = [];
    for (let index = 0; index < count; index += 1) {
      const child = decodeWkb(view, cursor);
      children.push(child.geometry);
      cursor = child.offset;
    }
    if (baseType === 4) {
      return {
        geometry: { type: "MultiPoint", coordinates: children.map((child) => (child as GeoJSON.Point).coordinates) },
        offset: cursor,
      };
    }
    if (baseType === 5) {
      return {
        geometry: { type: "MultiLineString", coordinates: children.map((child) => (child as GeoJSON.LineString).coordinates) },
        offset: cursor,
      };
    }
    if (baseType === 6) {
      return {
        geometry: { type: "MultiPolygon", coordinates: children.map((child) => (child as GeoJSON.Polygon).coordinates) },
        offset: cursor,
      };
    }
    return { geometry: { type: "GeometryCollection", geometries: children }, offset: cursor };
  }

  throw new Error(`Unsupported GeoPackage geometry type: ${baseType}.`);
}

function decodeGeoPackageGeometry(value: Uint8Array): Geometry | null {
  if (value.length < 8 || value[0] !== 0x47 || value[1] !== 0x50) return null;
  const flags = value[3];
  const empty = Boolean(flags & 0x10);
  if (empty) return null;
  const envelopeCode = (flags >> 1) & 0x07;
  const envelopeValues = envelopeCode === 0 ? 0 : envelopeCode === 1 ? 4 : envelopeCode === 2 || envelopeCode === 3 ? 6 : 8;
  const wkbOffset = 8 + envelopeValues * 8;
  return decodeWkb(new DataView(value.buffer, value.byteOffset + wkbOffset, value.byteLength - wkbOffset)).geometry;
}

function queryRows(database: Database, sql: string) {
  const statement = database.prepare(sql);
  const rows: Record<string, unknown>[] = [];
  try {
    while (statement.step()) rows.push(statement.getAsObject());
  } finally {
    statement.free();
  }
  return rows;
}

function quoteIdentifier(identifier: string) {
  return `"${identifier.replaceAll('"', '""')}"`;
}

export async function parseGeoPackage(buffer: ArrayBuffer): Promise<FeatureCollection> {
  const SQL = await initSqlJs({
    locateFile: () => "/sql-wasm.wasm",
  });
  const database = new SQL.Database(new Uint8Array(buffer));
  try {
    const layers = queryRows(
      database,
      `SELECT gc.table_name, gc.column_name, gc.srs_id
       FROM gpkg_geometry_columns gc
       JOIN gpkg_contents c ON c.table_name = gc.table_name
       WHERE c.data_type = 'features'`,
    );
    if (!layers.length) throw new Error("This GeoPackage does not contain a feature layer.");

    const features: Feature[] = [];
    for (const layer of layers) {
      const tableName = String(layer.table_name);
      const geometryColumn = String(layer.column_name);
      if (Number(layer.srs_id) !== 4326) {
        throw new Error(`Layer “${tableName}” uses EPSG:${layer.srs_id}. Export it as WGS84 (EPSG:4326) before uploading.`);
      }
      const rows = queryRows(database, `SELECT * FROM ${quoteIdentifier(tableName)}`);
      for (const row of rows) {
        const rawGeometry = row[geometryColumn];
        if (!(rawGeometry instanceof Uint8Array)) continue;
        const geometry = decodeGeoPackageGeometry(rawGeometry);
        if (!geometry) continue;
        const properties = Object.fromEntries(
          Object.entries(row)
            .filter(([key]) => key !== geometryColumn)
            .map(([key, value]) => [key, value instanceof Uint8Array ? "[binary]" : value]),
        );
        features.push({ type: "Feature", geometry, properties: { ...properties, _layer: tableName } });
      }
    }
    return { type: "FeatureCollection", features };
  } catch (error) {
    if (error instanceof Error && error.message.includes("file is not a database")) {
      throw new Error("This file is not a valid GeoPackage.");
    }
    throw error;
  } finally {
    database.close();
  }
}
