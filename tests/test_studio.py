"""Image Studio: free-form image generation outside any project or series.

What is worth pinning here:

* **The prompt reaches the provider verbatim.** The studio exists precisely so
  that none of the series' character, style or prompt templates are applied.
  A helpful "improvement" that routed it through ``build_image_prompt`` would
  quietly paint every picture in the house style.
* **A studio job is a third scope.** ``ck_generation_job_scope`` becomes
  exactly-one-of-three; neither, and two at once, are both still refused.
* **Spend is metered.** A studio job writes ``provider_usage`` rows like any
  other, so it lands inside the daily cap rather than beside it.
* **Validation is server-side.** Model, aspect ratio, count and references are
  allow-listed by the API, not trusted from the form.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pytest
import sqlalchemy as sa
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from videoforge.app import create_app
from videoforge.config import AppSettings
from videoforge.services.dispatch import RecordingDispatcher
from videoforge_persistence.models import GenerationJob, Workspace
from videoforge_persistence.uow import unit_of_work
from videoforge_shared.hashing import content_key, sha256_bytes
from videoforge_shared.ids import new_ulid
from videoforge_shared.settings import load_app_settings
from videoforge_shared.storage import StoredObject
from videoforge_shared.studio import (
    STUDIO_ASPECT_RATIOS,
    STUDIO_MAX_COUNT,
    STUDIO_MAX_REFERENCES,
    STUDIO_MODELS,
)
from videoforge_shared.tasks import STUDIO_GENERATE

pytestmark = pytest.mark.integration

#: Smallest valid PNG: 1×1, enough for the header parse and the mime check.
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA"
    "60e6kgAAAABJRU5ErkJggg=="
)


class _InMemoryStorage:
    """Just enough of ``StorageClient`` for the studio's three calls."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}

    def put_bytes(self, bucket: str, data: bytes, filename: str) -> StoredObject:
        digest = sha256_bytes(data)
        key = content_key(digest, filename)
        deduplicated = (bucket, key) in self.objects
        self.objects[(bucket, key)] = data
        return StoredObject(bucket, key, digest, len(data), deduplicated)

    def exists(self, bucket: str, key: str) -> bool:
        return (bucket, key) in self.objects

    def get_bytes(self, bucket: str, key: str) -> bytes:
        return self.objects[(bucket, key)]


@pytest.fixture()
def sessions(db_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=db_engine, autoflush=False, expire_on_commit=False)


@pytest.fixture()
def store() -> _InMemoryStorage:
    return _InMemoryStorage()


@pytest.fixture()
def dispatcher() -> RecordingDispatcher:
    return RecordingDispatcher()


@pytest.fixture()
def app(
    db_engine: Engine, dispatcher: RecordingDispatcher, store: _InMemoryStorage
) -> Iterator[Flask]:
    settings: AppSettings = load_app_settings()
    application = create_app(settings, dispatcher=dispatcher, engine=db_engine)
    application.config.update(TESTING=True, VIDEOFORGE_STORAGE=store)
    yield application


@pytest.fixture()
def client(app: Flask) -> FlaskClient:
    return app.test_client()


@pytest.fixture()
def worker_store(
    monkeypatch: pytest.MonkeyPatch, store: _InMemoryStorage
) -> _InMemoryStorage:
    """The worker reads references from, and writes images to, the same store
    the API uploaded into — so one fake serves both halves."""
    import videoforge_workers.studio as studio

    monkeypatch.setattr(studio, "storage", lambda: store)
    return store


def _valid_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "prompt": "a red bicycle leaning on a blue door, watercolour",
        "model": next(iter(STUDIO_MODELS)),
        "aspect_ratio": "1:1",
        "count": 2,
        "references": [],
    }
    body.update(overrides)
    return body


def _upload(client: FlaskClient, data: bytes = _PNG, mime: str = "image/png") -> str:
    response = client.post(
        "/api/v1/studio/references",
        json={"mime_type": mime, "data_base64": base64.b64encode(data).decode()},
    )
    assert response.status_code == 201, response.get_json()
    return str(response.get_json()["storage_key"])


# --------------------------------------------------------------------------- #
# Options
# --------------------------------------------------------------------------- #


