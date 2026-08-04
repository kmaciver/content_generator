"""The Celery application — one app, one broker, queue-per-resource-class.

Every setting from SADD §14.2 lives here with its reason attached, because
each one guards against a specific failure mode and this file is where the
next reader will wonder why.
"""

from __future__ import annotations

from typing import Any

from celery import Celery
from celery.signals import setup_logging

from videoforge_shared.logging import configure_logging
from videoforge_shared.settings import get_app_settings
from videoforge_shared.tasks import DRAIN_OUTBOX, RENDER_HELLO, SCRIPT_GENERATE

#: The queue set (SADD §14.1 + D4's `render`). Slow media work must never
#: starve cheap LLM calls, so these are separate queues consumed by separate
#: containers with separate concurrency — not one worker subscribed to all.
QUEUES = ("llm", "image", "voice", "timeline", "package", "events", "render")

_settings = get_app_settings()

app = Celery("videoforge")

app.conf.update(
    broker_url=_settings.celery.broker_url,
    result_backend=_settings.celery.result_backend,
    # ------------------------------------------------------------------ #
    # Delivery semantics (SADD §14.2/§14.3)
    # ------------------------------------------------------------------ #
    # Ack only after the task ran: a worker killed mid-task means redelivery,
    # not silence. Safe ONLY because the task skeleton is idempotent — the
    # RUNNING-guard (M1) makes the redelivered twin observe SUCCEEDED and exit.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    # One task in flight per worker process: long tasks must not hold a queue
    # of prefetched work hostage behind them.
    worker_prefetch_multiplier=1,
    # Redis redelivers anything unacked past this window. It must exceed the
    # longest legitimate task runtime or running tasks get duplicated mid-run.
    broker_transport_options={"visibility_timeout": 3600},
    # ------------------------------------------------------------------ #
    # Limits and hygiene
    # ------------------------------------------------------------------ #
    # Global ceiling; per-stage annotations tighten this in M1+ (llm 300s,
    # image 600s per SADD §14.2). Soft limit raises inside the task first so
    # it can record FAILED; the hard limit is the backstop kill.
    task_soft_time_limit=540,
    task_time_limit=600,
    # Recycle worker processes: provider SDKs leak, and a bounded lifetime
    # turns a slow leak into a non-event.
    worker_max_tasks_per_child=100,
    # Postgres is the state of record (GenerationJob), not the result backend;
    # results are only for ad-hoc inspection and expire after a day.
    result_expires=86400,
    # ------------------------------------------------------------------ #
    # Observability
    # ------------------------------------------------------------------ #
    # Flower is blind without these two.
    worker_send_task_events=True,
    task_send_sent_event=True,
    # Our structured logging owns the root logger; celery must not hijack it.
    worker_hijack_root_logger=False,
    timezone="UTC",
    enable_utc=True,
    broker_connection_retry_on_startup=True,
    # Task modules, loaded by celery AT WORKER BOOT rather than imported at the
    # bottom of this module. The bottom-import variant created a cycle: any
    # entrypoint starting from a task module (render -> skeleton -> celery_app
    # -> ping -> skeleton, partially initialised) blew up with ImportError.
    # `imports` breaks the cycle because nothing here imports the tasks at all;
    # producers register tasks simply by importing the module they enqueue.
    imports=(
        "videoforge_workers.ping",
        "videoforge_workers.render",
        "videoforge_workers.outbox",
        "videoforge_workers.research",
        "videoforge_workers.script",
        "videoforge_workers.scenes",
        "videoforge_workers.prompts_stage",
    ),
    # ------------------------------------------------------------------ #
    # Beat
    # ------------------------------------------------------------------ #
    beat_schedule={
        "heartbeat": {
            "task": "ping.events",
            "schedule": 30.0,
            "options": {"queue": "events"},
        },
        # 1s, per SADD §14.5. The latency a user feels is this interval plus
        # the poll interval, and the drain is a single indexed query against a
        # partial index sized to the backlog — cheap enough that a slower tick
        # would trade real responsiveness for no measurable saving.
        "drain-outbox": {
            "task": DRAIN_OUTBOX.name,
            "schedule": 1.0,
            "options": {"queue": DRAIN_OUTBOX.queue},
        },
    },
)

#: Explicit routing so a task name always maps to its queue even when a caller
#: forgets `queue=`. Tasks are still enqueued with an explicit queue by the
#: skeleton's helpers; this is the safety net, not the mechanism.
app.conf.task_routes = {
    **{f"ping.{queue}": {"queue": queue} for queue in QUEUES},
    RENDER_HELLO.name: {"queue": RENDER_HELLO.queue},
    DRAIN_OUTBOX.name: {"queue": DRAIN_OUTBOX.queue},
    SCRIPT_GENERATE.name: {"queue": SCRIPT_GENERATE.queue},
}


@setup_logging.connect
def _configure_logging(**_kwargs: Any) -> None:
    """Install our JSON logging in every worker process. Connecting this
    signal is also what stops celery installing its own root handler."""
    configure_logging(
        level=_settings.core.log_level.value,
        fmt=_settings.core.log_format.value,
    )
