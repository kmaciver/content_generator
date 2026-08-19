"""Project, artifact, version, review and job endpoints (SADD §19.1).

Views translate HTTP ⇄ DTOs ⇄ services and contain **no business logic**. That
is not stylistic: ADR-002's exit from uWSGI stays cheap only while the
transport boundary is clean, and any domain rule that leaks into this module
erodes it.

Two rules visible throughout:

* **The API never generates.** ``POST /generations`` writes a job row and
  returns 202 with its id. Nothing here calls a provider.
* **Capabilities come from the FSM.** Every artifact response carries what the
  machine allows, so a button the UI renders and an action the service accepts
  cannot disagree (§11).
"""

from __future__ import annotations

import logging

from flask import Blueprint, Response, current_app, jsonify, request
from pydantic import BaseModel, ValidationError

from videoforge.api.deps import dispatcher, transaction
from videoforge.api.errors import ApiError
from videoforge.dto import (
    ApproveManyRequest,
    ArtifactDetail,
    ArtifactSummary,
    BatchReviewResult,
    CommentRequest,
    ContactSheet,
    ContactTile,
    CreateProjectRequest,
    EditContentRequest,
    GenerateRequest,
    JobResponse,
    ProjectDetail,
    ProjectSummary,
    ReviewRequest,
    SceneSummary,
    SkippedApprovalDetail,
    VersionDetail,
    VersionSummary,
)
from videoforge.services.admission import AdmissionError
from videoforge.services.jobs import JobService
from videoforge.services.review import ReviewService, StaleVersionError
from videoforge_domain.artifact_lifecycle import IllegalTransitionError, capabilities
from videoforge_shared.enums import ArtifactKind, ArtifactState, SubjectType
from videoforge_shared.settings import AppSettings
from videoforge_shared.tasks import STAGE_TASKS

logger = logging.getLogger(__name__)

projects_blueprint = Blueprint("projects", __name__)


def _body[T: BaseModel](model: type[T]) -> T:
    """Parse and validate the request body, or raise a 400.

    Validation happens at the boundary and nowhere else (SADD §21): a service
    receiving a DTO can assume it is well-formed, which is what keeps the
    services free of defensive checks.
    """
    payload = request.get_json(silent=True)
    if payload is None:
        raise ApiError(400, "Invalid request", "expected a JSON object body")
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        raise ApiError(400, "Invalid request", _first_error(exc)) from exc


def _first_error(exc: ValidationError) -> str:
    """One readable message. The full error list is developer-facing noise in
    a UI toast; the correlation id in the problem body reaches the log that
    has everything."""
    first = exc.errors()[0]
    location = ".".join(str(p) for p in first["loc"]) or "body"
    return f"{location}: {first['msg']}"


def _ok(model: BaseModel, status: int = 200) -> tuple[Response, int]:
    return jsonify(model.model_dump(mode="json")), status


# --------------------------------------------------------------------------- #
# Projects
# --------------------------------------------------------------------------- #


@projects_blueprint.post("/projects")
def create_project() -> tuple[Response, int]:
    body = _body(CreateProjectRequest)
    with transaction() as uow:
        workspace = uow.workspaces.sole()
        if workspace is None:
            raise ApiError(
                409,
                "No workspace",
                "the database has no workspace; run `make seed`",
            )
        project = uow.projects.create(
            workspace_id=workspace.id,
            topic=body.topic,
            series_id=body.series_id,
            title=body.title,
        )
        uow.flush()
        return _ok(ProjectSummary.of(project), 201)


@projects_blueprint.get("/projects")
def list_projects() -> tuple[Response, int]:
    limit = min(request.args.get("limit", default=50, type=int), 200)
    offset = max(request.args.get("offset", default=0, type=int), 0)
    with transaction() as uow:
        workspace = uow.workspaces.sole()
        if workspace is None:
            return jsonify({"items": []}), 200
        projects = uow.projects.for_workspace(workspace.id, limit=limit, offset=offset)
        items = [ProjectSummary.of(p).model_dump(mode="json") for p in projects]
        return jsonify({"items": items}), 200


