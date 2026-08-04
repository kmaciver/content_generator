"""Deterministic demo data (SADD §10.4).

The promise this keeps: clone the repo, ``make up-prod && make seed``, open the
UI, and there is something to review — with no provider key, no network, and
nothing that can cost money. A reviewer screen with no artifacts in it is
untestable by hand and unreviewable in a PR.

**Deterministic** means the ids are fixed, not generated. That is what lets the
Playwright suite (M1-11) address a known project by URL, lets a bug report say
"the seeded script's v2", and lets ``make seed`` twice be a no-op rather than a
second demo project. ULIDs are normally time-ordered and random; these are
hand-written constants that merely *look* like ULIDs — they only have to be 26
characters of the right alphabet, and being obviously fake is a feature when
you are staring at a database.
"""

from __future__ import annotations

from database.seed.demo import (
    DEMO_PROJECT_ID,
    DEMO_SERIES_ID,
    DEMO_USER_ID,
    DEMO_WORKSPACE_ID,
    seed_demo,
)

__all__ = [
    "DEMO_PROJECT_ID",
    "DEMO_SERIES_ID",
    "DEMO_USER_ID",
    "DEMO_WORKSPACE_ID",
    "seed_demo",
]
