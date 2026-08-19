// Serving media in the **dev profile**, where there is no nginx.
//
// ADR-011 makes `/assets/{bucket}/{key}` an nginx location: it rewrites to the
// backend, which authorizes and answers with an empty body plus
// `X-Accel-Redirect` to a presigned MinIO URL, and nginx streams the object.
// That is the production path and M0-10 proved it end to end.
//
// The dev profile deliberately has no nginx ("the API is reached directly on
// :5000"), so nothing served `/assets/` there at all — every image in the
// contact sheet, the narration player's audio, and the rendered MP4 were
// broken in the profile the app is actually developed in. The API worked
// because the browser reaches it through the BFF at `/api/bff/*`; media had no
// equivalent and nobody noticed until there was media to look at.
//
// This is that equivalent, and it follows the same rule as every other BFF
// route: **authorize in the backend, forward server-side, never let the
// browser near the token or the object store.** In particular it does *not*
// bypass Flask — the backend still decides whether the bucket is servable and
// still mints the presigned URL, so the authorization story is unchanged. This
// handler only does what nginx would have done with the redirect.
//
// **In production this file is never reached.** nginx matches `/assets/`
// before Next sees the request, so the two cannot disagree: there is exactly
// one authorization point either way.

import { NextRequest } from "next/server";

import { CORRELATION_HEADER } from "@/lib/server/backend";

export const dynamic = "force-dynamic";

const BACKEND_URL = process.env.BACKEND_INTERNAL_URL ?? "http://backend:5000";
const API_TOKEN = process.env.API_TOKEN ?? "";
const STORAGE_URL = process.env.STORAGE_INTERNAL_URL ?? "http://minio:9000";

/** Headers worth carrying back from the object store.
 *
 * `Accept-Ranges` and `Content-Range` are the ones that matter: without them a
 * browser will not scrub a video, it will only play from the start. */
const PASS_THROUGH = [
  "content-type",
  "content-length",
  "content-range",
  "accept-ranges",
  "etag",
  "last-modified",
  "cache-control",
];

export async function GET(
  request: NextRequest,
  context: { params: Promise<{ path: string[] }> },
) {
  const { path } = await context.params;
  if (path.length < 2) {
    return new Response("malformed asset path", { status: 400 });
  }

  const correlationId = request.headers.get(CORRELATION_HEADER);
  const headers = new Headers({ Accept: "*/*" });
  if (API_TOKEN) headers.set("Authorization", `Bearer ${API_TOKEN}`);
  if (correlationId) headers.set(CORRELATION_HEADER, correlationId);

  // Step 1: ask the backend. It authorizes the bucket, confirms the object
  // exists, and answers with the redirect nginx would have followed.
  const authorized = await fetch(
    `${BACKEND_URL}/api/v1/assets/${path.map(encodeURIComponent).join("/")}`,
    { headers, cache: "no-store", redirect: "manual" },
  );
  if (!authorized.ok) {
    // 403 for a non-servable bucket, 404 for a missing object — both are
    // answers, and both should reach the browser as themselves.
    return new Response(await authorized.text(), {
      status: authorized.status,
      headers: { "content-type": "application/problem+json" },
    });
  }

  const accel = authorized.headers.get("x-accel-redirect");
  if (!accel) {
    return new Response("backend did not issue an asset redirect", {
      status: 502,
    });
  }

  // Step 2: follow it. The redirect is `/internal-assets/{path}?{presigned}`;
  // nginx strips the prefix and proxies to the store, so this does the same.
  // The signature was computed for the store's own host, which is why the URL
  // is rebuilt against STORAGE_URL rather than the browser's origin.
  const target = `${STORAGE_URL}${accel.replace(/^\/internal-assets/, "")}`;
  const range = request.headers.get("range");
  const object = await fetch(target, {
    headers: range ? { Range: range } : undefined,
    cache: "no-store",
  });

  const out = new Headers();
  for (const name of PASS_THROUGH) {
    const value = object.headers.get(name);
    if (value) out.set(name, value);
  }
  // Content-addressed keys never change, so the answer never does either.
  // Same claim nginx makes in production.
  out.set("cache-control", "public, max-age=31536000, immutable");

  return new Response(object.body, { status: object.status, headers: out });
}