@projects_blueprint.delete("/projects/<project_id>")
def delete_project(project_id: str) -> tuple[Response, int]:
    """Delete a project and everything hanging off it.

    **Irreversible, and the only endpoint in this API that destroys anything.**
    Every other write appends: a rejection is a row, a regeneration is a new
    version, even releasing a stuck stage leaves the dead job in place. This
    one removes rows, and the confirmation that guards it lives in the client
    because that is where the person is — a second endpoint asking "are you
    sure" would be a state machine for a decision made in one second.

    204 with no body. There is nothing left to describe, and returning the
    deleted project would invite a client to render something that no longer
    exists.

    What survives is deliberate and documented on ``ProjectRepository.delete``:
    the audit trail (immutable by §10.3, and a deletion is exactly when "what
    happened to that video?" gets asked) and the stored bytes (content-
    addressed and shared between projects, so deleting them would break a
    different video).
    """
    with transaction() as uow:
        project = uow.projects.get(project_id)
        if project is None:
            raise ApiError(404, "Not found", f"no project {project_id}")

        # The tombstone, written before the rows go. ``SubjectType`` has no
        # bare ``PROJECT``; ``PROJECT_PHASE`` is the existing project-scoped
        # subject and its ``subject_id`` is already a project id (see
        # ``projection.refresh_project_state``). Reusing it beats an
        # ``ALTER TYPE`` migration (§10.4) to improve one label — the
        # ``event_type`` carries the meaning, and the subject only has to say
        # what kind of id this is.
        #
        # This is the one write that must precede the delete rather than
        # follow it: afterwards there is no topic left to record.
        uow.audit.record_event(
            event_type="project.deleted",
            subject_type=SubjectType.PROJECT_PHASE,
            subject_id=project_id,
            payload={"topic": project.topic, "phase": project.phase},
        )
        uow.projects.delete(project)
        uow.flush()

    return Response(status=204), 204


@projects_blueprint.get("/projects/<project_id>")
def get_project(project_id: str) -> tuple[Response, int]:
    with transaction() as uow:
        project = uow.projects.get(project_id)
        if project is None:
            raise ApiError(404, "Not found", f"no project {project_id}")
        artifacts = uow.artifacts.for_project(project_id)
        return _ok(
            ProjectDetail.of_detail(
                project, artifacts, uow.scenes.for_approved_set(project_id)
            )
        )


# --------------------------------------------------------------------------- #
# Generation
# --------------------------------------------------------------------------- #


@projects_blueprint.post("/projects/<project_id>/generations")
def request_generation(project_id: str) -> tuple[Response, int]:
    """202 Accepted with a job id — the async pattern of §19.2.

    Long work never happens in a request handler. The response is a receipt,
    and the client polls ``GET /jobs/{id}`` (ADR-006 commits to polling first;
    SSE waits for M5 behind a flag).
    """
    body = _body(GenerateRequest)
    spec = STAGE_TASKS.get(body.stage)
    if spec is None:
        raise ApiError(
            400,
            "Stage not available",
            f"{body.stage.value!r} is not implemented yet; available: "
            f"{', '.join(sorted(k.value for k in STAGE_TASKS))}",
        )

    with transaction() as uow:
        if uow.projects.get(project_id) is None:
            raise ApiError(404, "Not found", f"no project {project_id}")
        service = JobService(uow, dispatcher())
        try:
            outcome = service.request(
                project_id=project_id,
                kind=body.stage,
                spec=spec,
                scene_ref=body.scene_id,
                regenerate=body.regenerate,
                input_extra=(
                    {"max_scenes": body.max_scenes} if body.max_scenes else None
                ),
            )
        except AdmissionError as exc:
            # The stage's inputs are not approved, or the project has no
            # branding (M3-06). Well-formed request, world not ready — 409.
            # Ahead of IllegalTransitionError in the handler order because it
            # is raised earlier and is the more actionable message.
            raise ApiError(409, "Conflict", str(exc)) from exc
        except IllegalTransitionError as exc:
            # The world moved: a worker is already generating, or the artifact
            # is in a state this request does not apply to. Well-formed
            # request, wrong moment — 409, not 400.
            raise ApiError(409, "Conflict", str(exc)) from exc
        job_id = outcome.job.id
        created = outcome.created

    # Published only after the transaction committed — a message describing
    # work that may still roll back is worse than a slightly later message.
    service.dispatch_pending()

    return (
        jsonify({"job_id": job_id, "created": created}),
        202,
    )