class TestOptions:
    def test_lists_models_ratios_and_limits(self, client: FlaskClient) -> None:
        response = client.get("/api/v1/studio/options")
        assert response.status_code == 200
        body = response.get_json()
        assert [m["id"] for m in body["models"]] == list(STUDIO_MODELS)
        assert body["aspect_ratios"] == list(STUDIO_ASPECT_RATIOS)
        assert body["max_count"] == STUDIO_MAX_COUNT
        assert body["max_references"] == STUDIO_MAX_REFERENCES

    def test_quoted_prices_match_the_pricing_table(self) -> None:
        """The studio quotes a per-image price; it must be the same number the
        spend meter charges, or the page and the cap disagree."""
        from videoforge_providers.pricing import estimate_image_cost

        for model_id, model in STUDIO_MODELS.items():
            quoted = Decimal(model.price_per_image)
            assert quoted == Decimal(str(estimate_image_cost(model_id, 1)))


# --------------------------------------------------------------------------- #
# Reference uploads
# --------------------------------------------------------------------------- #


class TestReferenceUpload:
    def test_stores_the_image_and_returns_its_key(
        self, client: FlaskClient, store: _InMemoryStorage
    ) -> None:
        response = client.post(
            "/api/v1/studio/references",
            json={
                "mime_type": "image/png",
                "data_base64": base64.b64encode(_PNG).decode(),
            },
        )
        assert response.status_code == 201
        body = response.get_json()
        assert body["storage_key"].endswith(".png")
        assert body["asset_url"].endswith(body["storage_key"])
        assert _PNG in store.objects.values()

    def test_refuses_a_non_image_type(self, client: FlaskClient) -> None:
        response = client.post(
            "/api/v1/studio/references",
            json={"mime_type": "text/html", "data_base64": "PGgxPg=="},
        )
        assert response.status_code == 400

    def test_refuses_bytes_that_are_not_the_declared_image(
        self, client: FlaskClient
    ) -> None:
        """The mime type is a claim; the magic bytes are the evidence."""
        response = client.post(
            "/api/v1/studio/references",
            json={
                "mime_type": "image/png",
                "data_base64": base64.b64encode(b"not a png at all").decode(),
            },
        )
        assert response.status_code == 400

    def test_refuses_malformed_base64(self, client: FlaskClient) -> None:
        response = client.post(
            "/api/v1/studio/references",
            json={"mime_type": "image/png", "data_base64": "!!!not base64!!!"},
        )
        assert response.status_code == 400

    def test_refuses_an_oversized_image(self, client: FlaskClient) -> None:
        from videoforge_shared.studio import STUDIO_MAX_REFERENCE_BYTES

        big = _PNG + b"\x00" * STUDIO_MAX_REFERENCE_BYTES
        response = client.post(
            "/api/v1/studio/references",
            json={
                "mime_type": "image/png",
                "data_base64": base64.b64encode(big).decode(),
            },
        )
        assert response.status_code == 413


# --------------------------------------------------------------------------- #
# Generation requests
# --------------------------------------------------------------------------- #


class TestCreateGeneration:
    def test_returns_202_with_a_job_and_dispatches_it(
        self, client: FlaskClient, dispatcher: RecordingDispatcher
    ) -> None:
        response = client.post("/api/v1/studio/generations", json=_valid_body())
        assert response.status_code == 202, response.get_json()
        body = response.get_json()
        assert body["job_id"]
        assert body["generation"]["status"] == "QUEUED"
        assert body["generation"]["images"] == []
        assert [spec.name for spec, _ in dispatcher.sent] == [STUDIO_GENERATE.name]

    def test_the_job_is_studio_scoped(
        self, client: FlaskClient, sessions: sessionmaker[Session]
    ) -> None:
        body = client.post("/api/v1/studio/generations", json=_valid_body()).get_json()
        with unit_of_work(sessions) as uow:
            job = uow.jobs.get(body["job_id"])
            assert job is not None
            assert job.project_id is None
            assert job.series_id is None
            assert job.studio_generation_id == body["generation"]["id"]
            assert job.queue == "image"

    @pytest.mark.parametrize(
        "overrides",
        [
            {"prompt": ""},
            {"prompt": "   "},
            {"model": "imagen-4.0"},
            {"model": "not-a-model"},
            {"aspect_ratio": "5:4"},
            {"count": 0},
            {"count": STUDIO_MAX_COUNT + 1},
            {"surprise": True},
        ],
        ids=lambda o: next(iter(o)),
    )
    def test_refuses_invalid_requests(
        self, client: FlaskClient, overrides: dict[str, Any]
    ) -> None:
        response = client.post(
            "/api/v1/studio/generations", json=_valid_body(**overrides)
        )
        assert response.status_code == 400, response.get_json()

    def test_refuses_too_many_references(self, client: FlaskClient) -> None:
        keys = [_upload(client) for _ in range(1)] * (STUDIO_MAX_REFERENCES + 1)
        response = client.post(
            "/api/v1/studio/generations", json=_valid_body(references=keys)
        )
        assert response.status_code == 400

    def test_refuses_a_reference_that_was_never_uploaded(
        self, client: FlaskClient
    ) -> None:
        missing = content_key("ab" * 32, "ghost.png")
        response = client.post(
            "/api/v1/studio/generations", json=_valid_body(references=[missing])
        )
        assert response.status_code == 400

    def test_refuses_a_path_traversal_key(self, client: FlaskClient) -> None:
        response = client.post(
            "/api/v1/studio/generations",
            json=_valid_body(references=["../../etc/passwd"]),
        )
        assert response.status_code == 400


