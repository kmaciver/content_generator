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
from videoforge_shared.tasks import RESEARCH_GENERATE

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
            f"/api/v1/projects/{project_id}/generations", json={"stage": "research"}
        )
        assert response.status_code == 202
        body = response.get_json()
        assert body["job_id"]
        assert body["created"] is True

        assert len(dispatcher.sent) == 1
        spec, kwargs = dispatcher.sent[0]
        assert spec == RESEARCH_GENERATE
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
            f"/api/v1/projects/{project_id}/generations", json={"stage": "research"}
        ).get_json()
        second = client.post(
            f"/api/v1/projects/{project_id}/generations", json={"stage": "research"}
        ).get_json()

        assert first["job_id"] == second["job_id"]
        assert first["created"] is True
        assert second["created"] is False
        assert len(dispatcher.sent) == 1

    def test_unimplemented_stage_is_400_with_options(
        self, client: FlaskClient, workspace: str
    ) -> None:
        """Later stages are not wired yet. Better a 400 naming what exists than
        a message on a queue nothing consumes.

        ``music`` rather than ``package``, and this is the **fourth** move —
        M3-07, M3-12, M4-08 and now M5-03 each implemented the stage this test
        was standing on. That it keeps failing is the signal it is doing its
        job: it breaks the moment its own premise stops being true, rather than
        passing for the wrong reason.

        The move is different this time. Every stage in the pipeline is now
        implemented, so there is no unimplemented *stage* left to name and the
        marker has to come off the DAG entirely. ``music`` is an
        ``ArtifactKind`` without a stage — S4 deferred the library and v1 ships
        without music — so it parses as a kind, finds no task, and takes this
        path. If music is ever wired, this test has run out of subjects and
        should be deleted rather than moved a fifth time.

        An implemented stage on a bare project answers 409 instead — the
        admission check talking, which is a different rule with its own tests.
        """
        project_id = _create_project(client)
        response = client.post(
            f"/api/v1/projects/{project_id}/generations", json={"stage": "music"}
        )
        assert response.status_code == 400
        assert "script" in response.get_json()["detail"]

    def test_job_status_is_readable(self, client: FlaskClient, workspace: str) -> None:
        project_id = _create_project(client)
        job_id = client.post(
            f"/api/v1/projects/{project_id}/generations", json={"stage": "research"}
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
        """A project with one generated version awaiting approval.

        Uses ``research`` since M3-06: it is a *root* stage, so the admission
        gate has nothing to refuse. The review mechanics under test here are
        identical for any stage — script would only add an approved-research
        setup step that has nothing to do with reviewing.
        """
        project_id = _create_project(client)
        client.post(
            f"/api/v1/projects/{project_id}/generations", json={"stage": "research"}
        )
        sessions = sessionmaker(bind=db_engine, expire_on_commit=False)
        with unit_of_work(sessions) as uow:
            artifact = uow.artifacts.find(project_id, ArtifactKind.RESEARCH)
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
            json={"stage": "research", "regenerate": True},
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


class TestAssetUrls:
    """M4-11. Which bucket a kind's bytes live in is a server fact (ADR-011)."""

    def _version_with_key(
        self,
        client: FlaskClient,
        db_engine: Engine,
        kind: ArtifactKind,
    ) -> dict[str, Any]:
        project_id = _create_project(client)
        sessions = sessionmaker(bind=db_engine, expire_on_commit=False)
        with unit_of_work(sessions) as uow:
            artifact = uow.artifacts.create(project_id, kind, None)
            uow.flush()
            version = uow.versions.add_version(
                artifact,
                origin=VersionOrigin.GENERATED,
                content_hash="h",
                storage_key="ab/abc/thing.bin",
            )
            artifact.state = ArtifactState.AWAITING_APPROVAL
            return {"artifact_id": artifact.id, "version_no": version.version_no}

    def test_a_render_points_at_the_artifacts_bucket(
        self, client: FlaskClient, db_engine: Engine, workspace: str
    ) -> None:
        """The bug this endpoint change exists for: the review screen used to
        compose `/assets/assets/{key}` itself, which is right for images and
        voice and **wrong for a render** — those are written to the artifacts
        bucket, so the client's guess would have 403'd on the first video the
        pipeline ever produced."""
        row = self._version_with_key(client, db_engine, ArtifactKind.RENDER)
        body = client.get(
            f"/api/v1/artifacts/{row['artifact_id']}/versions/{row['version_no']}"
        ).get_json()
        assert body["asset_url"] == "/assets/artifacts/ab/abc/thing.bin"

    def test_a_package_points_at_the_artifacts_bucket(
        self, client: FlaskClient, db_engine: Engine, workspace: str
    ) -> None:
        """M5-03/04. A package is a finished *output*, like a render — and the
        review screen's download link is this URL, so a package routed to the
        assets bucket would 403 on the one artifact a user actually takes away.
        """
        row = self._version_with_key(client, db_engine, ArtifactKind.PACKAGE)
        body = client.get(
            f"/api/v1/artifacts/{row['artifact_id']}/versions/{row['version_no']}"
        ).get_json()
        assert body["asset_url"] == "/assets/artifacts/ab/abc/thing.bin"

    def test_a_cover_points_at_the_assets_bucket(
        self, client: FlaskClient, db_engine: Engine, workspace: str
    ) -> None:
        """M5-02. The cover is a generated *input* to the package, and lives
        with the frames it is built from."""
        row = self._version_with_key(client, db_engine, ArtifactKind.THUMBNAIL)
        body = client.get(
            f"/api/v1/artifacts/{row['artifact_id']}/versions/{row['version_no']}"
        ).get_json()
        assert body["asset_url"] == "/assets/assets/ab/abc/thing.bin"

    def test_media_points_at_the_assets_bucket(
        self, client: FlaskClient, db_engine: Engine, workspace: str
    ) -> None:
        row = self._version_with_key(client, db_engine, ArtifactKind.VOICE)
        body = client.get(
            f"/api/v1/artifacts/{row['artifact_id']}/versions/{row['version_no']}"
        ).get_json()
        assert body["asset_url"] == "/assets/assets/ab/abc/thing.bin"

    def test_an_inline_version_has_no_asset_url(
        self, client: FlaskClient, db_engine: Engine, workspace: str
    ) -> None:
        """A text stage has no bytes, and a URL to nothing is worse than none:
        the UI branches on its absence."""
        project_id = _create_project(client)
        sessions = sessionmaker(bind=db_engine, expire_on_commit=False)
        with unit_of_work(sessions) as uow:
            artifact = uow.artifacts.create(project_id, ArtifactKind.SCRIPT, None)
            uow.flush()
            version = uow.versions.add_version(
                artifact,
                origin=VersionOrigin.GENERATED,
                content_hash="h",
                inline_content={"script": "words"},
            )
            artifact.state = ArtifactState.AWAITING_APPROVAL
            ids = (artifact.id, version.version_no)

        body = client.get(f"/api/v1/artifacts/{ids[0]}/versions/{ids[1]}").get_json()
        assert body["asset_url"] is None


class TestRejectionVocabulary:
    """M3-10 fix. Every reason in the vocabulary describes a *picture*."""

    def _artifact(
        self, client: FlaskClient, db_engine: Engine, kind: ArtifactKind
    ) -> str:
        project_id = _create_project(client)
        sessions = sessionmaker(bind=db_engine, expire_on_commit=False)
        with unit_of_work(sessions) as uow:
            artifact = uow.artifacts.create(project_id, kind, None)
            uow.flush()
            uow.versions.add_version(
                artifact,
                origin=VersionOrigin.GENERATED,
                content_hash="h",
                inline_content={"x": 1},
            )
            artifact.state = ArtifactState.AWAITING_APPROVAL
            return str(artifact.id)

    def test_an_image_offers_the_full_vocabulary(
        self, client: FlaskClient, db_engine: Engine, workspace: str
    ) -> None:
        artifact_id = self._artifact(client, db_engine, ArtifactKind.IMAGE)
        body = client.get(f"/api/v1/artifacts/{artifact_id}").get_json()
        assert "anatomy" in body["rejection_reasons"]
        assert "character_drift" in body["rejection_reasons"]

    @pytest.mark.parametrize(
        "kind", [ArtifactKind.VOICE, ArtifactKind.SCRIPT, ArtifactKind.TIMELINE]
    )
    def test_non_image_kinds_offer_none(
        self,
        client: FlaskClient,
        db_engine: Engine,
        workspace: str,
        kind: ArtifactKind,
    ) -> None:
        """**The bug.** The review screen rendered one hardcoded list on every
        rejectable artifact, so a reviewer rejecting a narration was asked to
        choose between "Anatomy", "Text in image" and "Framing" — nine image
        failure modes, none of which a voice take can have.

        Empty is a real answer: the comment box carries the rejection, and a
        vocabulary invented before any narration had ever been rejected would
        be guesswork the reviewer answers wrongly.
        """
        artifact_id = self._artifact(client, db_engine, kind)
        body = client.get(f"/api/v1/artifacts/{artifact_id}").get_json()
        assert body["rejection_reasons"] == []


class TestCaptionCues:
    """The preview and the burn agree about captions again.

    They disagreed for the length of M4: ``narration-player.tsx`` kept showing
    one word at a time from M3-12's raw spans while the render burned grouped
    phrases from M4-04. Both now read ``group_into_cues`` — the compiler calls
    it when it compiles, this endpoint calls it when a reviewer looks.

    Derived on read rather than stored beside the audio. Spans are a
    *measurement* of an approved narration and must never be re-derived;
    grouping is a *presentation* choice remade at every compile, so a cue
    frozen at synthesis time would preview rules the render no longer uses.
    """

    def _voice_version(
        self,
        client: FlaskClient,
        db_engine: Engine,
        spans: list[dict[str, Any]],
    ) -> tuple[str, int]:
        project_id = _create_project(client)
        sessions = sessionmaker(bind=db_engine, expire_on_commit=False)
        with unit_of_work(sessions) as uow:
            artifact = uow.artifacts.create(project_id, ArtifactKind.VOICE, None)
            uow.flush()
            version = uow.versions.add_version(
                artifact,
                origin=VersionOrigin.GENERATED,
                content_hash="h",
                storage_key="ab/abc/narration.mp3",
                meta={"duration_ms": 4000, "spans": spans},
            )
            artifact.state = ArtifactState.AWAITING_APPROVAL
            return artifact.id, version.version_no

    @staticmethod
    def _span(
        index: int, words: list[tuple[str, int, int]], kind: str | None = None
    ) -> dict[str, Any]:
        span: dict[str, Any] = {
            "scene_index": index,
            "scene_id": f"scene-{index}",
            "start_ms": words[0][1],
            "end_ms": words[-1][2],
            "words": [
                {"text": text, "start_ms": start, "end_ms": end}
                for text, start, end in words
            ],
        }
        if kind is not None:
            span["kind"] = kind
        return span

    def _cues(
        self, client: FlaskClient, db_engine: Engine, spans: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        artifact_id, version_no = self._voice_version(client, db_engine, spans)
        body = client.get(
            f"/api/v1/artifacts/{artifact_id}/versions/{version_no}"
        ).get_json()
        return list(body["caption_cues"])

    def test_words_arrive_grouped_into_phrases(
        self, client: FlaskClient, db_engine: Engine, workspace: str
    ) -> None:
        """The whole point: six words, not six cues."""
        cues = self._cues(
            client,
            db_engine,
            [
                self._span(
                    1,
                    [
                        ("Pay", 0, 200),
                        ("yourself", 200, 600),
                        ("first,", 600, 900),
                        ("every", 900, 1200),
                        ("single", 1200, 1600),
                        ("month.", 1600, 2000),
                    ],
                )
            ],
        )
        assert [cue["text"] for cue in cues] == [
            "Pay yourself first,",
            "every single month.",
        ]
        assert cues[0]["start_ms"] == 0
        assert cues[-1]["end_ms"] == 2000

    def test_a_cue_never_spans_a_scene_change(
        self, client: FlaskClient, db_engine: Engine, workspace: str
    ) -> None:
        """Two words that would happily share a frame, either side of a cut.

        A cue that straddled the boundary would stay on screen while the image
        changed underneath it. The compiler enforces this by grouping one
        scene's words at a time, and so does this.
        """
        cues = self._cues(
            client,
            db_engine,
            [
                self._span(1, [("Save", 0, 300)]),
                self._span(2, [("more", 400, 700)]),
            ],
        )
        assert [cue["text"] for cue in cues] == ["Save", "more"]

    def test_card_scenes_are_not_captioned(
        self, client: FlaskClient, db_engine: Engine, workspace: str
    ) -> None:
        """Matching ``CompileOptions.caption_cards``, which defaults off: a
        card is already text on screen and a caption over it competes with the
        words the scene exists to show. Without this the preview would show a
        caption the render never burns — the same class of divergence this
        whole change is closing."""
        cues = self._cues(
            client,
            db_engine,
            [
                self._span(1, [("Step", 0, 300), ("five.", 300, 600)], kind="card"),
                self._span(2, [("Rinse", 700, 1000)], kind="illustration"),
            ],
        )
        assert [cue["text"] for cue in cues] == ["Rinse"]

    def test_spans_without_a_kind_are_captioned(
        self, client: FlaskClient, db_engine: Engine, workspace: str
    ) -> None:
        """Every voice version stored before M4-01 has no ``kind`` on its
        spans, and every scene that existed then was an illustration. Absent
        reads as illustration — the same direction ``_kind_of`` fails in."""
        cues = self._cues(client, db_engine, [self._span(1, [("Rinse", 0, 300)])])
        assert [cue["text"] for cue in cues] == ["Rinse"]

    def test_a_kind_without_timings_has_no_cues(
        self, client: FlaskClient, db_engine: Engine, workspace: str
    ) -> None:
        """Empty, not absent: the field is always present so the player has one
        shape to render, and a script version simply has nothing to say."""
        project_id = _create_project(client)
        sessions = sessionmaker(bind=db_engine, expire_on_commit=False)
        with unit_of_work(sessions) as uow:
            artifact = uow.artifacts.create(project_id, ArtifactKind.SCRIPT, None)
            uow.flush()
            version = uow.versions.add_version(
                artifact,
                origin=VersionOrigin.GENERATED,
                content_hash="h",
                inline_content={"script": "words"},
            )
            artifact.state = ArtifactState.AWAITING_APPROVAL
            ids = (artifact.id, version.version_no)

        body = client.get(f"/api/v1/artifacts/{ids[0]}/versions/{ids[1]}").get_json()
        assert body["caption_cues"] == []

    def test_malformed_spans_lose_the_captions_not_the_page(
        self, client: FlaskClient, db_engine: Engine, workspace: str
    ) -> None:
        """``meta`` is JSONB and nothing validates it on the way out. A reviewer
        who can still hear the narration has lost far less than one who gets a
        500 on the version they were about to approve."""
        cues = self._cues(
            client,
            db_engine,
            [
                {"scene_index": 1, "scene_id": "s1", "words": "not a list"},
                self._span(2, [("Rinse", 0, 300)]),
            ],
        )
        assert [cue["text"] for cue in cues] == ["Rinse"]


class TestContactSheetBatch:
    """ "Approve all remaining" must actually finish the stage (M4-12).

    **The bug, and why nothing caught it.** A per-scene stage produces N
    artifacts *and* the project-wide row ``JobService.request`` created, which
    the worker completes with a manifest of the batch. The contact sheet builds
    its tiles from artifacts that have a ``scene_ref``, so the manifest was
    neither a tile nor in ``pending_version_ids`` — and stage state is the
    least advanced artifact of a kind. Approving all six pictures therefore
    reported success, left ``image`` awaiting approval, and made ``timeline``
    ungeneratable through the UI with nothing on screen still pending.

    It survived because this endpoint had no test at all. It was found by
    M4-12's e2e on its first run, four stages into a pipeline nobody had ever
    driven end to end in a browser.
    """

    def _project_with_images(
        self, client: FlaskClient, db_engine: Engine
    ) -> tuple[str, list[str], str]:
        """A project with an approved scene set, two scene images, and the
        manifest artifact the fan-out leaves behind."""
        from videoforge_persistence.models import Scene, SceneSet

        project_id = _create_project(client)
        sessions = sessionmaker(bind=db_engine, expire_on_commit=False)
        with unit_of_work(sessions) as uow:
            # `scene_set.script_version_id` is NOT NULL — the set is anchored to
            # the script it was derived from, so the chain is built in full
            # rather than faked.
            script = uow.artifacts.create(project_id, ArtifactKind.SCRIPT, None)
            uow.flush()
            script_version = uow.versions.add_version(
                script,
                origin=VersionOrigin.GENERATED,
                content_hash="script",
                inline_content={"script": "words"},
            )
            script.state = ArtifactState.APPROVED
            uow.flush()

            scene_set = uow.artifacts.create(project_id, ArtifactKind.SCENE_SET, None)
            uow.flush()
            set_version = uow.versions.add_version(
                scene_set,
                origin=VersionOrigin.GENERATED,
                content_hash="scenes",
                inline_content={"scenes": []},
            )
            # Left awaiting a decision and approved through the review endpoint
            # below. `for_approved_set` reads the `artifact_version_status`
            # view, which derives approval from *review rows* rather than from
            # `artifact.state` (B1: the state column is a cache) — so setting
            # the state here would produce a scene set the repository cannot
            # see and a contact sheet with no tiles at all.
            scene_set.state = ArtifactState.AWAITING_APPROVAL
            uow.flush()

            set_id = new_ulid()
            uow.session.add(
                SceneSet(
                    id=set_id,
                    artifact_version_id=set_version.id,
                    script_version_id=script_version.id,
                )
            )
            uow.flush()

            scene_ids = [new_ulid(), new_ulid()]
            for index, scene_id in enumerate(scene_ids, start=1):
                uow.session.add(
                    Scene(
                        id=scene_id,
                        scene_set_id=set_id,
                        index=index,
                        narration_text=f"scene {index}",
                        visual_brief="a brief",
                        target_duration_ms=3000,
                    )
                )
            uow.flush()

            tile_versions = []
            for scene_id in scene_ids:
                artifact = uow.artifacts.create(
                    project_id, ArtifactKind.IMAGE, scene_id
                )
                uow.flush()
                version = uow.versions.add_version(
                    artifact,
                    origin=VersionOrigin.GENERATED,
                    content_hash=f"h{scene_id}",
                    storage_key=f"ab/{scene_id}.png",
                )
                artifact.state = ArtifactState.AWAITING_APPROVAL
                tile_versions.append(version.id)

            # The one the sheet used to ignore.
            manifest = uow.artifacts.create(project_id, ArtifactKind.IMAGE, None)
            uow.flush()
            manifest_version = uow.versions.add_version(
                manifest,
                origin=VersionOrigin.GENERATED,
                content_hash="manifest",
                inline_content={"scene_count": 2},
            )
            manifest.state = ArtifactState.AWAITING_APPROVAL
            ids = (project_id, tile_versions, manifest_version.id, set_version.id)

        approved = client.post(
            f"/api/v1/artifact-versions/{ids[3]}/reviews/approve",
            json={"comment": "scenes look right"},
        )
        assert approved.status_code == 200, approved.get_json()
        return ids[0], ids[1], ids[2]

    def test_the_batch_carries_the_manifest(
        self, client: FlaskClient, db_engine: Engine, workspace: str
    ) -> None:
        project_id, tiles, manifest = self._project_with_images(client, db_engine)
        body = client.get(
            f"/api/v1/projects/{project_id}/contact-sheet/image"
        ).get_json()

        # Two tiles, three approvals. The manifest is in the batch and is not
        # a tile: a blank cell in a contact sheet reads as a scene that failed.
        assert body["total"] == 2
        assert body["pending"] == 2
        assert sorted(body["pending_version_ids"]) == sorted([*tiles, manifest])

    def test_approving_the_batch_finishes_the_stage(
        self, client: FlaskClient, db_engine: Engine, workspace: str
    ) -> None:
        """The assertion the bug actually failed: after one click the *stage*
        is approved, not merely every picture in it."""
        project_id, _, _ = self._project_with_images(client, db_engine)
        sheet = client.get(
            f"/api/v1/projects/{project_id}/contact-sheet/image"
        ).get_json()

        result = client.post(
            f"/api/v1/projects/{project_id}/reviews/approve-remaining",
            json={"version_ids": sheet["pending_version_ids"]},
        )
        assert result.status_code == 200
        assert result.get_json()["approved"] == 3

        detail = client.get(f"/api/v1/projects/{project_id}").get_json()
        states = {a["state"] for a in detail["artifacts"] if a["kind"] == "image"}
        assert states == {"APPROVED"}


class TestReleaseStuckStage:
    """M5-05 — the escape hatch, and the failure it exists for.

    **Observed in M4, not imagined.** A worker discarded a queued message (its
    image had been rebuilt and no longer registered the task name). The job row
    stayed ``QUEUED``, which is a *live* status, so it kept holding
    ``uq_generation_job_live_idempotency_key``. That key is derived from state
    the parked job never advanced — ``task:artifact:v{next}`` — so every retry
    computed the same key and deduplicated onto the corpse. The stage could not
    be run again at all.

    ``JobService.cancel`` already fixed half of it and **nothing could reach
    it**: no route called it, so the cure was psql. These tests pin both
    halves — the key is released, and the artifact leaves ``GENERATING``.
    """

    def _stuck(self, client: FlaskClient) -> tuple[str, str]:
        """A project whose research stage has a job nobody will ever run.

        Exactly what the API does on Generate, and then nothing — which is
        precisely the state a discarded broker message leaves behind.
        """
        project_id = _create_project(client)
        job_id = client.post(
            f"/api/v1/projects/{project_id}/generations", json={"stage": "research"}
        ).get_json()["job_id"]
        return project_id, job_id

    def _artifact(self, client: FlaskClient, project_id: str) -> dict[str, Any]:
        body = client.get(f"/api/v1/projects/{project_id}").get_json()
        return dict(body["artifacts"][0])

    def test_the_stage_is_unrunnable_before_release(
        self, client: FlaskClient, workspace: str
    ) -> None:
        """The bug itself, asserted rather than described. A second Generate
        finds the parked job and reports ``created: false`` — no new work, and
        no way forward."""
        project_id, job_id = self._stuck(client)

        retry = client.post(
            f"/api/v1/projects/{project_id}/generations", json={"stage": "research"}
        ).get_json()
        assert retry["job_id"] == job_id
        assert retry["created"] is False

    def test_release_frees_the_stage(self, client: FlaskClient, workspace: str) -> None:
        """After releasing, the retry makes a **new** job. This is the whole
        point: the idempotency key is no longer held.

        ``regenerate: true``, and that is not a detail. Release lands the
        artifact in ``FAILED``, and the only event ``FAILED`` accepts is
        ``REGENERATE_REQUESTED`` (§12.5 — a retry never reuses the version
        slot). A plain Generate answers 409, which is the FSM being right; the
        UI offers Regenerate for exactly this reason.

        The new job carries the *same* idempotency key — no version was ever
        written, so ``current_version_no`` has not moved. That is precisely why
        the partial unique index is scoped to live rows: a dead job must not
        hold a key forever.
        """
        project_id, job_id = self._stuck(client)
        artifact = self._artifact(client, project_id)

        released = client.post(f"/api/v1/artifacts/{artifact['id']}/release")
        assert released.status_code == 200

        retry = client.post(
            f"/api/v1/projects/{project_id}/generations",
            json={"stage": "research", "regenerate": True},
        ).get_json()
        assert retry["created"] is True
        assert retry["job_id"] != job_id

    def test_the_artifact_lands_in_failed_not_pending(
        self, client: FlaskClient, workspace: str
    ) -> None:
        """``FAILED`` is deliberate: the operator should see that something
        broke, and it is retryable — so Regenerate is the next step and there
        is no second retry path to keep in step with the first."""
        project_id, _ = self._stuck(client)
        artifact = self._artifact(client, project_id)

        body = client.post(f"/api/v1/artifacts/{artifact['id']}/release").get_json()

        assert body["state"] == "FAILED"
        assert body["capabilities"]["can_regenerate"] is True

    def test_the_job_is_cancelled_rather_than_left_running(
        self, client: FlaskClient, workspace: str
    ) -> None:
        """Releasing the artifact without killing the job would leave the key
        held — half a fix, and the half that looks like it worked."""
        project_id, job_id = self._stuck(client)
        artifact = self._artifact(client, project_id)
        client.post(f"/api/v1/artifacts/{artifact['id']}/release")

        assert client.get(f"/api/v1/jobs/{job_id}").get_json()["status"] == (
            "CANCELLED"
        )

    def test_releasing_a_stage_that_is_not_stuck_is_409(
        self, client: FlaskClient, db_engine: Engine, workspace: str
    ) -> None:
        """Well-formed request, world not in that state — the same distinction
        ``AdmissionError`` already carries. A 404 would say the artifact does
        not exist, which is a different and misleading problem."""
        project_id = _create_project(client)
        sessions = sessionmaker(bind=db_engine, expire_on_commit=False)
        with unit_of_work(sessions) as uow:
            artifact = uow.artifacts.create(project_id, ArtifactKind.SCRIPT, None)
            uow.flush()
            uow.versions.add_version(
                artifact,
                origin=VersionOrigin.GENERATED,
                content_hash="h",
                inline_content={"script": "words"},
            )
            artifact.state = ArtifactState.AWAITING_APPROVAL
            artifact_id = artifact.id

        response = client.post(f"/api/v1/artifacts/{artifact_id}/release")
        assert response.status_code == 409
        assert "not generating" in response.get_json()["detail"]

    def test_releasing_an_unknown_artifact_is_404(
        self, client: FlaskClient, workspace: str
    ) -> None:
        assert client.post("/api/v1/artifacts/01NOPE/release").status_code == 404


class TestDeleteProject:
    """The only endpoint in this API that destroys anything.

    Every other write appends — a rejection is a row, a regeneration is a new
    version, releasing a stuck stage leaves the dead job in place. So the two
    claims worth pinning are what it *does* take (everything hanging off the
    project, via FKs verified against the live schema) and what it deliberately
    leaves: the audit trail and the stored bytes.
    """

    def _project_with_work(
        self, client: FlaskClient, db_engine: Engine
    ) -> tuple[str, str]:
        """A project with an artifact, a version and a job behind it."""
        project_id = _create_project(client)
        client.post(
            f"/api/v1/projects/{project_id}/generations", json={"stage": "research"}
        )
        sessions = sessionmaker(bind=db_engine, expire_on_commit=False)
        with unit_of_work(sessions) as uow:
            artifact = uow.artifacts.find(project_id, ArtifactKind.RESEARCH)
            assert artifact is not None
            version = uow.versions.add_version(
                artifact,
                origin=VersionOrigin.GENERATED,
                content_hash="h",
                inline_content={"summary": "notes"},
            )
            return project_id, version.id

    def test_it_deletes_the_project(
        self, client: FlaskClient, db_engine: Engine, workspace: str
    ) -> None:
        project_id, _ = self._project_with_work(client, db_engine)

        response = client.delete(f"/api/v1/projects/{project_id}")
        assert response.status_code == 204
        assert response.get_data() == b""

        assert client.get(f"/api/v1/projects/{project_id}").status_code == 404

    def test_it_takes_the_artifacts_and_versions_with_it(
        self, client: FlaskClient, db_engine: Engine, workspace: str
    ) -> None:
        """The cascade, asserted rather than trusted. A project whose artifacts
        survived would leave rows nothing can reach — and a per-scene image
        artifact pointing at a deleted scene is unanswerable."""
        project_id, version_id = self._project_with_work(client, db_engine)
        client.delete(f"/api/v1/projects/{project_id}")

        sessions = sessionmaker(bind=db_engine, expire_on_commit=False)
        with unit_of_work(sessions) as uow:
            assert uow.artifacts.for_project(project_id) == []
            assert uow.versions.get(version_id) is None
            remaining = uow.session.execute(
                sa.text("SELECT count(*) FROM generation_job WHERE project_id = :p"),
                {"p": project_id},
            ).scalar()
        assert remaining == 0

    def test_the_audit_trail_survives(
        self, client: FlaskClient, db_engine: Engine, workspace: str
    ) -> None:
        """**Deliberate, and the enum says so.** ``SubjectType``'s own
        docstring: the subject is polymorphic with no foreign key "because the
        audit log must outlive its subjects — a hard-deleted project should not
        take its history with it". A deletion is exactly when "what happened to
        that video?" gets asked."""
        project_id, _ = self._project_with_work(client, db_engine)
        client.delete(f"/api/v1/projects/{project_id}")

        sessions = sessionmaker(bind=db_engine, expire_on_commit=False)
        with unit_of_work(sessions) as uow:
            events = list(
                uow.session.execute(
                    sa.text("SELECT event_type FROM audit_event WHERE subject_id = :p"),
                    {"p": project_id},
                ).scalars()
            )
        assert "project.deleted" in events, "no tombstone was written"

    def test_the_tombstone_records_what_was_lost(
        self, client: FlaskClient, db_engine: Engine, workspace: str
    ) -> None:
        """Written *before* the rows go — afterwards there is no topic left to
        record, which is the whole reason the order matters."""
        project_id, _ = self._project_with_work(client, db_engine)
        client.delete(f"/api/v1/projects/{project_id}")

        sessions = sessionmaker(bind=db_engine, expire_on_commit=False)
        with unit_of_work(sessions) as uow:
            payload: dict[str, Any] | None = uow.session.execute(
                sa.text(
                    "SELECT payload FROM audit_event"
                    " WHERE subject_id = :p AND event_type = 'project.deleted'"
                ),
                {"p": project_id},
            ).scalar()
        assert payload is not None, "no tombstone was written"
        assert payload["topic"] == "photosynthesis"

    def test_it_disappears_from_the_list(
        self, client: FlaskClient, db_engine: Engine, workspace: str
    ) -> None:
        project_id, _ = self._project_with_work(client, db_engine)
        client.delete(f"/api/v1/projects/{project_id}")

        items = client.get("/api/v1/projects").get_json()["items"]
        assert project_id not in {item["id"] for item in items}

    def test_deleting_an_unknown_project_is_404(
        self, client: FlaskClient, workspace: str
    ) -> None:
        """Not 204. A delete that reports success for something that was never
        there hides a client addressing the wrong id."""
        assert client.delete("/api/v1/projects/01NOPE").status_code == 404
