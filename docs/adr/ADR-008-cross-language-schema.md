# ADR-008 — JSON Schema as the cross-language contract source

- **Status:** ⚠️ **WITHDRAWN** (finding S8 + decision D4, 2026-07-30) — never implemented
- **Original status:** Accepted
- **Related:** SADD §7.7

## Original context and decision

The Timeline JSON and event payloads were to cross a Python ⇄ TypeScript
boundary: the timeline compiler in Python, the renderer in TypeScript. To stop
the two drifting, JSON Schema documents in `packages/schemas` would be the
source of truth, code-generated into Pydantic models and TS types at build
time.

## Why it was withdrawn

The motivation was **preventing drift between two language implementations of
one contract**. D4 removed the TypeScript renderer — the timeline compiler and
the renderer are now both Python, in the same repository, sharing the same
models. The timeline crosses no language boundary at all.

The frontend consumes the timeline read-only, if ever, and can do so through
the OpenAPI-generated client like any other API response.

## Decision

Plain Pydantic models are the single definition. `packages/schemas` is not
created, and the codegen toolchain (`datamodel-code-generator`,
`json-schema-to-typescript`) is not introduced.

## Consequences

- Two build-time generators and a drift-check CI step avoided.
- Should a second language ever consume the timeline directly, revisit this —
  the reasoning above is sound, only its premise expired.