class TestListGenerations:
    def test_newest_first(self, client: FlaskClient) -> None:
        first = client.post(
            "/api/v1/studio/generations", json=_valid_body(prompt="first")
        ).get_json()
        second = client.post(
            "/api/v1/studio/generations", json=_valid_body(prompt="second")
        ).get_json()

        body = client.get("/api/v1/studio/generations").get_json()
        ids = [g["id"] for g in body["items"]]
        assert ids[:2] == [second["generation"]["id"], first["generation"]["id"]]

    def test_pages_with_a_cursor(self, client: FlaskClient) -> None:
        for i in range(3):
            client.post("/api/v1/studio/generations", json=_valid_body(prompt=f"p{i}"))

        page1 = client.get("/api/v1/studio/generations?limit=2").get_json()
        assert len(page1["items"]) == 2
        assert page1["next_cursor"]
        page2 = client.get(
            f"/api/v1/studio/generations?limit=2&before={page1['next_cursor']}"
        ).get_json()
        assert {g["id"] for g in page2["items"]}.isdisjoint(
            {g["id"] for g in page1["items"]}
        )

    def test_unknown_generation_is_404(self, client: FlaskClient) -> None:
        assert client.get("/api/v1/studio/generations/01NOPE").status_code == 404


# --------------------------------------------------------------------------- #
# The worker
# --------------------------------------------------------------------------- #


def _run(
    monkeypatch: pytest.MonkeyPatch, sessions: sessionmaker[Session], job_id: str
) -> bool:
    import videoforge_workers.db as worker_db
    from videoforge_workers.skeleton import run_job
    from videoforge_workers.studio import studio_body

    monkeypatch.setattr(worker_db, "get_session_factory", lambda: sessions)
    return run_job(job_id, studio_body, task_name=STUDIO_GENERATE.name)


class _CapturingProvider:
    """Records each request; answers with a fixed PNG."""

    name = "capture"

    def __init__(self) -> None:
        self.requests: list[Any] = []

    def capabilities(self) -> Any:  # pragma: no cover - not consulted
        raise NotImplementedError

    def generate(self, req: Any) -> Any:
        from videoforge_providers.models import GeneratedImage, ImageResult, Usage

        self.requests.append(req)
        return ImageResult(
            images=(
                GeneratedImage(
                    data=_PNG + str(len(self.requests)).encode(),
                    mime_type="image/png",
                    width=1,
                    height=1,
                    seed=None,
                ),
            ),
            usage=Usage(images=1, unit_cost_estimate=0.04),
            provider_meta={"provider": "capture", "model": req.model_hint},
            latency_ms=5,
        )


@pytest.fixture()
def provider(monkeypatch: pytest.MonkeyPatch) -> _CapturingProvider:
    import videoforge_workers.studio as studio

    captured = _CapturingProvider()
    monkeypatch.setattr(studio, "_provider", lambda: captured)
    return captured