@projects_blueprint.get("/jobs/<job_id>")
def get_job(job_id: str) -> tuple[Response, int]:
    with transaction() as uow:
        job = uow.jobs.get(job_id)
        if job is None:
            raise ApiError(404, "Not found", f"no job {job_id}")
        return _ok(JobResponse.of(job))


# --------------------------------------------------------------------------- #
# Artifacts and versions
# --------------------------------------------------------------------------- #


@projects_blueprint.get("/artifacts/<artifact_id>")
def get_artifact(artifact_id: str) -> tuple[Response, int]:
    with transaction() as uow:
        artifact = uow.artifacts.get(artifact_id)
        if artifact is None:
            raise ApiError(404, "Not found", f"no artifact {artifact_id}")
        versions = uow.versions.history(artifact_id)
        statuses = {
            row.artifact_version_id: row
            for row in uow.versions.statuses_for_artifact(artifact_id)
        }
        return _ok(
            ArtifactDetail.of_detail(
                artifact,
                [VersionSummary.of(v, statuses.get(v.id)) for v in versions],
            )
        )


def _bucket_for(kind: ArtifactKind) -> str:
    """Which bucket a kind's bytes live in (ADR-011).

    One function rather than a literal at each call site: the split is real —
    generated *inputs* (frames, narration) go to assets, finished *outputs*
    (renders, packages) go to artifacts — and a second copy of that rule is a
    second thing to get wrong when a bucket moves. Text stages have no bytes
    and never reach here.
    """
    settings: AppSettings = current_app.config["VIDEOFORGE_SETTINGS"]
    if kind in (ArtifactKind.RENDER, ArtifactKind.PACKAGE):
        return str(settings.minio.bucket_artifacts)
    return str(settings.minio.bucket_assets)


@projects_blueprint.post("/artifacts/<artifact_id>/release")
def release_artifact(artifact_id: str) -> tuple[Response, int]:
    """Free a stage whose job will never finish (M5-05).

    **Keyed on the artifact, not the job**, because the artifact id is what the
    UI has: ``StageSummary`` carries it and nothing in the client ever sees a
    job id. An endpoint the screen cannot address is the shape this bug was
    already in — ``JobService.cancel`` has existed since M1-04 with no route
    reaching it, which is why a parked job needed psql.

    409 rather than 404 when the stage is not generating: the request is
    well-formed and the world is simply not in the state it describes, which is
    the same distinction ``AdmissionError`` already carries.
    """
    with transaction() as uow:
        artifact = uow.artifacts.get(artifact_id)
        if artifact is None:
            raise ApiError(404, "Not found", f"no artifact {artifact_id}")

        if not JobService(uow, dispatcher()).release(artifact):
            raise ApiError(
                409,
                "Not stuck",
                f"{artifact.kind} is {artifact.state}, not generating; "
                "there is no in-flight job to release",
            )
        uow.flush()
        return _ok(ArtifactSummary.of(artifact))


@projects_blueprint.get("/artifacts/<artifact_id>/versions/<int:version_no>")
def get_version(artifact_id: str, version_no: int) -> tuple[Response, int]:
    with transaction() as uow:
        artifact = uow.artifacts.get(artifact_id)
        if artifact is None:
            raise ApiError(404, "Not found", f"no artifact {artifact_id}")
        versions = uow.versions.history(artifact_id)
        match = next((v for v in versions if v.version_no == version_no), None)
        if match is None:
            raise ApiError(
                404, "Not found", f"artifact {artifact_id} has no version {version_no}"
            )
        return _ok(
            VersionDetail.of_detail(
                match,
                uow.versions.status_of(match.id),
                bucket=_bucket_for(ArtifactKind(artifact.kind)),
            )
        )


