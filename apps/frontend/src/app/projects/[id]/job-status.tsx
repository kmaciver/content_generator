"use client";

import type { JobResponse } from "@/lib/api";
import { humanise, jobStatusColor } from "@/lib/state-colors";

/** What the worker is doing, or what it hit (M5-06, F14).
 *
 * The screen used to say "Generating… this page updates itself" and nothing
 * else. That is fine at four seconds and useless at four minutes, and on a
 * failure it was worse than useless: `generation_job` has recorded the queue,
 * the attempt and the provider's own error message since M1-04, and no client
 * had ever asked for any of it — so the first real render failure of this
 * project was diagnosed by reading a container log.
 *
 * Every field here already existed. This ticket is a window, not a mechanism.
 */
export function JobStatus({
  job,
  onRetry,
  canRetry,
  retrying,
}: {
  job: JobResponse | null;
  onRetry: () => void;
  canRetry: boolean;
  retrying: boolean;
}) {
  if (!job) {
    return null;
  }

  const message =
    typeof job.error?.message === "string" ? job.error.message : null;
  const errorType = typeof job.error?.type === "string" ? job.error.type : null;

  return (
    <div
      data-testid="job-status"
      className="flex flex-col gap-2 rounded-md px-3 py-2 text-xs"
      style={{
        background: "var(--color-surface-raised)",
        border: "1px solid var(--color-border-subtle)",
      }}
    >
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <span
          data-testid="job-state"
          className="font-medium"
          style={{ color: jobStatusColor(job.status) }}
        >
          {humanise(job.status)}
        </span>
        {/* The queue, because "which queue is it on" is the first question
            about a job that is not moving — and `queue` is stored on the row
            rather than inferred from the task name for exactly that reason. */}
        <Field label="queue">{job.queue}</Field>
        <Field label="task">{job.task_name}</Field>
        {/* Only when it means something. `attempt` is 1 on every job today —
            re-dispatch is gated off in the worker until the reconciler lands —
            so printing "attempt 1 of 3" on every stage would be three words of
            noise that also implies retries are happening. */}
        {job.attempt > 1 ? (
          <Field label="attempt">
            {job.attempt} of {job.max_attempts}
          </Field>
        ) : null}
        <span style={{ color: "var(--color-ink-muted)" }}>{timing(job)}</span>
      </div>

      {message ? (
        <div className="flex flex-col gap-2">
          <p
            data-testid="job-error"
            className="font-mono leading-relaxed"
            style={{ color: "var(--color-state-failed)" }}
          >
            {errorType ? <strong>{errorType}: </strong> : null}
            {message}
          </p>
          {canRetry ? (
            // The same door Regenerate uses, put where the failure is.
            //
            // Not a second mechanism: there is no `POST /jobs/{id}/retry`, and
            // there deliberately is not one — `JobRepository.requeue` only
            // writes QUEUED, and nothing re-dispatches a QUEUED job, so an
            // endpoint over it would park the row in a *live* status that
            // holds the idempotency key forever and makes Regenerate itself a
            // silent no-op. One way to re-run a job, reachable from the place
            // that tells you it needs re-running.
            <button
              type="button"
              data-testid="retry-stage"
              disabled={retrying}
              onClick={onRetry}
              className="self-start rounded px-3 py-1 disabled:opacity-40"
              style={{
                background: "var(--color-state-generating)",
                color: "var(--color-surface)",
              }}
            >
              {retrying ? "Retrying…" : "Retry"}
            </button>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function Field({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <span style={{ color: "var(--color-ink-muted)" }}>
      {label} <span style={{ color: "var(--color-ink)" }}>{children}</span>
    </span>
  );
}

/** How long, phrased by what the job is doing.
 *
 * A finished job is measured **server clock against server clock** —
 * `finished_at` minus `started_at`, both written by `now()` — so the number is
 * a duration and not a duration plus this browser's skew. A job still running
 * has no second server timestamp to subtract, so that one case does compare
 * `Date.now()` against a server instant; it is a live counter next to a
 * spinner rather than a recorded fact, and it is the only place here that
 * mixes the two.
 */
function timing(job: JobResponse): string {
  if (job.finished_at) {
    const from = job.started_at ?? job.queued_at;
    return `took ${duration(Date.parse(job.finished_at) - Date.parse(from))}`;
  }
  if (job.started_at) {
    return `running for ${duration(Date.now() - Date.parse(job.started_at))}`;
  }
  return `queued for ${duration(Date.now() - Date.parse(job.queued_at))}`;
}

function duration(ms: number): string {
  // Clamped at zero: sub-second clock skew between the browser and the
  // database can otherwise render "running for -0.3s", which reads as a bug in
  // the pipeline rather than in the clock.
  const seconds = Math.max(0, ms) / 1000;
  if (seconds < 60) {
    return `${seconds.toFixed(1)}s`;
  }
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) {
    return `${minutes}m ${Math.floor(seconds % 60)}s`;
  }
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}
