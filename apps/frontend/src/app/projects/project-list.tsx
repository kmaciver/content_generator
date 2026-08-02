"use client";

import Link from "next/link";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "@/lib/api";
import { humanise } from "@/lib/state-colors";

export function ProjectList() {
  const queryClient = useQueryClient();
  const [topic, setTopic] = useState("");

  const projects = useQuery({
    queryKey: ["projects"],
    queryFn: api.listProjects,
  });

  const create = useMutation({
    mutationFn: (value: string) => api.createProject(value),
    onSuccess: () => {
      setTopic("");
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

      {create.error ? (
        <p className="text-sm" style={{ color: "var(--color-state-failed)" }}>
          {create.error.message}
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
          {projects.data.items.map((project) => (
            <li key={project.id}>
              <Link
                href={`/projects/${project.id}`}
                className="flex items-center justify-between rounded-md px-4 py-3 text-sm"
                style={{
                  background: "var(--color-surface-raised)",
                  border: "1px solid var(--color-border-subtle)",
                }}
              >
                <span>{project.title ?? project.topic}</span>
                <span style={{ color: "var(--color-ink-muted)" }}>
                  {humanise(project.phase)}
                </span>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
