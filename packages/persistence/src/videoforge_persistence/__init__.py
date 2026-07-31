"""Persistence layer shared by the backend and the workers.

This package exists because BOTH apps write to the database — the API creates
jobs, the worker task skeleton inserts artifact versions in the same
transaction as its outputs (SADD §13) — and the apps must never import each
other. The SADD's §8 tree drew ``orm/`` under the backend; that placement
could not survive contact with the worker skeleton, so the data layer lives
here. Recorded as a SADD amendment in M0-13.

M0-07 ships the foundations: declarative base with a naming convention, and
engine/session factories. M1 fills in the actual tables and repositories.
"""

from videoforge_persistence.base import Base
from videoforge_persistence.engine import create_engine_from_settings, session_factory

__all__ = ["Base", "create_engine_from_settings", "session_factory"]
