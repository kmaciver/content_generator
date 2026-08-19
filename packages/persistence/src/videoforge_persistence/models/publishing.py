"""The publishing package (SADD §10.2, M5-03).

``publishing_package(…, zip_key, manifest jsonb)`` — an **artifact_version
extension**, hanging off ``artifact_version_id`` like ``scene_set`` and for the
reason that model's docstring gives at length: artifact is identity, version is
content, and a package is unambiguously content. Regenerating a package after
rewording the caption produces a second version and a second row, not an UPDATE.

**Why a table at all**, when the version already has a ``storage_key`` and a
``meta`` column that could hold the manifest. Two reasons, and only the second
is about this milestone:

* ``zip_key`` is a foreign concept to ``artifact_version.storage_key`` only by
  accident — they are the same key. But the **manifest** is queryable content:
  "which packages contain scene 7's image?" is a real support question and a
  jsonb column with an index answers it, where a blob inside ``meta`` mixes it
  with provider debris that has no schema.
* N2 reserves ``PublishingProvider`` as the seam for actual uploading. When it
  lands, the row that says "this archive was built" is where "and it was
  published at 14:02 to this account" belongs. Putting it in ``meta`` now would
  mean migrating it out later.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from videoforge_persistence.base import Base
from videoforge_persistence.columns import ULIDType, created_at_col, ulid_pk


class PublishingPackage(Base):
    """One assembled archive, pinned to the version that produced it."""

    __tablename__ = "publishing_package"

    id: Mapped[str] = ulid_pk()
    #: UNIQUE, exactly as ``scene_set``: a version has one package, which makes
    #: "the archive of version N" a lookup rather than a query with an
    #: ordering guess.
    artifact_version_id: Mapped[str] = mapped_column(
        ULIDType,
        sa.ForeignKey("artifact_version.id", ondelete="CASCADE"),
        nullable=False,
    )
    #: The object key of the zip, in the **artifacts** bucket (ADR-011) — a
    #: finished output, not a generated input.
    zip_key: Mapped[str] = mapped_column(sa.Text, nullable=False)
    #: Every entry with its sha256, so a downloaded package can be verified
    #: rather than trusted. See ``videoforge_workers.packaging``.
    manifest: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = created_at_col()
    # NOTE: no ``updated_at``. Its absence is a claim; the trigger enforces it.

    __table_args__ = (sa.UniqueConstraint("artifact_version_id"),)
