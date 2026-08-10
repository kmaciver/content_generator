"""M3-04b: series-scoped jobs and reference-sheet generation.

Two properties that only exist in the database, and one that only exists in the
worker:

* ``ck_generation_job_scope`` — a job has exactly one scope, never both or
  neither. Making ``project_id`` nullable buys a real case and would otherwise
  also buy two nonsense ones.
* ``provider_usage`` rows land against a series-scoped job, which is the whole
  reason branding generation gets a job row at all.
* Nothing is auto-approved. The run makes a character *reviewable*; choosing a
  group is a separate human act.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from videoforge.services.dispatch import RecordingDispatcher
from videoforge.services.jobs import JobService
from videoforge_persistence.models import GenerationJob, Workspace
from videoforge_persistence.uow import unit_of_work
from videoforge_shared.enums import BrandingStatus
from videoforge_shared.ids import new_ulid
from videoforge_shared.tasks import REFERENCES_GENERATE

pytestmark = pytest.mark.integration


@pytest.fixture()
def sessions(db_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=db_engine, autoflush=False, expire_on_commit=False)


@pytest.fixture(autouse=True)
def _fake_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    """An in-memory object store.

    The harness stands up a real PostgreSQL because the database is where the
    interesting properties live — constraints that must *raise*, and a view.
    MinIO has no equivalent: the property under test here is "four rows with
    the right provenance", not "boto3 can PUT", so a real bucket would add a
    container's startup to every run and catch nothing.
    """
    import videoforge_workers.references as references
    from videoforge_shared.hashing import sha256_bytes
    from videoforge_shared.storage import StoredObject

    class _InMemory:
        def __init__(self) -> None:
            self.objects: dict[str, bytes] = {}

        def put_bytes(self, bucket: str, data: bytes, filename: str) -> StoredObject:
            digest = sha256_bytes(data)
            key = f"{digest[:2]}/{digest}/{filename}"
            # Deduplication is real behaviour, not a detail: four candidates of
            # a deterministic mock can legitimately collide, and the caller
            # must still get a usable receipt.
            deduplicated = key in self.objects
            self.objects[key] = data
            return StoredObject(
                bucket=bucket,
                key=key,
                sha256=digest,
                size=len(data),
                deduplicated=deduplicated,
            )

    monkeypatch.setattr(references, "storage", _InMemory)


@pytest.fixture()
def branded(sessions: sessionmaker[Session]) -> dict[str, str]:
    """A series with a character and an approved style, ready to generate."""
    with unit_of_work(sessions) as uow:
        workspace = Workspace(id=new_ulid(), name="references-test")
        uow.session.add(workspace)
        uow.flush()
        series = uow.series.create(workspace_id=workspace.id, title="Explainers")
        uow.flush()
        character = uow.branding.add_character_version(
            series.id,
            name="Pip",
            immutable_traits={"head": "a smooth cream #F4EDE4 circle, no hair"},
        )
        style = uow.branding.add_style_version(
            series.id, name="Flat", fields={"medium": "flat vector"}
        )
        uow.flush()
        uow.branding.approve_style(style.id)
        return {
            "series": str(series.id),
            "character": str(character.id),
            "style": str(style.id),
        }


class TestJobScope:
    def test_a_series_job_needs_no_project(
        self, sessions: sessionmaker[Session], branded: dict[str, str]
    ) -> None:
        with unit_of_work(sessions) as uow:
            reserved = JobService(uow, RecordingDispatcher()).request_series_job(
                series_id=branded["series"],
                spec=REFERENCES_GENERATE,
                idempotency_key_suffix=f"character:{branded['character']}",
                input_snapshot={"character_id": branded["character"]},
            )
            assert reserved.created is True
            assert reserved.job.project_id is None
            assert reserved.job.series_id == branded["series"]

    def test_asking_twice_makes_one_job(
        self, sessions: sessionmaker[Session], branded: dict[str, str]
    ) -> None:
        """Keyed on the character version: a double-click must not cost another
        four images."""
        ids = []
        for _ in range(2):
            with unit_of_work(sessions) as uow:
                reserved = JobService(uow, RecordingDispatcher()).request_series_job(
                    series_id=branded["series"],
                    spec=REFERENCES_GENERATE,
                    idempotency_key_suffix=f"character:{branded['character']}",
                    input_snapshot={"character_id": branded["character"]},
                )
                ids.append((reserved.job.id, reserved.created))
        assert ids[0][1] is True
        assert ids[1][1] is False
        assert ids[0][0] == ids[1][0]

    def test_a_job_with_neither_scope_is_rejected(
        self, sessions: sessionmaker[Session]
    ) -> None:
        """**The reason this file needs Postgres.**

        Making ``project_id`` nullable buys one real case and would otherwise
        buy a nonsense one. The CHECK is what stops a reader ever having to
        defend against it.
        """
        # ``pytest.raises`` **outside** the unit of work: caught inside, the
        # transaction stays poisoned and the context manager then tries to
        # commit it, failing for a second, unrelated reason.
        with pytest.raises(IntegrityError), unit_of_work(sessions) as uow:
            uow.session.add(
                GenerationJob(
                    id=new_ulid(),
                    project_id=None,
                    series_id=None,
                    task_name="x",
                    queue="llm",
                    idempotency_key=new_ulid(),
                )
            )
            uow.flush()

    def test_a_job_with_both_scopes_is_rejected(
        self, sessions: sessionmaker[Session], branded: dict[str, str]
    ) -> None:
        with unit_of_work(sessions) as uow:
            workspace = uow.workspaces.sole()
            assert workspace is not None
            project_id = uow.projects.create(
                workspace_id=workspace.id, series_id=branded["series"], topic="t"
            ).id

        with pytest.raises(IntegrityError), unit_of_work(sessions) as uow:
            uow.session.add(
                GenerationJob(
                    id=new_ulid(),
                    project_id=project_id,
                    series_id=branded["series"],
                    task_name="x",
                    queue="llm",
                    idempotency_key=new_ulid(),
                )
            )
            uow.flush()

    def test_the_repository_rejects_a_bad_scope_before_the_database_does(
        self, sessions: sessionmaker[Session]
    ) -> None:
        """An IntegrityError at flush names the constraint, not the call site
        that got it wrong. This is a programming error, so it should say so."""
        with (
            unit_of_work(sessions) as uow,
            pytest.raises(ValueError, match="exactly one"),
        ):
            uow.jobs.reserve(task_name="x", queue="llm", idempotency_key=new_ulid())


class TestReferenceGeneration:
    def _run(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        branded: dict[str, str],
    ) -> str:
        import videoforge_workers.db as worker_db
        from videoforge_workers.references import references_body
        from videoforge_workers.skeleton import run_job

        group_id = new_ulid()
        with unit_of_work(sessions) as uow:
            job_id = (
                JobService(uow, RecordingDispatcher())
                .request_series_job(
                    series_id=branded["series"],
                    spec=REFERENCES_GENERATE,
                    idempotency_key_suffix=f"character:{branded['character']}",
                    input_snapshot={
                        "character_id": branded["character"],
                        "group_id": group_id,
                    },
                )
                .job.id
            )
        monkeypatch.setattr(worker_db, "get_session_factory", lambda: sessions)
        assert run_job(job_id, references_body, task_name=REFERENCES_GENERATE.name)
        return group_id

    def test_writes_a_full_candidate_group(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        branded: dict[str, str],
    ) -> None:
        from videoforge_workers.references import REFERENCE_POSES

        group_id = self._run(monkeypatch, sessions, branded)
        with unit_of_work(sessions) as uow:
            references = uow.branding.references(group_id)
        assert len(references) == len(REFERENCE_POSES)
        assert [r.index for r in references] == list(range(1, len(REFERENCE_POSES) + 1))

    def test_the_group_spans_the_character(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        branded: dict[str, str],
    ) -> None:
        """Four near-identical front views would tell a reviewer nothing about
        whether the convention survives being turned."""
        group_id = self._run(monkeypatch, sessions, branded)
        with unit_of_work(sessions) as uow:
            angles = {r.angle for r in uow.branding.references(group_id)}
        assert len(angles) == 4

    def test_nothing_is_auto_approved(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        branded: dict[str, str],
    ) -> None:
        """A run that promoted its own output would make the review gate
        decorative for the one asset every episode depends on."""
        self._run(monkeypatch, sessions, branded)
        with unit_of_work(sessions) as uow:
            character = uow.branding.character(branded["character"])
            assert character is not None
            assert character.status is BrandingStatus.AWAITING_APPROVAL
            assert character.approved_reference_group_id is None
            assert uow.branding.approved_character(branded["series"]) is None

    def test_spend_is_metered_against_the_series_job(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        branded: dict[str, str],
    ) -> None:
        """**The reason branding generation gets a job row at all.**

        ``provider_usage.job_id`` is NOT NULL, so without one the most
        expensive operation in the system would be invisible to the S10 cap.
        """
        from videoforge_workers.references import REFERENCE_POSES

        self._run(monkeypatch, sessions, branded)
        with unit_of_work(sessions) as uow:
            rows = uow.session.execute(
                sa.text(
                    "SELECT count(*) FROM provider_usage u "
                    "JOIN generation_job j ON j.id = u.job_id "
                    "WHERE j.series_id = :series"
                ),
                {"series": branded["series"]},
            ).scalar_one()
        assert rows == len(REFERENCE_POSES)

    def test_each_sheet_records_how_it_was_made(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        branded: dict[str, str],
    ) -> None:
        """§10.3 rule 4 — enough to explain a sheet without re-deriving it."""
        group_id = self._run(monkeypatch, sessions, branded)
        with unit_of_work(sessions) as uow:
            snapshot = uow.branding.references(group_id)[0].generation_snapshot
        assert snapshot["prompt"]
        assert snapshot["prompt_digest"]
        assert snapshot["character_version_id"] == branded["character"]
        assert snapshot["style_version_id"] == branded["style"]

    def test_the_sheet_background_overrides_the_series_style(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        branded: dict[str, str],
    ) -> None:
        """A sheet is the character's visual definition, so nothing else belongs
        in the frame.

        Measured on 2026-08-08: a style whose ``background`` permitted "at most
        one simple suggested element" — correct for scenes — put a tuft of grass
        beside the character in the back view. The style is not wrong; it is
        answering a question about scenes, and a sheet must not inherit it.
        """
        from videoforge_workers.references import _REFERENCE_BACKGROUND

        with unit_of_work(sessions) as uow:
            style = uow.branding.style(branded["style"])
            assert style is not None
            style.fields = {
                "medium": "flat vector",
                "background": "a meadow with trees and a distant farmhouse",
            }

        group_id = self._run(monkeypatch, sessions, branded)
        with unit_of_work(sessions) as uow:
            snapshots = [
                r.generation_snapshot for r in uow.branding.references(group_id)
            ]

        assert snapshots
        for snapshot in snapshots:
            assert _REFERENCE_BACKGROUND in snapshot["prompt"]
            assert "meadow" not in snapshot["prompt"]
            # The style's other axes still apply — this overrides one field,
            # it does not discard the series' look.
            assert "flat vector" in snapshot["prompt"]
            assert "scenery" in snapshot["negative_prompt"]

    def test_the_series_style_is_not_rewritten_by_generating(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        branded: dict[str, str],
    ) -> None:
        """``style.fields`` is the persisted jsonb of an *approved* version.

        Overriding the background by mutating it in place would rewrite an
        immutable record as a side effect of drawing a picture — and every
        future scene would silently inherit the sheet's empty background.
        """
        self._run(monkeypatch, sessions, branded)
        with unit_of_work(sessions) as uow:
            style = uow.branding.style(branded["style"])
            assert style is not None
            assert "background" not in style.fields

    def test_a_series_without_an_approved_style_fails_loudly(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        branded: dict[str, str],
    ) -> None:
        """Sheets drawn without the series style would not match the scenes
        they anchor — worse than no sheets at all."""
        with unit_of_work(sessions) as uow:
            style = uow.branding.style(branded["style"])
            assert style is not None
            style.status = BrandingStatus.SUPERSEDED

        with pytest.raises(RuntimeError, match="approved style"):
            self._run(monkeypatch, sessions, branded)
