#!/bin/sh
# MinIO bootstrap: create the buckets the platform expects and apply retention.
#
# Runs as a one-shot compose service. Every operation is idempotent, because
# this executes on every `make up`, not just the first one.
set -eu

: "${MINIO_ENDPOINT:?MINIO_ENDPOINT is required}"
: "${MINIO_ROOT_USER:?MINIO_ROOT_USER is required}"
: "${MINIO_ROOT_PASSWORD:?MINIO_ROOT_PASSWORD is required}"

echo "==> registering alias for ${MINIO_ENDPOINT}"
mc alias set local "${MINIO_ENDPOINT}" "${MINIO_ROOT_USER}" "${MINIO_ROOT_PASSWORD}" >/dev/null

# SADD §18.3. Content-addressed keys make artifacts immutable by construction,
# so these need no versioning: the same bytes always land on the same key, and
# different bytes never collide with an existing one.
for bucket in \
  "${MINIO_BUCKET_ARTIFACTS:-artifacts}" \
  "${MINIO_BUCKET_ASSETS:-assets}" \
  "${MINIO_BUCKET_PACKAGES:-packages}" \
  "${MINIO_BUCKET_TMP_RENDER:-tmp-render}"
do
  echo "==> ensuring bucket: ${bucket}"
  mc mb --ignore-existing "local/${bucket}" >/dev/null
done

# tmp-render holds intermediate frames and scratch files from the FFmpeg render
# worker. Everything here is reproducible from the timeline, so it expires
# rather than accumulating. The other three buckets never auto-delete: history
# is the product's safety net (SADD §18.5).
TMP_BUCKET="${MINIO_BUCKET_TMP_RENDER:-tmp-render}"
echo "==> applying 1-day expiry to ${TMP_BUCKET}"

# NOTE: the minio/mc image is minimal and ships no grep, awk, or jq -- only mc
# and a POSIX shell. An earlier version of this piped `mc ilm rule ls` into grep
# to test for an existing rule; grep was missing, the guard failed open, and a
# duplicate expiry rule was appended on every `make up`. Use shell pattern
# matching, which needs no external binary.
existing_rules="$(mc ilm rule ls "local/${TMP_BUCKET}" 2>/dev/null || true)"
case "${existing_rules}" in
  *Expiration*|*EXPIRY*|*expiry*)
    echo "    (expiry rule already present, leaving as-is)"
    ;;
  *)
    # Older mc spells this `mc ilm add`; try the modern form first.
    mc ilm rule add --expire-days 1 "local/${TMP_BUCKET}" 2>/dev/null \
      || mc ilm add --expiry-days 1 "local/${TMP_BUCKET}" 2>/dev/null \
      || echo "    WARNING: could not apply lifecycle rule (non-fatal)"
    ;;
esac

echo "==> buckets present:"
mc ls local

# TODO(M1): create a restricted application credential and deny it DeleteObject
# and overwrite on `artifacts`, per SADD §18.1. Deferred because there is no
# application credential to scope yet -- the platform currently uses the root
# user, which is acceptable only while nothing but this bootstrap runs.
echo "==> bootstrap complete"
