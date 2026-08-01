# ADR-015 — Workflow rules live in `packages/domain`, not under the backend

- **Status:** Accepted
- **Date:** 2026-08-01
- **Deciders:** kmaciver
- **Related:** supersedes SADD §8's placement of `domain/`; extends the same
  reasoning as ADR-013 (settings and data layer in `packages/`)
- **Verified by:** ticket M1-02

## Context

SADD §8 draws the repository tree with `domain/` inside `apps/backend`, next to
`api/`, `services/`, and `dto/`. That placement cannot survive §12.2.

§12.2 states that artifact transitions are caused by exactly four things: job
success, job failure, review decision, and human edit. Two of those four happen
in **workers** — a worker that finishes generating a script must move the
artifact `GENERATING → AWAITING_APPROVAL` in the same transaction as the
artifact-version row it just wrote (§13, §10.3 rule 6).

Workers must never import the backend. That rule is enforced by a structural
test, not by convention, and it exists so the two apps can be deployed,
scaled, and reasoned about separately.

So a backend-owned FSM leaves exactly three options, all bad:

1. Workers duplicate the transition rules. Two definitions of "what may follow
   GENERATING" drift, and the symptom is a project wedged in a state no one can
   explain.
2. Workers write `artifact.state` by hand with no machine at all — which is the
   same as (1), minus the pretence.
3. Workers call back into the API to request a transition, adding a network hop
   and a failure mode to a path that is currently one transaction.

This is not a new problem. M0-07 hit precisely this shape with the ORM: §8 drew
`orm/` under the backend, while §13's worker skeleton has workers inserting
artifact-version rows. That was resolved by moving the data layer to
`packages/persistence` (ADR-013).

## Decision

**Workflow rules live in `packages/domain`, imported by both apps.**

`videoforge_domain` contains the artifact FSM (§12.2), the job FSM (§12.3), and
`ApprovalPolicy` (§11). Its only dependency is `videoforge-shared`, which is
where the shared state vocabulary (`videoforge_shared.enums`) now lives so that
neither the ORM nor the domain layer owns the words the other needs.

The package has **no SQLAlchemy, no Flask, no Celery, no clock, and no I/O**,
and that is the point rather than an accident: it makes SADD §10.1's "DB-free
domain tests" literally true. The M1-02 suite is 48 tests running in 0.05s with
no fixtures — for a system whose core complexity is workflow rules, that is
where the tests should be cheapest.

A dependency added to this package is a claim that a workflow rule needs
infrastructure to be expressed. That should be argued in an ADR, not slipped in.

## Consequences

- The dependency arrow still points `apps → packages`, never sideways. The rule
  §8 was protecting is preserved; only the drawing changes.
- The transition table and the API's `capabilities` payload are the same object,
  so a button the UI renders and a transition the service accepts cannot
  disagree (§11). Reimplementing guards in TypeScript is explicitly not done.
- SADD §8's tree needs amending, as §8 already was for `packages/persistence`.
- One more workspace member to register: `pyproject.toml` sources, `mypy_path`,
  and the app image's manifest-copy layer.

## Alternatives rejected

- **Duplicate the FSM in the worker** — the drift failure mode above, and it is
  silent.
- **Put the FSM in `packages/persistence`** — it is not persistence, and it
  would drag SQLAlchemy into the pure layer through the package `__init__`.
- **Put the FSM in `packages/shared`** — `shared` is cross-cutting *primitives*
  (ULIDs, hashing, logging, storage). Workflow rules are the application's core
  domain; burying them among utilities misrepresents both.
- **Transition-by-API-callback from workers** — a network round-trip and a new
  partial-failure mode inserted into what is currently one local transaction.
