"""Series branding endpoints — the API half of M3-13.

Split from the editor UI and built ahead of it, because the *API* is a
prerequisite for M3-04 and the *screen* is not: reference-sheet generation
needs a character to exist, and without these routes the only way to make one
is hand-written SQL.

Branding lives on its own surface for the reason ADR-016 gives — character and
style are not stages a project produces, they are preconditions it consumes, so
they belong beside the series rather than in the project review screen.

The same two rules as ``projects.py`` hold here: views translate HTTP ⇄ DTOs ⇄
repositories and carry no business logic, and what a client is allowed to do
comes from the server (``BrandingDetail.ready``) rather than being re-derived
in TypeScript.
"""

from __future__ import annotations

import logging

from flask import Blueprint, Response, jsonify, request
from pydantic import BaseModel, ValidationError

from videoforge.api.deps import dispatcher, transaction
from videoforge.api.errors import ApiError
from videoforge.dto import (
    ApproveCharacterRequest,
    BrandingDetail,
    CharacterSummary,
    CreateCharacterRequest,
    CreateStyleRequest,
    ReferenceSummary,
    SeriesSummary,
    StyleSummary,
)
from videoforge.services.jobs import JobService
from videoforge_persistence.uow import UnitOfWork
from videoforge_prompts.style import compile_style_block
from videoforge_shared.ids import new_ulid
from videoforge_shared.tasks import REFERENCES_GENERATE

logger = logging.getLogger(__name__)

series_blueprint = Blueprint("series", __name__)


def _body[T: BaseModel](model: type[T]) -> T:
    payload = request.get_json(silent=True)
    if payload is None:
        raise ApiError(400, "Invalid request", "expected a JSON object body")
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        first = exc.errors()[0]
        location = ".".join(str(p) for p in first["loc"]) or "body"
        raise ApiError(400, "Invalid request", f"{location}: {first['msg']}") from exc


def _ok(model: BaseModel, status: int = 200) -> tuple[Response, int]:
    return jsonify(model.model_dump(mode="json")), status


def _require_series(uow: UnitOfWork, series_id: str) -> None:
    if uow.series.get(series_id) is None:
        raise ApiError(404, "Not found", f"no series {series_id}")


@series_blueprint.get("/series")
def list_series() -> tuple[Response, int]:
    """Every series in the sole workspace.

    ``sole()`` rather than a workspace path parameter: v1 seeds exactly one
    workspace, and inventing a route shape for multi-tenancy that does not
    exist would be a URL to migrate later for no benefit now.
    """
    with transaction() as uow:
        workspace = uow.workspaces.sole()
        if workspace is None:
            raise ApiError(500, "Not configured", "no workspace exists")
        rows = uow.series.for_workspace(workspace.id)
        return jsonify([SeriesSummary.of(s).model_dump(mode="json") for s in rows]), 200


@series_blueprint.get("/series/<series_id>/branding")
def get_branding(series_id: str) -> tuple[Response, int]:
    """Approved character and style, their history, and whether images can run."""
    with transaction() as uow:
        _require_series(uow, series_id)

        character = uow.branding.approved_character(series_id)
        style = uow.branding.approved_style(series_id)
        references = uow.branding.approved_references(series_id)

        # The same question the write path asks, answered here so the UI never
        # reimplements it. Deliberately *not* a call into `resolve_branding`:
        # that one is project-scoped and pin-aware, and a series has no pin.
        missing = [
            label
            for label, value in (
                ("an approved character", character),
                ("an approved style", style),
            )
            if value is None
        ]

        return _ok(
            BrandingDetail(
                series_id=series_id,
                character=CharacterSummary.of(character) if character else None,
                style=StyleSummary.of(style) if style else None,
                references=[ReferenceSummary.of(r) for r in references],
                characters=[
                    CharacterSummary.of(c) for c in uow.branding.characters(series_id)
                ],
                styles=[StyleSummary.of(s) for s in uow.branding.styles(series_id)],
                ready=not missing,
                missing=missing,
            )
        )


