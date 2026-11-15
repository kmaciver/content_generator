"use client";

import Link from "next/link";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, type ProjectSummary, type WaitingOn } from "@/lib/api";
import { humanise } from "@/lib/state-colors";

export function ProjectList() {
  const queryClient = useQueryClient();
  const [topic, setTopic] = useState("");
  const [seriesId, setSeriesId] = useState("");
  // Which row is asking "really?". A single id rather than a per-row flag, so
  // arming one delete disarms any other — two rows both mid-confirmation is a
  // state where the wrong Enter deletes the wrong project.
  const [pendingDelete, setPendingDelete] = useState<string | null>(null);

  const projects = useQuery({
    queryKey: ["projects"],
    queryFn: api.listProjects,
    // The list used to be static: a project could finish generating while
    // someone watched it and the row never changed. Polls only while a worker
    // is actually running, which is the same rule the review screen uses and
    // the only time this page has anything new to say.
    refetchInterval: (query) =>
      query.state.data?.items.some((p) => p.waiting_on === "machine")
        ? 3000
        : false,
  });

  // The series is not decoration. ADR-016 makes an approved character and
  // style a *precondition* an image job consumes, resolved through the
  // project's series — and this form used to send a topic and nothing else,
  // so every project created through the UI was refused at image admission
  // with "this project belongs to no series" and no way to fix it from here.
  // Found by M4-12, which could not drive the pipeline past prompts.
  const series = useQuery({ queryKey: ["series"], queryFn: api.listSeries });

  // Default to the only series when there is one, which is what v1 seeds.
  // Chosen in an effect-free derivation rather than stored state so it cannot
  // go stale against a list that arrives after the first render.
  const options = series.data ?? [];
  const chosen =
    seriesId || (options.length === 1 ? (options[0]?.id ?? "") : "");

  const create = useMutation({
    mutationFn: (value: string) => api.createProject(value, chosen || null),
    onSuccess: () => {
      setTopic("");
      void queryClient.invalidateQueries({ queryKey: ["projects"] });
    },
  });

  const remove = useMutation({
    mutationFn: (id: string) => api.deleteProject(id),
    onSuccess: () => {
      setPendingDelete(null);
      void queryClient.invalidateQueries({ queryKey: ["projects"] });
    },
  });

  return (
    <div className="flex flex-col gap-6">
      <form
        className="flex gap-2"
        onSubmit={(event) => {
          event.preventDefault();
          if (topic.trim()) create.mutate(topic.trim());
        }}
      >
        <input
          value={topic}
          onChange={(event) => setTopic(event.target.value)}
          placeholder="What should the video explain?"
          aria-label="Topic"
          className="flex-1 rounded-md px-3 py-2 text-sm"
          style={{
            background: "var(--color-surface-raised)",
            border: "1px solid var(--color-border-subtle)",
            color: "var(--color-ink)",
          }}
        />
        {/* Shown even when there is only one series, rather than hidden as a
            silent default: which character and style a video is branded with
            is pinned on the first image job and never changes (ADR-016), so a
            reviewer should see the choice being made. */}
        <select
          value={chosen}
          onChange={(event) => setSeriesId(event.target.value)}
          aria-label="Series"
          className="rounded-md px-3 py-2 text-sm"
          style={{
            background: "var(--color-surface-raised)",
            border: "1px solid var(--color-border-subtle)",
            color: "var(--color-ink)",
          }}
        >
          <option value="">No series</option>
          {options.map((entry) => (
            <option key={entry.id} value={entry.id}>
              {entry.title}
            </option>
          ))}
        </select>
        <button
          type="submit"
          // Disabled while in flight as well as while empty: creating a project
          // is not idempotent, so a double-click would make two.
          disabled={!topic.trim() || create.isPending}
          className="rounded-md px-4 py-2 text-sm font-medium disabled:opacity-40"
          style={{
            background: "var(--color-state-generating)",
            color: "var(--color-surface)",
          }}
        >
          {create.isPending ? "Creating…" : "Create"}
        </button>
      </form>

      {(create.error ?? remove.error) ? (
        <p className="text-sm" style={{ color: "var(--color-state-failed)" }}>
          {(create.error ?? remove.error)?.message}
        </p>
      ) : null}

      {projects.isPending ? (
        <p className="text-sm" style={{ color: "var(--color-ink-muted)" }}>
          Loading…
        </p>
      ) : projects.error ? (
        <p className="text-sm" style={{ color: "var(--color-state-failed)" }}>
          {projects.error.message}
        </p>
      ) : projects.data.items.length === 0 ? (
        <p className="text-sm" style={{ color: "var(--color-ink-muted)" }}>
          No projects yet. Enter a topic above to start one.
        </p>
      ) : (
        // **Grouped by who is blocked, not listed by recency.** A flat list
        // answers "what exists"; the question someone actually opens this app
        // with is "what needs me". The order of the groups is that question's
        // answer, and it is why `waiting_on` is derived on the server — the
        // phase string cannot tell "a worker has this" from "nobody has
        // started it", so grouping on it here would file the one project
        // needing attention under Working.
        <div className="flex flex-col gap-6">
          {GROUPS.map(({ key, title, hint }) => {
            const items = projects.data.items.filter(
              (project) => project.waiting_on === key,
            );
            // Empty groups are omitted rather than shown empty: three headings
            // over two projects is a filing cabinet, not a dashboard.
            if (items.length === 0) {
              return null;
            }
            return (
              <section
                key={key}
                data-testid={`group-${key}`}
                className="flex flex-col gap-2"
              >
                <h2
                  className="text-xs font-medium uppercase tracking-wide"
                  style={{ color: "var(--color-ink-muted)" }}
                >
                  {title}{" "}
                  <span style={{ opacity: 0.7 }}>
                    {items.length} · {hint}
                  </span>
                </h2>
                <ul className="flex flex-col gap-2">
                  {items.map((project) => (
                    <ProjectRow
                      key={project.id}
                      project={project}
                      confirming={pendingDelete === project.id}
                      deleting={remove.isPending}
                      onArm={() => setPendingDelete(project.id)}
                      onDisarm={() => setPendingDelete(null)}
                      onDelete={() => remove.mutate(project.id)}
                    />
                  ))}
                </ul>
              </section>
            );
          })}
        </div>
      )}
    </div>
  );
}

