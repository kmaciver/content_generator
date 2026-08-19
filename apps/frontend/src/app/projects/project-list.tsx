"use client";

import Link from "next/link";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "@/lib/api";
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
        <ul className="flex flex-col gap-2">
          {projects.data.items.map((project) => {
            const confirming = pendingDelete === project.id;
            return (
              <li
                key={project.id}
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
                {/* The link no longer wraps the row: a delete control inside
                    an anchor is a click target inside a click target, and the
                    one time it mis-fires it navigates instead of deleting —
                    or worse, the other way round. */}
                <Link
                  href={`/projects/${project.id}`}
                  className="flex flex-1 items-center justify-between gap-4"
                >
                  <span>{project.title ?? project.topic}</span>
                  <span style={{ color: "var(--color-ink-muted)" }}>
                    {humanise(project.phase)}
                  </span>
                </Link>

                {/* **Two steps, not a `confirm()` dialog.** This is the only
                    irreversible action in the app — it takes the artifacts,
                    versions, scenes and packages with it — so it should not be
                    one click. An inline second press keeps the project's name
                    on screen while you decide, which a modal covers up, and it
                    is reachable by keyboard without trapping focus. */}
                {confirming ? (
                  <>
                    <button
                      type="button"
                      data-testid={`confirm-delete-${project.id}`}
                      disabled={remove.isPending}
                      onClick={() => remove.mutate(project.id)}
                      className="rounded px-2 py-1 text-xs disabled:opacity-40"
                      style={{
                        border: "1px solid var(--color-state-failed)",
                        color: "var(--color-state-failed)",
                      }}
                    >
                      {remove.isPending ? "Deleting…" : "Delete for good"}
                    </button>
                    <button
                      type="button"
                      onClick={() => setPendingDelete(null)}
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
                    onClick={() => setPendingDelete(project.id)}
                    className="rounded px-2 py-1 text-xs"
                    style={{ color: "var(--color-ink-muted)" }}
                  >
                    Delete
                  </button>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