@series_blueprint.post("/series/<series_id>/characters")
def create_character(series_id: str) -> tuple[Response, int]:
    """A new character version, PENDING until explicitly approved."""
    payload = _body(CreateCharacterRequest)
    with transaction() as uow:
        _require_series(uow, series_id)
        character = uow.branding.add_character_version(
            series_id,
            name=payload.name,
            immutable_traits=payload.immutable_traits,
            variable_traits=payload.variable_traits,
        )
        uow.flush()
        summary = CharacterSummary.of(character)
    return _ok(summary, 201)


@series_blueprint.post("/characters/<character_id>/approve")
def approve_character(character_id: str) -> tuple[Response, int]:
    """Approve a character version, superseding the incumbent.

    Both writes are in one transaction, which is what makes them safe: the
    partial unique index allows exactly one approved character per series, so
    superseding and approving committed separately would collide.
    """
    payload = _body(ApproveCharacterRequest)
    with transaction() as uow:
        character = uow.branding.approve_character(
            character_id, reference_group_id=payload.reference_group_id
        )
        if character is None:
            raise ApiError(404, "Not found", f"no character {character_id}")
        uow.flush()
        summary = CharacterSummary.of(character)
    return _ok(summary)


@series_blueprint.post("/characters/<character_id>/references")
def generate_references(character_id: str) -> tuple[Response, int]:
    """Kick off a candidate reference-sheet run — 202 with a job id (M3-04b).

    Async for the ordinary reason (§19.2): four images is roughly half a
    minute, and the API never generates. The response is a receipt; the client
    polls ``GET /jobs/{id}``.

    The style is checked *here* rather than only in the worker so an operator
    who has not set one gets a 409 immediately, instead of a job that fails
    thirty seconds later — a reference sheet drawn without the series style
    would not match the scenes it is supposed to anchor.
    """
    with transaction() as uow:
        character = uow.branding.character(character_id)
        if character is None:
            raise ApiError(404, "Not found", f"no character {character_id}")
        if uow.branding.approved_style(character.series_id) is None:
            raise ApiError(
                409,
                "Conflict",
                "this series has no approved style; reference sheets drawn "
                "without one would not match the scenes they anchor",
            )

        # The group id is minted **here**, not in the worker, so it can be
        # returned in this response and folded into the idempotency key. A
        # worker-side id would make two clicks two groups.
        group_id = new_ulid()
        service = JobService(uow, dispatcher())
        reserved = service.request_series_job(
            series_id=character.series_id,
            spec=REFERENCES_GENERATE,
            # Keyed on the character *version*, so asking twice for the same
            # version is one run — while a new version legitimately gets its
            # own sheets.
            idempotency_key_suffix=f"character:{character_id}",
            input_snapshot={"character_id": character_id, "group_id": group_id},
        )
        job_id = reserved.job.id
        created = reserved.created
        # On a duplicate the *original* group is the one being generated; ours
        # was never used. Returning it would send the client to an empty group.
        actual_group = str(reserved.job.input_snapshot.get("group_id", group_id))

    service.dispatch_pending()
    return (
        jsonify({"job_id": job_id, "created": created, "group_id": actual_group}),
        202,
    )


@series_blueprint.post("/series/<series_id>/styles")
def create_style(series_id: str) -> tuple[Response, int]:
    """A new style version, with its fields compiled to a prompt block.

    Compiled **on write** rather than on read, because the block is what
    actually reaches the provider and §10.3 rule 4 needs the value used, not
    one re-derived later by whatever the compiler does then. Storing it also
    lets the editor show the operator exactly what their fields become.
    """
    payload = _body(CreateStyleRequest)
    spec = compile_style_block(payload.fields)
    with transaction() as uow:
        _require_series(uow, series_id)
        style = uow.branding.add_style_version(
            series_id,
            name=payload.name,
            fields=payload.fields,
            prompt_block=spec.block,
        )
        uow.flush()
        summary = StyleSummary.of(style)
    return _ok(summary, 201)


@series_blueprint.post("/styles/<style_id>/approve")
def approve_style(style_id: str) -> tuple[Response, int]:
    with transaction() as uow:
        style = uow.branding.approve_style(style_id)
        if style is None:
            raise ApiError(404, "Not found", f"no style {style_id}")
        uow.flush()
        summary = StyleSummary.of(style)
    return _ok(summary)
