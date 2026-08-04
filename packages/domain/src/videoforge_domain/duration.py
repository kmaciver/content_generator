"""Target video length (finding S11).

§13 says the scenes stage "validates durations sum ≈ target length" and §10.2
gave that target nowhere to live. S11 proposed
``video_project.settings.target_duration_ms`` "defaulted from
``series.style_preset``" — and that column was dropped by ADR-016 before
anything read it, so the default needed a new home.

**Settled 2026-08-03:** the value lives in ``video_project.settings``, a jsonb
column that already exists, with a system default here. No `series`-level
default until a second series exists to want one; not on M3's style table,
which would make M2 wait on M3.

Pure — a dict in, milliseconds out — so the scenes validator is unit-testable
with no project row.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = [
    "DEFAULT_TARGET_DURATION_MS",
    "MAX_TARGET_DURATION_MS",
    "MIN_TARGET_DURATION_MS",
    "SETTINGS_KEY",
    "duration_tolerance_ms",
    "target_duration_ms",
]

SETTINGS_KEY = "target_duration_ms"

#: 50 seconds. Inside every short-form platform's limit, and long enough for
#: the ~20 hard cuts §1.0.1 assumes — roughly 2.5s per scene.
DEFAULT_TARGET_DURATION_MS = 50_000

#: Below this there is no room for a hook and a payoff; above it, the platforms
#: that matter start refusing the upload. Bounds rather than free text because
#: a typo'd `target_duration_ms: 50` (milliseconds, not seconds) would ask the
#: model for a fifty-millisecond video and get something baffling back.
MIN_TARGET_DURATION_MS = 10_000
MAX_TARGET_DURATION_MS = 180_000

#: How far the sum of scene durations may drift from the target before the
#: scenes stage rejects the plan. 15% of a 50s video is ±7.5s — loose enough
#: that a model pacing scenes sensibly is never punished, tight enough to catch
#: the real failure, which is a model producing six scenes or forty.
_TOLERANCE = 0.15


def target_duration_ms(settings: Mapping[str, Any] | None) -> int:
    """Read the target from a project's ``settings``, falling back and clamping.

    Never raises. An out-of-range or unparseable value clamps to the bounds
    rather than failing the job: the target is a *goal for a prompt*, and
    refusing to generate anything because someone typed a bad number would be
    a worse outcome than generating a sensibly-sized video.
    """
    raw = (settings or {}).get(SETTINGS_KEY)
    try:
        value = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_TARGET_DURATION_MS
    return max(MIN_TARGET_DURATION_MS, min(MAX_TARGET_DURATION_MS, value))


def duration_tolerance_ms(target_ms: int) -> int:
    """The ± window the scenes stage accepts around ``target_ms``."""
    return int(target_ms * _TOLERANCE)
