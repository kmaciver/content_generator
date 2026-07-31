"""Cross-cutting primitives.

ULIDs, structured logging, correlation ids, hashing, and the storage client
that is the single choke point for all artifact I/O.

Submodules stay importable individually (``videoforge_shared.storage``); this
namespace re-exports only the names practically every consumer needs.
"""

from videoforge_shared.correlation import (
    CORRELATION_HEADER,
    correlation_context,
    correlation_headers,
    ensure_correlation_id,
    extract_correlation_id,
    get_correlation_id,
)
from videoforge_shared.hashing import content_key, sha256_bytes, sha256_stream
from videoforge_shared.ids import is_ulid, new_ulid
from videoforge_shared.logging import configure_logging
from videoforge_shared.storage import (
    IntegrityError,
    ObjectNotFoundError,
    StorageClient,
    StorageError,
    StoredObject,
    storage_client_from_settings,
)

__all__ = [
    "CORRELATION_HEADER",
    "IntegrityError",
    "ObjectNotFoundError",
    "StorageClient",
    "StorageError",
    "StoredObject",
    "configure_logging",
    "content_key",
    "correlation_context",
    "correlation_headers",
    "ensure_correlation_id",
    "extract_correlation_id",
    "get_correlation_id",
    "is_ulid",
    "new_ulid",
    "sha256_bytes",
    "sha256_stream",
    "storage_client_from_settings",
]
