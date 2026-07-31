# ADR-013 — Settings and the data layer live in `packages/`, not in the backend

- **Status:** Accepted
- **Date:** 2026-07-31
- **Related:** SADD §8, §10.1; deviates from the SADD's directory tree
- **Implemented in:** M0-04 (settings), M0-07 (persistence)

## Context

SADD §8 places `config/` and `orm/` inside `apps/backend`. Two M0 tickets ran
into the same wall: **both apps need them, and the apps must never import each
other** (a rule enforced by `tests/test_workspace_structure.py`).

- **Settings** (M0-04): workers need database, broker, and storage
  configuration exactly as much as the API does.
- **The data layer** (M0-07): the SADD's own §13 task skeleton has *workers*
  inserting artifact-version rows in the same transaction as their outputs.
  Workers write to the schema; the schema cannot live in the backend.

The §8 placement predates the structural rule and could not survive contact
with it. Discovered in M0, it would otherwise have become a wall mid-M1.

## Decision

- **`videoforge_shared.settings`** — the Pydantic Settings models.
  `videoforge.config` remains as a thin re-export of the *App-scoped names
  only*, deliberately omitting `WorkerSettings`, `ProviderKeys`, and
  `get_worker_settings` so the backend cannot reach worker configuration
  (NF8; asserted by an AST scan in `tests/test_secret_isolation.py`).
- **`packages/persistence`** — declarative `Base` with the naming convention,
  and engine/session factories. M1 adds the ORM models and repositories here.

## Consequences

- The apps → packages dependency arrow holds; neither app imports the other.
- Alembic's `env.py` imports metadata from `videoforge_persistence`, which is
  app-neutral — resolving finding S9 without the migrate service depending on
  the API.
- SADD §8's tree is amended accordingly (M0-13).
- Cost: two more workspace packages. The alternative was a rule violation the
  test suite would have failed on anyway.