/** The dashboard's three buckets, in the order someone scans them.
 *
 * "Waiting on you" first and always: it is the only group a person can act on,
 * and burying it under a longer list of things that are already moving is how
 * a review queue silently grows.
 */
const GROUPS: { key: WaitingOn; title: string; hint: string }[] = [
  { key: "human", title: "Waiting on you", hint: "approve, retry or generate" },
  { key: "machine", title: "Working", hint: "a worker has it" },
  { key: "nobody", title: "Done", hint: "package ready" },
];

function ProjectRow({
  project,
  confirming,
  deleting,
  onArm,
  onDisarm,
  onDelete,
}: {
  project: ProjectSummary;
  confirming: boolean;
  deleting: boolean;
  onArm: () => void;
  onDisarm: () => void;
  onDelete: () => void;
}) {
  return (
    <li
      data-testid={`project-row-${project.id}`}
      className="flex items-center gap-2 rounded-md px-4 py-3 text-sm"
      style={{
        background: "var(--color-surface-raised)",
        border: `1px solid ${
          confirming
            ? "var(--color-state-failed)"
            : "var(--color-border-subtle)"
        }`,
      }}
    >
      {/* The link no longer wraps the row: a delete control inside an anchor
          is a click target inside a click target, and the one time it
          mis-fires it navigates instead of deleting — or worse, the other way
          round. */}
      <Link
        href={`/projects/${project.id}`}
        className="flex flex-1 items-center justify-between gap-4"
      >
        <span>{project.title ?? project.topic}</span>
        <span className="flex items-center gap-3">
          {/* §12.4's HAS_FAILURES, orthogonal to the phase and invisible until
              now. A failed image among twenty does not move the phase — the
              other nineteen are fine — so a broken project sat in this list
              looking exactly like a healthy one. */}
          {project.has_failures ? (
            <span
              data-testid={`project-failed-${project.id}`}
              style={{ color: "var(--color-state-failed)" }}
            >
              Needs attention
            </span>
          ) : null}
          <span style={{ color: "var(--color-ink-muted)" }}>
            {humanise(project.phase)}
          </span>
        </span>
      </Link>

      {/* **Two steps, not a `confirm()` dialog.** This is the only
          irreversible action in the app — it takes the artifacts, versions,
          scenes and packages with it — so it should not be one click. An
          inline second press keeps the project's name on screen while you
          decide, which a modal covers up, and it is reachable by keyboard
          without trapping focus. */}
      {confirming ? (
        <>
          <button
            type="button"
            data-testid={`confirm-delete-${project.id}`}
            disabled={deleting}
            onClick={onDelete}
            className="rounded px-2 py-1 text-xs disabled:opacity-40"
            style={{
              border: "1px solid var(--color-state-failed)",
              color: "var(--color-state-failed)",
            }}
          >
            {deleting ? "Deleting…" : "Delete for good"}
          </button>
          <button
            type="button"
            onClick={onDisarm}
            className="rounded px-2 py-1 text-xs"
            style={{ color: "var(--color-ink-muted)" }}
          >
            Cancel
          </button>
        </>
      ) : (
        <button
          type="button"
          aria-label={`Delete ${project.title ?? project.topic}`}
          data-testid={`delete-${project.id}`}
          onClick={onArm}
          className="rounded px-2 py-1 text-xs"
          style={{ color: "var(--color-ink-muted)" }}
        >
          Delete
        </button>
      )}
    </li>
  );
}
