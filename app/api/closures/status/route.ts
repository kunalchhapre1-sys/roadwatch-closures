import { env } from "cloudflare:workers";

export const runtime = "edge";
const OBJECT_KEY = "road-closures/current.gpkg";

export async function GET() {
  if (!env.ROAD_CLOSURES) {
    return Response.json({ error: "Road closure storage is not configured." }, { status: 503 });
  }
  const object = await (env.ROAD_CLOSURES as R2Bucket).head(OBJECT_KEY);
  if (!object) return Response.json({ exists: false }, { headers: { "Cache-Control": "no-store" } });
  return Response.json(
    {
      exists: true,
      etag: object.etag,
      updatedAt: object.customMetadata?.updatedAt || object.uploaded.toISOString(),
      fileName: object.customMetadata?.fileName || "road-closures.gpkg",
      size: object.size,
    },
    { headers: { "Cache-Control": "no-store" } },
  );
}
