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

from flask import Blueprint, Response, jsonify, request
from pydantic import BaseModel, ValidationError
from videoforge_domain.artifact_lifecycle import IllegalTransitionError

from videoforge.api.deps import dispatcher, transaction
from videoforge.api.errors import ApiError
from videoforge.dto import (
    ArtifactDetail,
    ArtifactSummary,
    CommentRequest,
    CreateProjectRequest,
    EditContentRequest,
    GenerateRequest,
    JobResponse,
    ProjectDetail,
    ProjectSummary,
    ReviewRequest,
    VersionDetail,
    VersionSummary,
)
from videoforge.services.jobs import JobService
from videoforge.services.review import ReviewService, StaleVersionError
from videoforge_shared.enums import ArtifactKind
from videoforge_shared.tasks import SCRIPT_GENERATE, TaskSpec

logger = logging.getLogger(__name__)

projects_blueprint = Blueprint("projects", __name__)

#: Which task produces which artifact kind. A dict rather than a naming
#: convention so an unimplemented stage is a 400 with a list, not a message
#: published to a queue nothing consumes. M2+ fill in the rest.
STAGE_TASKS: dict[ArtifactKind, TaskSpec] = {
    ArtifactKind.SCRIPT: SCRIPT_GENERATE,
}


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


@projects_blueprint.get("/projects/<project_id>")
def get_project(project_id: str) -> tuple[Response, int]:
    with transaction() as uow:
        project = uow.projects.get(project_id)
        if project is None:
            raise ApiError(404, "Not found", f"no project {project_id}")
        artifacts = uow.artifacts.for_project(project_id)
        return _ok(ProjectDetail.of_detail(project, artifacts))


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
            )
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


@projects_blueprint.get("/artifacts/<artifact_id>/versions/<int:version_no>")
def get_version(artifact_id: str, version_no: int) -> tuple[Response, int]:
    with transaction() as uow:
        versions = uow.versions.history(artifact_id)
        match = next((v for v in versions if v.version_no == version_no), None)
        if match is None:
            raise ApiError(
                404, "Not found", f"artifact {artifact_id} has no version {version_no}"
            )
        return _ok(VersionDetail.of_detail(match, uow.versions.status_of(match.id)))


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
        action = service.approve if approve else service.reject
        try:
            outcome = action(
                version_id,
                comment=body.comment,
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
