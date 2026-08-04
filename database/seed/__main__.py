"""``python -m database.seed`` — the entry point ``make seed`` calls.

Runs inside the application image, against whatever ``PostgresSettings``
resolves to, so it needs no arguments and no separate connection config.
"""

from __future__ import annotations

import logging
import sys

from database.seed.demo import seed_demo
from videoforge_persistence.engine import create_engine_from_settings
from videoforge_shared.logging import configure_logging
from videoforge_shared.settings import get_app_settings

logger = logging.getLogger("database.seed")


def main() -> int:
    settings = get_app_settings()
    configure_logging(
        level=settings.core.log_level.value, fmt=settings.core.log_format.value
    )
    engine = create_engine_from_settings(settings.postgres)
    try:
        result = seed_demo(engine)
    finally:
        engine.dispose()

    if result.created:
        print(f"seeded demo data — project {result.project_id}")
    else:
        print("demo data already present; nothing to do")
    # Zero either way: `make seed` on an already-seeded database is a normal
    # thing to do, not a failure, and a non-zero exit would break any script
    # that seeds before running something else.
    return 0


if __name__ == "__main__":
    sys.exit(main())