@projects_blueprint.put("/artifacts/<artifact_id>/content")
def edit_content(artifact_id: str) -> tuple[Response, int]:
    """A human edit → a new version with ``origin=human_edit`` (§10.3 rule 3)."""
    body = _body(EditContentRequest)
    with transaction() as uow:
        try:
            outcome = ReviewService(uow).edit(artifact_id, body.content)
        except LookupError as exc:
            raise ApiError(404, "Not found", str(exc)) from exc
        except IllegalTransitionError as exc:
            raise ApiError(409, "Conflict", str(exc)) from exc
        uow.flush()
        return _ok(
            VersionDetail.of_detail(
                outcome.version, uow.versions.status_of(outcome.version.id)
            ),
            201,
        )


# --------------------------------------------------------------------------- #
# Review
# --------------------------------------------------------------------------- #


@projects_blueprint.post("/artifact-versions/<version_id>/reviews/approve")
def approve_version(version_id: str) -> tuple[Response, int]:
    return _review(version_id, approve=True)


@projects_blueprint.post("/artifact-versions/<version_id>/reviews/reject")
def reject_version(version_id: str) -> tuple[Response, int]:
    return _review(version_id, approve=False)


def _review(version_id: str, *, approve: bool) -> tuple[Response, int]:
    body = _body(ReviewRequest)
    with transaction() as uow:
        service = ReviewService(uow)
        try:
            if approve:
                outcome = service.approve(
                    version_id,
                    comment=body.comment,
                    expected_version_no=body.expected_version_no,
                )
            else:
                # Structured reasons only reach a rejection (M3-10): they are
                # what the next attempt's correction block is built from, and
                # a correction derived from an approval would be nonsense.
                outcome = service.reject(
                    version_id,
                    comment=body.comment,
                    reasons=[reason.value for reason in body.reasons],
                    expected_version_no=body.expected_version_no,
                )
        except LookupError as exc:
            raise ApiError(404, "Not found", str(exc)) from exc
        except StaleVersionError as exc:
            # Someone regenerated while this reviewer was reading. Approving
            # anyway would attach a decision to content nobody looked at.
            raise ApiError(409, "Stale version", str(exc)) from exc
        except IllegalTransitionError as exc:
            raise ApiError(409, "Conflict", str(exc)) from exc
        uow.flush()
        return _ok(ArtifactSummary.of(outcome.artifact))


