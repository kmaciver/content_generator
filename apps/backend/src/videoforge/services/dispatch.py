"""Publishing work to the broker, by name.

The backend must never import the workers (SADD §8), so it cannot reference
the task functions it enqueues. Celery's ``send_task`` publishes by name, and
``videoforge_shared.tasks`` is where both sides agree on what the names are.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from celery import Celery

from videoforge_shared.correlation import get_correlation_id
from videoforge_shared.settings import CelerySettings
from videoforge_shared.tasks import TaskSpec

logger = logging.getLogger(__name__)

#: Header key for the Celery leg — underscored, matching the worker skeleton.
#: Protocol-2 headers can surface as attribute lookups on the task request,
#: and hyphens do not survive that.
CELERY_CORRELATION_HEADER = "x_request_id"

__all__ = ["CeleryDispatcher", "RecordingDispatcher", "TaskDispatcher"]


class TaskDispatcher(Protocol):
    """How a service asks for work to happen elsewhere.

    A Protocol rather than a concrete class so services take a dependency on
    the *act of dispatching* rather than on Celery. Tests use
    :class:`RecordingDispatcher` and assert on what would have been sent,
    without a broker.
    """

    def send(
        self, spec: TaskSpec, *, correlation_id: str | None = None, **kwargs: Any
    ) -> str | None:
        """Publish ``spec``. Returns the broker's task id where there is one."""
        ...


class CeleryDispatcher:
    """The real one.

    Holds a Celery app configured with the broker and **no task imports** —
    it only ever produces. That is what keeps `send_task` honest: this process
    genuinely cannot run a worker task, so a name that no worker registers
    fails as an unconsumed message rather than being silently executed
    in-process.
    """

    def __init__(self, celery_settings: CelerySettings) -> None:
        self._app = Celery("videoforge-producer")
        self._app.conf.update(
            broker_url=celery_settings.broker_url,
            result_backend=celery_settings.result_backend,
            task_send_sent_event=True,
            broker_connection_retry_on_startup=True,
            timezone="UTC",
            enable_utc=True,
        )

    def send(
        self, spec: TaskSpec, *, correlation_id: str | None = None, **kwargs: Any
    ) -> str | None:
        cid = correlation_id or get_correlation_id()
        headers = {CELERY_CORRELATION_HEADER: cid} if cid else {}
        result = self._app.send_task(
            spec.name, kwargs=kwargs, queue=spec.queue, headers=headers
        )
        logger.info(
            "task dispatched",
            extra={"task": spec.name, "queue": spec.queue, "task_id": result.id},
        )
        return str(result.id)


class RecordingDispatcher:
    """Test double that records instead of publishing.

    Lives beside the real implementation rather than in the test tree because
    the seam is only worth anything if both sides are maintained together —
    a fake that drifts from the Protocol stops catching the bugs it exists for.
    """

    def __init__(self) -> None:
        self.sent: list[tuple[TaskSpec, dict[str, Any]]] = []

    def send(
        self, spec: TaskSpec, *, correlation_id: str | None = None, **kwargs: Any
    ) -> str | None:
        self.sent.append((spec, kwargs))
        return None
