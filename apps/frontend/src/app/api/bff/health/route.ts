// BFF route: the browser's only door to backend health.
//
// Everything the UI needs follows this shape — a same-origin route handler
// that authorizes server-side and forwards. The browser never sees a bearer
// token, and never needs to reach the backend directly.

import { NextRequest, NextResponse } from "next/server";

import { CORRELATION_HEADER, backendFetch } from "@/lib/server/backend";

export const dynamic = "force-dynamic";

export async function GET(request: NextRequest) {
  // Reuse the caller's correlation id when present so the whole chain —
  // browser, BFF, Flask, and any task it spawns — shares one id.
  const correlationId = request.headers.get(CORRELATION_HEADER);

  try {
    const {
      status,
      body,
      correlationId: downstreamId,
    } = await backendFetch<Record<string, unknown>>("/api/v1/health/deep", {
      correlationId,
    });

    const response = NextResponse.json(body, { status });
    if (downstreamId) {
      response.headers.set(CORRELATION_HEADER, downstreamId);
    }
    return response;
  } catch (error) {
    // The backend being unreachable is a legitimate answer to "how are you",
    // not a 500 from the BFF: report it in the same shape the UI renders.
    return NextResponse.json(
      {
        status: "unreachable",
        components: {},
        detail: error instanceof Error ? error.message : "backend unreachable",
      },
      { status: 503 },
    );
  }
}
