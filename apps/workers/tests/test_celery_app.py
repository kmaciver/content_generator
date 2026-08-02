"""Celery skeleton tests (M0-08).

Configuration and registration only — no broker. Round-trips through real
Redis are the live verification's job (and testcontainers', from M1 per SADD
§22); a unit test that quietly required a broker would be a flaky lie.
"""

from __future__ import annotations

from videoforge_shared.correlation import correlation_context
from videoforge_workers.celery_app import QUEUES, app
from videoforge_workers.ping import PING_TASKS


class TestDeliverySemantics:
    """Each of these guards a specific failure mode; a regression here is an
    at-least-once-delivery bug waiting for production traffic."""

    def test_acks_late_with_reject_on_worker_lost(self) -> None:
        assert app.conf.task_acks_late is True
        assert app.conf.task_reject_on_worker_lost is True

    def test_no_prefetch_hoarding(self) -> None:
        assert app.conf.worker_prefetch_multiplier == 1

    def test_visibility_timeout_exceeds_hard_time_limit(self) -> None:
        visibility = app.conf.broker_transport_options["visibility_timeout"]
        assert visibility >= app.conf.task_time_limit, (
            "a task may still be running when redis redelivers it — "
            "that is the duplicate-artifact bug R5 describes"
        )

    def test_soft_limit_precedes_hard_limit(self) -> None:
        assert app.conf.task_soft_time_limit < app.conf.task_time_limit

    def test_flower_visibility_events_enabled(self) -> None:
        assert app.conf.worker_send_task_events is True
        assert app.conf.task_send_sent_event is True


class TestRegistration:
    def test_every_queue_has_a_ping_task(self) -> None:
        assert set(PING_TASKS) == set(QUEUES)
        for queue in QUEUES:
            assert f"ping.{queue}" in app.tasks

    def test_routes_cover_every_ping(self) -> None:
        routes = app.conf.task_routes
        for queue in QUEUES:
            assert routes[f"ping.{queue}"] == {"queue": queue}

    def test_beat_schedule_references_a_registered_task(self) -> None:
        """Every beat entry must name a task some module actually registers.

        The import loop reproduces what a worker does at boot: Celery imports
        ``app.conf.imports`` and *that* is what populates the registry. Doing
        it from the config rather than from a hand-written list means the
        check still holds for a task added later — a beat entry pointing at a
        module missing from ``imports`` fails here instead of erroring on
        every tick, forever, in a container nobody is watching.
        """
        import importlib

        for module in app.conf.imports:
            importlib.import_module(module)

        for name, entry in app.conf.beat_schedule.items():
            assert entry["task"] in app.tasks, (
                f"beat entry {name!r} schedules {entry['task']!r}, which none "
                f"of {app.conf.imports} registers"
            )


class TestSkeletonStub:
    def test_ping_executes_inline_and_reports_its_queue(self) -> None:
        # .apply() runs the task body in-process — no broker involved.
        result = PING_TASKS["llm"].apply()
        payload = result.get()
        assert payload["queue"] == "llm"
        assert payload["pid"] > 0

    def test_task_context_does_not_leak_outward(self) -> None:
        """The skeleton binds correlation inside the task; after an inline run
        the ambient context must be exactly what it was before."""
        with correlation_context("cid-outside"):
            PING_TASKS["events"].apply()
            from videoforge_shared.correlation import get_correlation_id

            assert get_correlation_id() == "cid-outside"
