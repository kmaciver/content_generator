"""M1-08: the API, against a real database and a recording dispatcher.

Uses the real Flask app rather than calling services directly, because the
things worth checking here are transport concerns the service layer cannot
express: status codes, problem+json shapes, and whether the capabilities
payload actually reaches the client.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import Engine
from sqlalchemy.orm import sessionmaker

from videoforge.app import create_app
from videoforge.config import AppSettings
from videoforge.services.dispatch import RecordingDispatcher
from videoforge_persistence.models import Workspace
from videoforge_persistence.uow import unit_of_work
from videoforge_shared.enums import ArtifactKind, ArtifactState, VersionOrigin
from videoforge_shared.ids import new_ulid
from videoforge_shared.settings import load_app_settings
from videoforge_shared.tasks import SCRIPT_GENERATE

pytestmark = pytest.mark.integration


@pytest.fixture()
def dispatcher() -> RecordingDispatcher:
    return RecordingDispatcher()


@pytest.fixture()
def app(db_engine: Engine, dispatcher: RecordingDispatcher) -> Iterator[Flask]:
    settings: AppSettings = load_app_settings()
    application = create_app(settings, dispatcher=dispatcher, engine=db_engine)
    application.config.update(TESTING=True)
    yield application


@pytest.fixture()
def client(app: Flask) -> FlaskClient:
    return app.test_client()


@pytest.fixture()
def workspace(db_engine: Engine) -> Iterator[str]:
    sessions = sessionmaker(bind=db_engine, expire_on_commit=False)
    workspace_id = new_ulid()
    with unit_of_work(sessions) as uow:
        uow.session.add(Workspace(id=workspace_id, name="api-test"))
    yield workspace_id
    with unit_of_work(sessions) as uow:
        uow.session.execute(
            sa.text("DELETE FROM workspace WHERE id = :id"), {"id": workspace_id}
        )
        uow.session.execute(sa.text("DELETE FROM outbox_event"))


def _create_project(client: FlaskClient, topic: str = "photosynthesis") -> str:
    response = client.post("/api/v1/projects", json={"topic": topic})
    assert response.status_code == 201, response.get_json()
    return str(response.get_json()["id"])


class TestProjects:
    def test_create_and_fetch(self, client: FlaskClient, workspace: str) -> None:
        project_id = _create_project(client)
        response = client.get(f"/api/v1/projects/{project_id}")
        assert response.status_code == 200
        body = response.get_json()
        assert body["topic"] == "photosynthesis"
        assert body["phase"] == "DRAFT"
        assert body["artifacts"] == []

    def test_unknown_project_is_problem_json(
        self, client: FlaskClient, workspace: str
    ) -> None:
        response = client.get("/api/v1/projects/01NOPE")
        assert response.status_code == 404
        assert response.mimetype == "application/problem+json"
        body = response.get_json()
        assert body["status"] == 404
        # The correlation id is how a user's screenshot reaches the server log.
        assert body["correlation_id"]

    def test_rejects_unknown_fields(self, client: FlaskClient, workspace: str) -> None:
        """``extra="forbid"``: a typo'd field must not be silently dropped.

        Accepting and ignoring it would let a client believe it set something
        it did not — the most confusing possible outcome.
        """
        response = client.post("/api/v1/projects", json={"topic": "x", "titel": "typo"})
        assert response.status_code == 400

    def test_rejects_empty_topic(self, client: FlaskClient, workspace: str) -> None:
        assert client.post("/api/v1/projects", json={"topic": ""}).status_code == 400

    def test_non_json_body_is_400(self, client: FlaskClient, workspace: str) -> None:
        response = client.post(
            "/api/v1/projects", data="topic=x", content_type="text/plain"
        )
        assert response.status_code == 400


class TestGeneration:
    def test_returns_202_and_dispatches(
        self,
        client: FlaskClient,
        workspace: str,
        dispatcher: RecordingDispatcher,
    ) -> None:
        """The API never generates — it returns a receipt (§19.2)."""
        project_id = _create_project(client)
        response = client.post(
            f"/api/v1/projects/{project_id}/generations", json={"stage": "script"}
        )
        assert response.status_code == 202
        body = response.get_json()
        assert body["job_id"]
        assert body["created"] is True

        assert len(dispatcher.sent) == 1
        spec, kwargs = dispatcher.sent[0]
        assert spec == SCRIPT_GENERATE
        assert kwargs == {"job_id": body["job_id"]}

    def test_duplicate_request_does_not_dispatch_twice(
        self,
        client: FlaskClient,
        workspace: str,
        dispatcher: RecordingDispatcher,
    ) -> None:
        """The double-click. One intent, one job, one broker message."""
        project_id = _create_project(client)
        first = client.post(
            f"/api/v1/projects/{project_id}/generations", json={"stage": "script"}
        ).get_json()
        second = client.post(
            f"/api/v1/projects/{project_id}/generations", json={"stage": "script"}
        ).get_json()

        assert first["job_id"] == second["job_id"]
        assert first["created"] is True
        assert second["created"] is False
        assert len(dispatcher.sent) == 1

    def test_unimplemented_stage_is_400_with_options(
        self, client: FlaskClient, workspace: str
    ) -> None:
        """M2+ stages are not wired yet. Better a 400 naming what exists than
        a message on a queue nothing consumes."""
        project_id = _create_project(client)
        response = client.post(
            f"/api/v1/projects/{project_id}/generations", json={"stage": "image"}
        )
        assert response.status_code == 400
        assert "script" in response.get_json()["detail"]

    def test_job_status_is_readable(self, client: FlaskClient, workspace: str) -> None:
        project_id = _create_project(client)
        job_id = client.post(
            f"/api/v1/projects/{project_id}/generations", json={"stage": "script"}
        ).get_json()["job_id"]

        response = client.get(f"/api/v1/jobs/{job_id}")
        assert response.status_code == 200
        body = response.get_json()
        assert body["status"] == "QUEUED"
        assert body["queue"] == "llm"
        assert body["attempt"] == 0


class TestReview:
    @pytest.fixture()
    def version(
        self, client: FlaskClient, db_engine: Engine, workspace: str
    ) -> dict[str, Any]:
        """A project with one generated script version awaiting approval."""
        project_id = _create_project(client)
        client.post(
            f"/api/v1/projects/{project_id}/generations", json={"stage": "script"}
        )
        sessions = sessionmaker(bind=db_engine, expire_on_commit=False)
        with unit_of_work(sessions) as uow:
            artifact = uow.artifacts.find(project_id, ArtifactKind.SCRIPT)
            assert artifact is not None
            version = uow.versions.add_version(
                artifact,
                origin=VersionOrigin.GENERATED,
                content_hash="h1",
                inline_content={"title": "T", "script": "body"},
            )
            artifact.state = ArtifactState.AWAITING_APPROVAL
            return {
                "project_id": project_id,
                "artifact_id": artifact.id,
                "version_id": version.id,
                "version_no": version.version_no,
            }

    def test_capabilities_reach_the_client(
        self, client: FlaskClient, version: dict[str, Any]
    ) -> None:
        """§11: the UI renders buttons from this, and never decides itself."""
        response = client.get(f"/api/v1/artifacts/{version['artifact_id']}")
        assert response.status_code == 200
        caps = response.get_json()["capabilities"]
        assert caps == {
            "can_approve": True,
            "can_reject": True,
            "can_regenerate": True,
            "can_edit": True,
        }

    def test_version_status_is_derived(
        self, client: FlaskClient, version: dict[str, Any]
    ) -> None:
        """Finding B1 surfacing through the API: no stored status column."""
        body = client.get(f"/api/v1/artifacts/{version['artifact_id']}").get_json()
        assert body["versions"][0]["status"] == "AWAITING_APPROVAL"

    def test_approve(self, client: FlaskClient, version: dict[str, Any]) -> None:
        response = client.post(
            f"/api/v1/artifact-versions/{version['version_id']}/reviews/approve",
            json={"expected_version_no": version["version_no"]},
        )
        assert response.status_code == 200
        assert response.get_json()["state"] == "APPROVED"

        body = client.get(f"/api/v1/artifacts/{version['artifact_id']}").get_json()
        assert body["versions"][0]["status"] == "APPROVED"
        assert body["capabilities"]["can_approve"] is False

    def test_reject_then_regenerate(
        self,
        client: FlaskClient,
        version: dict[str, Any],
        dispatcher: RecordingDispatcher,
    ) -> None:
        """The M1 exit loop, over HTTP."""
        assert (
            client.post(
                f"/api/v1/artifact-versions/{version['version_id']}/reviews/reject",
                json={},
            ).status_code
            == 200
        )
        response = client.post(
            f"/api/v1/projects/{version['project_id']}/generations",
            json={"stage": "script", "regenerate": True},
        )
        assert response.status_code == 202
        assert response.get_json()["created"] is True

    def test_stale_version_is_409(
        self, client: FlaskClient, version: dict[str, Any]
    ) -> None:
        """Two tabs. An approval must not land on content nobody read."""
        response = client.post(
            f"/api/v1/artifact-versions/{version['version_id']}/reviews/approve",
            json={"expected_version_no": version["version_no"] + 5},
        )
        assert response.status_code == 409
        assert response.mimetype == "application/problem+json"

    def test_approving_twice_is_409(
        self, client: FlaskClient, version: dict[str, Any]
    ) -> None:
        """The FSM refuses; the API translates. Not a 400 — the request was
        well-formed, the world moved."""
        path = f"/api/v1/artifact-versions/{version['version_id']}/reviews/approve"
        assert client.post(path, json={}).status_code == 200
        assert client.post(path, json={}).status_code == 409

    def test_human_edit_creates_a_new_version(
        self, client: FlaskClient, version: dict[str, Any]
    ) -> None:
        """§10.3 rule 3 — mechanically identical, distinguishable in audit."""
        response = client.put(
            f"/api/v1/artifacts/{version['artifact_id']}/content",
            json={"content": {"title": "Edited", "script": "my own words"}},
        )
        assert response.status_code == 201
        body = response.get_json()
        assert body["version_no"] == version["version_no"] + 1
        assert body["origin"] == "human_edit"
        # An edit is not an approval: it still faces the gate.
        assert body["status"] == "AWAITING_APPROVAL"

    def test_comments_round_trip(
        self, client: FlaskClient, version: dict[str, Any]
    ) -> None:
        assert (
            client.post(
                f"/api/v1/artifact-versions/{version['version_id']}/comments",
                json={"body": "tighten the intro"},
            ).status_code
            == 201
        )
        items = client.get(
            f"/api/v1/artifact-versions/{version['version_id']}/comments"
        ).get_json()["items"]
        assert [c["body"] for c in items] == ["tighten the intro"]

    def test_version_detail_carries_content_and_provenance(
        self, client: FlaskClient, version: dict[str, Any]
    ) -> None:
        response = client.get(
            f"/api/v1/artifacts/{version['artifact_id']}/versions/"
            f"{version['version_no']}"
        )
        assert response.status_code == 200
        body = response.get_json()
        assert body["content"]["script"] == "body"
        assert body["content_hash"] == "h1"