@projects_blueprint.get("/projects/<project_id>/contact-sheet/<kind>")
def contact_sheet(project_id: str, kind: str) -> tuple[Response, int]:
    """Every scene of one kind as a grid (M3-09, risk R9).

    One request for twenty scenes rather than twenty. The per-scene review
    panel still exists for the ones that need a closer look; this is the sweep.

    ``pending_version_ids`` is the batch the "approve all remaining" button
    posts back, computed here from the FSM's own ``can_approve`` — so the set
    the client submits is exactly the set the server would allow, and nothing
    in TypeScript re-derives it.
    """
    try:
        artifact_kind = ArtifactKind(kind)
    except ValueError as exc:
        raise ApiError(400, "Invalid request", f"unknown kind {kind!r}") from exc

    settings: AppSettings = current_app.config["VIDEOFORGE_SETTINGS"]
    bucket = settings.minio.bucket_assets

    with transaction() as uow:
        if uow.projects.get(project_id) is None:
            raise ApiError(404, "Not found", f"no project {project_id}")

        scenes = uow.scenes.for_approved_set(project_id)
        by_scene = {
            artifact.scene_ref: artifact
            for artifact in uow.artifacts.for_project(project_id)
            if ArtifactKind(artifact.kind) is artifact_kind and artifact.scene_ref
        }

        tiles: list[ContactTile] = []
        pending: list[str] = []
        for scene in scenes:
            artifact = by_scene.get(scene.id)
            tile = ContactTile(
                scene_id=scene.id,
                scene_index=scene.index,
                narration=SceneSummary.of(scene).narration,
            )
            if artifact is not None:
                caps = capabilities(ArtifactState(artifact.state))
                version = uow.versions.latest(artifact.id)
                tile = tile.model_copy(
                    update={
                        "artifact_id": artifact.id,
                        "state": artifact.state.value,
                        "stale_since": artifact.stale_since,
                        "capabilities": caps,
                        "version_id": version.id if version else None,
                        "version_no": version.version_no if version else None,
                        # Built server-side: which bucket media lives in is a
                        # server fact (ADR-011), and a client composing the
                        # path would be a second place to change when it moves.
                        "asset_url": (
                            f"/assets/{bucket}/{version.storage_key}"
                            if version and version.storage_key
                            else None
                        ),
                    }
                )
                if caps.get("can_approve") and version is not None:
                    pending.append(version.id)
            tiles.append(tile)

        # The set-level artifact belongs in the batch, and its absence was a
        # dead end (M4-12).
        #
        # A per-scene stage produces N picture artifacts **and** the
        # project-wide row `JobService.request` created, which the worker
        # completes with a manifest. Stage state is the *least advanced* of a
        # kind, so leaving that row AWAITING_APPROVAL holds the whole stage
        # there — and `by_scene` above filters it out, so it was neither a tile
        # nor in this list. "Approve all remaining (6)" therefore approved six
        # pictures, reported success, and left `image` unapproved with nothing
        # on screen still pending: `timeline` could never be generated through
        # the UI at all.
        #
        # Appended rather than turned into a tile: it has no picture, and a
        # blank cell in a contact sheet is a scene that failed. It stays out of
        # `pending`, which counts what a reviewer is *looking* at — hence the
        # count being taken here, before the append, rather than from the list.
        pending_tiles = len(pending)

        manifest = uow.artifacts.find(project_id, artifact_kind, None)
        if manifest is not None and capabilities(ArtifactState(manifest.state)).get(
            "can_approve"
        ):
            manifest_version = uow.versions.latest(manifest.id)
            if manifest_version is not None:
                pending.append(manifest_version.id)

        return _ok(
            ContactSheet(
                kind=artifact_kind.value,
                tiles=tiles,
                total=len(tiles),
                pending=pending_tiles,
                pending_version_ids=pending,
            )
        )


@projects_blueprint.post("/projects/<project_id>/reviews/approve-remaining")
def approve_remaining(project_id: str) -> tuple[Response, int]:
    """Approve a set of versions in one transaction (M3-09).

    200 even when some were skipped: partial success is the honest outcome,
    and the body says which ones and why. Failing the whole batch would make
    one raced tile cost the reviewer the entire pass.
    """
    body = _body(ApproveManyRequest)
    with transaction() as uow:
        if uow.projects.get(project_id) is None:
            raise ApiError(404, "Not found", f"no project {project_id}")
        outcome = ReviewService(uow).approve_many(
            body.version_ids, comment=body.comment
        )
        uow.flush()
        result = BatchReviewResult(
            approved=len(outcome.approved),
            skipped=[
                SkippedApprovalDetail(version_id=s.version_id, reason=s.reason)
                for s in outcome.skipped
            ],
        )
    return _ok(result)


@projects_blueprint.post("/artifact-versions/<version_id>/comments")
def add_comment(version_id: str) -> tuple[Response, int]:
    body = _body(CommentRequest)
    with transaction() as uow:
        if uow.versions.get(version_id) is None:
            raise ApiError(404, "Not found", f"no artifact version {version_id}")
        ReviewService(uow).comment(version_id, body.body, anchor=body.anchor)
    return jsonify({"status": "created"}), 201


@projects_blueprint.get("/artifact-versions/<version_id>/comments")
def list_comments(version_id: str) -> tuple[Response, int]:
    with transaction() as uow:
        comments = uow.comments.for_version(version_id)
        return (
            jsonify(
                {
                    "items": [
                        {
                            "id": c.id,
                            "body": c.body,
                            "author_id": c.author_id,
                            "anchor": c.anchor,
                            "created_at": c.created_at.isoformat(),
                        }
                        for c in comments
                    ]
                }
            ),
            200,
        )
