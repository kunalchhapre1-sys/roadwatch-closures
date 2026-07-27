import { env } from "cloudflare:workers";

export const runtime = "edge";
const OBJECT_KEY = "road-closures/current.gpkg";
const MAX_FILE_SIZE = 50 * 1024 * 1024;

function bucket() {
  if (!env.ROAD_CLOSURES) throw new Error("Road closure storage is not configured.");
  return env.ROAD_CLOSURES as R2Bucket;
}

export async function GET() {
  const object = await bucket().get(OBJECT_KEY);
  if (!object) return Response.json({ error: "No GeoPackage has been published." }, { status: 404 });
  return new Response(object.body, {
    headers: {
      "Content-Type": object.httpMetadata?.contentType || "application/geopackage+sqlite3",
      "Content-Length": String(object.size),
      "Cache-Control": "no-store",
      ETag: object.etag,
    },
  });
}

export async function PUT(request: Request) {
  const contentLength = Number(request.headers.get("content-length") || 0);
  if (!contentLength || contentLength > MAX_FILE_SIZE) {
    return Response.json({ error: "The GeoPackage must be 50 MB or smaller." }, { status: 413 });
  }
  const encodedName = request.headers.get("x-file-name") || "road-closures.gpkg";
  const fileName = decodeURIComponent(encodedName).replace(/[^\w.\- ()]/g, "_").slice(0, 160);
  if (!fileName.toLowerCase().endsWith(".gpkg")) {
    return Response.json({ error: "Only .gpkg files are accepted." }, { status: 400 });
  }
  const updatedAt = new Date().toISOString();
  const object = await bucket().put(OBJECT_KEY, request.body, {
    httpMetadata: { contentType: "application/geopackage+sqlite3" },
    customMetadata: { fileName, updatedAt },
  });
  return Response.json({
    exists: true,
    etag: object.etag,
    updatedAt,
    fileName,
    size: contentLength,
  });
}
