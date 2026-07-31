// The BFF's server-side backend client (S6, SADD §21.1).
//
// `server-only` is the enforcement mechanism, not decoration: if any client
// component ever imports this module — directly or through a chain — the Next
// build FAILS. The alternative (a convention that says "don't import this in
// client code") is a rule nobody can verify. This one is checked by the
// compiler on every build.
//
// The browser therefore never holds API_TOKEN. It calls same-origin routes
// under /api/bff/*, and those route handlers call the backend through here
// with the bearer attached server-side.
import "server-only";

/** Internal backend base URL. Container DNS in compose; never browser-reachable. */
const BACKEND_URL = process.env.BACKEND_INTERNAL_URL ?? "http://backend:5000";

/** Shared bearer for the API (SADD §21.1). Server-side only, by construction. */
const API_TOKEN = process.env.API_TOKEN ?? "";

/** Correlation header name — the same one nginx mints and uWSGI logs. */
export const CORRELATION_HEADER = "X-Request-Id";

export interface BackendResponse<T> {
  status: number;
  body: T;
  correlationId: string | null;
}

/**
 * Call the backend API with the bearer injected.
 *
 * `correlationId` is threaded through explicitly so a browser request, the BFF
 * hop, the Flask handler, and any Celery task it spawns all share one id
 * (SADD §21.8) — the property that makes a user-reported failure traceable.
 */
export async function backendFetch<T>(
  path: string,
  init: RequestInit & { correlationId?: string | null } = {},
): Promise<BackendResponse<T>> {
  const { correlationId, ...requestInit } = init;

  const headers = new Headers(requestInit.headers);
  headers.set("Accept", "application/json");
  if (API_TOKEN) {
    headers.set("Authorization", `Bearer ${API_TOKEN}`);
  }
  if (correlationId) {
    headers.set(CORRELATION_HEADER, correlationId);
  }

  const response = await fetch(`${BACKEND_URL}${path}`, {
    ...requestInit,
    headers,
    // Health and job state are live values; a cached "everything is fine" is
    // worse than no answer at all.
    cache: "no-store",
  });

  return {
    status: response.status,
    body: (await response.json()) as T,
    correlationId: response.headers.get(CORRELATION_HEADER),
  };
}