class TestWorker:
    def test_writes_one_image_per_count_and_succeeds(
        self,
        monkeypatch: pytest.MonkeyPatch,
        client: FlaskClient,
        sessions: sessionmaker[Session],
        worker_store: _InMemoryStorage,
        provider: _CapturingProvider,
    ) -> None:
        created = client.post(
            "/api/v1/studio/generations", json=_valid_body(count=3)
        ).get_json()
        assert _run(monkeypatch, sessions, created["job_id"])

        body = client.get(
            f"/api/v1/studio/generations/{created['generation']['id']}"
        ).get_json()
        assert body["status"] == "SUCCEEDED"
        assert [i["index"] for i in body["images"]] == [1, 2, 3]
        assert all(i["asset_url"].startswith("/assets/") for i in body["images"])

    def test_the_prompt_is_sent_verbatim(
        self,
        monkeypatch: pytest.MonkeyPatch,
        client: FlaskClient,
        sessions: sessionmaker[Session],
        worker_store: _InMemoryStorage,
        provider: _CapturingProvider,
    ) -> None:
        """No character, no style, no template — the reason the studio exists."""
        prompt = "  a lighthouse at dusk, 35mm film grain  "
        model = list(STUDIO_MODELS)[-1]
        created = client.post(
            "/api/v1/studio/generations",
            json=_valid_body(prompt=prompt, count=1, model=model, aspect_ratio="16:9"),
        ).get_json()
        assert _run(monkeypatch, sessions, created["job_id"])

        (request,) = provider.requests
        assert request.prompt == prompt.strip()
        assert request.negative_prompt == ""
        assert request.model_hint == model
        assert request.aspect_ratio == "16:9"

    def test_references_are_passed_as_bytes(
        self,
        monkeypatch: pytest.MonkeyPatch,
        client: FlaskClient,
        sessions: sessionmaker[Session],
        worker_store: _InMemoryStorage,
        provider: _CapturingProvider,
    ) -> None:
        key = _upload(client)
        created = client.post(
            "/api/v1/studio/generations",
            json=_valid_body(count=1, references=[key]),
        ).get_json()
        assert _run(monkeypatch, sessions, created["job_id"])

        (request,) = provider.requests
        assert [r.data for r in request.references] == [_PNG]
        assert [r.mime_type for r in request.references] == ["image/png"]

    def test_usage_is_metered_against_the_job(
        self,
        monkeypatch: pytest.MonkeyPatch,
        client: FlaskClient,
        sessions: sessionmaker[Session],
        worker_store: _InMemoryStorage,
        provider: _CapturingProvider,
    ) -> None:
        created = client.post(
            "/api/v1/studio/generations", json=_valid_body(count=2)
        ).get_json()
        assert _run(monkeypatch, sessions, created["job_id"])

        with unit_of_work(sessions) as uow:
            rows = uow.session.execute(
                sa.text("SELECT count(*) FROM provider_usage WHERE job_id = :j"),
                {"j": created["job_id"]},
            ).scalar_one()
        assert rows == 2

    def test_a_provider_failure_marks_the_generation_failed(
        self,
        monkeypatch: pytest.MonkeyPatch,
        client: FlaskClient,
        sessions: sessionmaker[Session],
        worker_store: _InMemoryStorage,
    ) -> None:
        import videoforge_workers.studio as studio
        from videoforge_providers.models import ProviderError

        class _Blocked(_CapturingProvider):
            def generate(self, req: Any) -> Any:
                raise ProviderError("blocked by a safety filter", provider="x")

        monkeypatch.setattr(studio, "_provider", _Blocked)
        created = client.post(
            "/api/v1/studio/generations", json=_valid_body(count=1)
        ).get_json()
        # run_job records the failure and then re-raises for Celery's retry.
        with pytest.raises(ProviderError):
            _run(monkeypatch, sessions, created["job_id"])

        body = client.get(
            f"/api/v1/studio/generations/{created['generation']['id']}"
        ).get_json()
        # QUEUED when the skeleton requeues a retryable failure.
        assert body["status"] in {"FAILED", "QUEUED"}
        if body["status"] == "FAILED":
            assert "safety filter" in body["error"]
        assert body["images"] == []


# --------------------------------------------------------------------------- #
# The scope constraint
# --------------------------------------------------------------------------- #


class TestScope:
    def test_a_job_with_two_scopes_is_rejected(
        self, client: FlaskClient, sessions: sessionmaker[Session]
    ) -> None:
        generation_id = client.post(
            "/api/v1/studio/generations", json=_valid_body()
        ).get_json()["generation"]["id"]
        with unit_of_work(sessions) as uow:
            workspace = Workspace(id=new_ulid(), name="studio-scope-test")
            uow.session.add(workspace)
            uow.flush()
            series_id = uow.series.create(workspace_id=workspace.id, title="S").id

        with pytest.raises(IntegrityError), unit_of_work(sessions) as uow:
            uow.session.add(
                GenerationJob(
                    id=new_ulid(),
                    series_id=series_id,
                    studio_generation_id=generation_id,
                    task_name="x",
                    queue="image",
                    idempotency_key=new_ulid(),
                )
            )
            uow.flush()

    def test_the_repository_names_all_three_scopes(
        self, sessions: sessionmaker[Session]
    ) -> None:
        with (
            unit_of_work(sessions) as uow,
            pytest.raises(ValueError, match="exactly one"),
        ):
            uow.jobs.reserve(task_name="x", queue="image", idempotency_key=new_ulid())
