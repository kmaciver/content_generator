// BFF catch-all: the browser's only door to the API (S6, SADD §21.1).
//
// One handler rather than a file per endpoint. The alternative — a route
// module for /projects, another for /artifacts, another for /reviews — is a
// dozen near-identical files whose only difference is a path string, and the
// day one of them forgets to attach the bearer is the day the browser starts
// getting 401s that look like a backend problem.
//
// The allowlist below is what keeps a catch-all from becoming an open proxy.
// Without it this route would forward *any* path a client asked for, including
// ones the UI has no business reaching, with the API token attached.

import { NextRequest, NextResponse } from "next/server";

import { CORRELATION_HEADER, backendFetch } from "@/lib/server/backend";

export const dynamic = "force-dynamic";

// Paths the UI is allowed to reach, as prefixes under /api/v1.
//
// `health` is absent deliberately: it has its own route already, and adding it
// here would give two ways to ask the same question with different error
// shapes. `assets` is absent because those are served by nginx via
// X-Accel-Redirect (ADR-011) and must never be proxied through Node — the
// whole point of that design is that bytes do not pass through an application
// tier.
const ALLOWED_PREFIXES = [
  "projects",
  "artifacts",
  "artifact-versions",
  "jobs",
] as const;

function isAllowed(segments: string[]): boolean {
  const [head] = segments;
  return (
    head !== undefined && (ALLOWED_PREFIXES as readonly string[]).includes(head)
  );
}

async function proxy(request: NextRequest, segments: string[]) {
  if (!isAllowed(segments)) {
    return NextResponse.json(
      {
        type: "about:blank",
        title: "Not found",
        status: 404,
        detail: `no BFF route for /${segments.join("/")}`,
      },
      { status: 404 },
    );
  }

  // Reuse the caller's correlation id when present so browser, BFF, Flask and
  // any Celery task it spawns all share one id (SADD §21.8).
  const correlationId = request.headers.get(CORRELATION_HEADER);
  const search = request.nextUrl.search;
  const path = `/api/v1/${segments.join("/")}${search}`;

  // Only read a body for methods that have one; calling .text() on a GET
  // yields "" and turns a valid request into one with a Content-Type and an
  // empty payload, which Flask then rejects as malformed JSON.
  const hasBody = !["GET", "HEAD"].includes(request.method);
  const body = hasBody ? await request.text() : undefined;

  try {
    const {
      status,
      body: responseBody,
      correlationId: downstreamId,
    } = await backendFetch<unknown>(path, {
      method: request.method,
      correlationId,
      body,
      headers: hasBody ? { "Content-Type": "application/json" } : undefined,
    });

    const response = NextResponse.json(responseBody, { status });
    if (downstreamId) {
      response.headers.set(CORRELATION_HEADER, downstreamId);
    }
    return response;
  } catch (error) {
    // Shaped as problem+json so the UI has exactly one error format to render,
    // whether the failure came from Flask or from this hop.
    return NextResponse.json(
      {
        type: "about:blank",
        title: "Backend unreachable",
        status: 503,
        detail: error instanceof Error ? error.message : "backend unreachable",
      },
      { status: 503 },
    );
  }
}

type Context = { params: Promise<{ path: string[] }> };

export async function GET(request: NextRequest, context: Context) {
  return proxy(request, (await context.params).path);
}

export async function POST(request: NextRequest, context: Context) {
  return proxy(request, (await context.params).path);
}

export async function PUT(request: NextRequest, context: Context) {
  return proxy(request, (await context.params).path);
}
