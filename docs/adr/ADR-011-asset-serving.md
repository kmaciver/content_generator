# ADR-011 — Asset serving via X-Accel-Redirect with internal presigning

- **Status:** Accepted
- **Date:** 2026-07-31
- **Deciders:** kmaciver
- **Related:** finding B4 (the unworkable presign + `auth_request` combination, described below); supersedes SADD §21.5/§23.2's `/assets/` design
- **Verified by:** ticket M0-10 (live, all four checks)

## Context

The SADD's asset path combined two authorization schemes that cannot coexist
(B4): MinIO-presigned URLs *and* an nginx `auth_request` gate. If URLs are
presigned, `auth_request` is redundant and the proxy must preserve the exact
signed host and query or MinIO returns `SignatureDoesNotMatch`. If they are
not presigned, nginx must authenticate to MinIO itself — and **nginx cannot
compute SigV4 request signatures**. Additionally, `Cache-Control: immutable`
on a presigned URL is dead weight: the rotating query string is part of the
browser's cache key, so nothing ever hits cache — defeating the main payoff
of content-addressed keys. And `auth_request` fires per byte-range request,
turning MP4 scrubbing into a subrequest storm.

## Decision

**Stable public URLs, API authorization, internal presigning, X-Accel-Redirect
delivery.**

```
Browser ── GET /assets/{bucket}/{content-key}          (stable URL, cacheable)
   nginx ── rewrite → /api/v1/assets/... → uwsgi_pass  (Flask authorizes)
  Flask ── 200, empty body, X-Accel-Redirect:
           /internal-assets/{bucket}/{key}?X-Amz-...   (presigned internally)
   nginx ── internal location (`internal;`) → proxy_pass → MinIO
  MinIO ── validates the SigV4 signature, streams bytes → nginx → browser
```

This refines the original B4 remedy one step further: instead of anonymous
bucket access at the nginx→MinIO hop, **the API presigns internally** and the
signed query rides only on the internal redirect. Three properties fall out:

1. **The browser's URL never changes** → `Cache-Control: public,
   max-age=31536000, immutable` is *correct*, not optimistic — the key embeds
   the content hash, so changed content is a different URL by construction.
2. **No bucket is ever anonymous** → MinIO verifies a real signature on every
   fetch; a leaked internal path without a valid signature serves nothing.
3. **nginx never signs anything** → the impossible requirement disappears.

Authorization is one fast Flask round-trip per asset URL (not per range
request); the bytes stream nginx→browser without touching Python (NF2).
`tmp-render` is excluded from the servable-bucket allowlist — scratch space is
never public. Presigned URLs to the *browser* remain for exactly one case: the
publishing-package download 302 (SADD §19.1), one-shot and uncached.

## Verification (M0-10, live against a real rendered MP4)

| Check | Result |
|---|---|
| Full fetch through the chain | 200, `Content-Type: video/mp4`, length exact, **sha256 of received bytes matches the key** |
| Range mid-file | 206, `Content-Range: bytes 1000-1999/56439`, exactly 1000 bytes |
| Range at tail (seek-to-end) | 206, 439 bytes |
| Conditional revalidation | `If-None-Match` → **304**, zero bytes |
| Immutable header | present on 200 and 206 |
| Direct `/internal-assets/` from outside | **404** (the `internal;` guard) before any storage access |
| Scratch bucket via public URL | 403 problem+json |
| Missing key | 404 problem+json |
| MinIO from the host (prod-local) | port unpublished, connection refused |
| Real browser | video plays inline; **seek to 5.2s lands correctly** (1080×1920, 5.6s duration) |

Two implementation details discovered live, now encoded:

- Uploads must carry a real `ContentType` (storage client now guesses from the
  filename) and the authorizing response must declare the type explicitly —
  nginx carries Content-Type from the redirecting response, and Flask's
  default smeared `; charset=utf-8` onto `video/mp4`.
- The `upstream s3 { server minio:9000; }` block made build-time `nginx -t`
  impossible (the hostname only resolves on the compose network); the config
  check moved to runtime, where a bad config still fails the `service_healthy`
  gate within seconds.

## Consequences

- The M4 video-review screen builds on a proven path; nothing about scrubbing
  is speculative.
- The bearer-token gate (SADD §21.1, M1) slots in front of the Flask handler
  without changing the handshake.
- Cloud migration: this pattern is nginx-specific. On the §24 path,
  `/assets/` becomes CloudFront + Origin Access Control in front of S3 — the
  *public URL shape survives*, which is what the frontend depends on.
- Presign TTL (15 min) bounds the internal redirect's validity; irrelevant to
  callers since the redirect is consumed within the same request.

## Alternatives rejected

- **`auth_request` + presigned public URLs** (the SADD's sketch): mutually
  exclusive mechanisms; caching defeated; subrequest per range (B4).
- **Anonymous bucket read scoped to the docker network**: works, but grants
  every container (and, in the dev profile, the host) unauthenticated reads,
  and diverges harder from the S3 migration path.
- **Presigned URLs handed to the browser**: no caching, MinIO reachable from
  the browser required, and URL expiry surfaces as user-visible 403s mid-view.
