"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, type ArtifactSummary } from "@/lib/api";
import { artifactStateColor, humanise } from "@/lib/state-colors";
import { VersionSwitcher } from "./version-switcher";

// Polling, not SSE. ADR-006 commits to polling first, and finding S7 defers
// the event consumer to M5 — the outbox and its drain exist, but nothing
// subscribes yet. 1.5s while a job is in flight, off otherwise: the interval
// only matters when something is actually moving.
const POLL_MS = 1500;

export function ScriptReview({ projectId }: { projectId: string }) {
  const queryClient = useQueryClient();
  const [selectedVersion, setSelectedVersion] = useState<number | null>(null);
  const [draft, setDraft] = useState<string | null>(null);
  const [comment, setComment] = useState("");

  const project = useQuery({
    queryKey: ["project", projectId],
    queryFn: () => api.getProject(projectId),
    refetchInterval: (query) =>
      query.state.data?.artifacts.some((a) => a.state === "GENERATING")
        ? POLL_MS
        : false,
  });

  const script = project.data?.artifacts.find((a) => a.kind === "script");

  const artifact = useQuery({
    queryKey: ["artifact", script?.id],
    queryFn: () => api.getArtifact(script!.id),
    enabled: Boolean(script),
    refetchInterval: script?.state === "GENERATING" ? POLL_MS : false,
  });

  const versions = artifact.data?.versions ?? [];
  // Selection is *derived*, not synchronised. `selectedVersion` records only a
  // deliberate choice; everything else falls through to the newest version.
  //
  // The obvious alternative — an effect that copies the newest version into
  // state — is what React's `set-state-in-effect` rule exists to stop, and it
  // would also be wrong here: it re-runs whenever a poll returns, so a
  // regeneration landing mid-read would yank the reviewer off the version they
  // were looking at. Setting `selectedVersion` back to null after a mutation is
  // the explicit way to say "follow the newest again".
  const current =
    versions.find((v) => v.version_no === selectedVersion) ?? versions[0];

  const detail = useQuery({
    queryKey: ["version", script?.id, current?.version_no],
    queryFn: () => api.getVersion(script!.id, current!.version_no),
    enabled: Boolean(script && current),
  });

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ["project", projectId] });
    void queryClient.invalidateQueries({ queryKey: ["artifact", script?.id] });
    void queryClient.invalidateQueries({ queryKey: ["version"] });
  };

  const generate = useMutation({
    mutationFn: (regenerate: boolean) =>
      api.generate(projectId, "script", regenerate),
    onSuccess: () => {
      setSelectedVersion(null);
      invalidate();
    },
  });

  const decide = useMutation({
    mutationFn: ({ approve }: { approve: boolean }) => {
      const action = approve ? api.approve : api.reject;
      return action(current!.id, current!.version_no, comment || undefined);
    },
    onSuccess: () => {
      setComment("");
      invalidate();
    },
  });

  const saveEdit = useMutation({
    mutationFn: (text: string) =>
      api.edit(script!.id, {
        ...(detail.data?.content ?? {}),
        script: text,
      }),
    onSuccess: () => {
      setDraft(null);
      setSelectedVersion(null);
      invalidate();
    },
  });

  if (project.isPending) {
    return <Muted>Loading project…</Muted>;
  }
  if (project.error) {
    return <Failed>{project.error.message}</Failed>;
  }

  const caps = artifact.data?.capabilities;
  const busy = generate.isPending || decide.isPending || saveEdit.isPending;
  const error = generate.error ?? decide.error ?? saveEdit.error;

  return (
    <>
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">
          {project.data.title ?? project.data.topic}
        </h1>
        <p className="mt-1 text-sm" style={{ color: "var(--color-ink-muted)" }}>
          {humanise(project.data.phase)}
        </p>
      </header>

      {error ? <Failed>{error.message}</Failed> : null}

      {!script ? (
        <section className="flex flex-col gap-3">
          <Muted>No script yet.</Muted>
          <button
            type="button"
            onClick={() => generate.mutate(false)}
            disabled={busy}
            className="self-start rounded-md px-4 py-2 text-sm font-medium disabled:opacity-40"
            style={{
              background: "var(--color-state-generating)",
              color: "var(--color-surface)",
            }}
          >
            {generate.isPending ? "Starting…" : "Generate script"}
          </button>
        </section>
      ) : (
        <section className="flex flex-col gap-5">
          <StateBadge artifact={artifact.data ?? script} />

          {script.state === "GENERATING" ? (
            <Muted>Generating… this page updates itself.</Muted>
          ) : null}

          <VersionSwitcher
            versions={versions}
            selected={current?.version_no ?? 0}
            onSelect={setSelectedVersion}
          />

          {detail.data ? (
            <article
              className="rounded-md p-4 text-sm leading-relaxed"
              style={{
                background: "var(--color-surface-raised)",
                border: "1px solid var(--color-border-subtle)",
              }}
            >
              {draft === null ? (
                <p className="whitespace-pre-wrap">
                  {String(detail.data.content?.script ?? "")}
                </p>
              ) : (
                <textarea
                  value={draft}
                  onChange={(event) => setDraft(event.target.value)}
                  rows={12}
                  aria-label="Script"
                  className="w-full resize-y bg-transparent outline-none"
                  style={{ color: "var(--color-ink)" }}
                />
              )}
            </article>
          ) : null}

          {detail.data ? (
            <p className="text-xs" style={{ color: "var(--color-ink-muted)" }}>
              {/* Provenance, surfaced rather than buried: §10.3 rule 4 only
                  helps if a reviewer can see it. */}
              {detail.data.provider_ref ?? "unknown provider"} ·{" "}
              {detail.data.prompt_template_ref ?? "no template"} ·{" "}
              {detail.data.content_hash.slice(0, 12)}
            </p>
          ) : null}

          {draft === null ? (
            <>
              <input
                value={comment}
                onChange={(event) => setComment(event.target.value)}
                placeholder="Add a note with your decision (optional)"
                aria-label="Review comment"
                className="rounded-md px-3 py-2 text-sm"
                style={{
                  background: "var(--color-surface-raised)",
                  border: "1px solid var(--color-border-subtle)",
                  color: "var(--color-ink)",
                }}
              />
              <div className="flex flex-wrap gap-2">
                {/* Every button below is gated by the server's capabilities
                    payload, computed from the domain FSM (§11). Nothing here
                    decides for itself whether an action is legal. */}
                <Action
                  label="Approve"
                  enabled={Boolean(caps?.can_approve) && !busy}
                  tone="var(--color-state-ok)"
                  onClick={() => decide.mutate({ approve: true })}
                />
                <Action
                  label="Reject"
                  enabled={Boolean(caps?.can_reject) && !busy}
                  tone="var(--color-state-failed)"
                  onClick={() => decide.mutate({ approve: false })}
                />
                <Action
                  label="Regenerate"
                  enabled={Boolean(caps?.can_regenerate) && !busy}
                  tone="var(--color-state-generating)"
                  onClick={() => generate.mutate(true)}
                />
                <Action
                  label="Edit"
                  enabled={
                    Boolean(caps?.can_edit) && !busy && Boolean(detail.data)
                  }
                  tone="var(--color-ink-muted)"
                  onClick={() =>
                    setDraft(String(detail.data?.content?.script ?? ""))
                  }
                />
              </div>
            </>
          ) : (
            <div className="flex gap-2">
              <Action
                label={saveEdit.isPending ? "Saving…" : "Save as new version"}
                enabled={!busy}
                tone="var(--color-state-ok)"
                onClick={() => saveEdit.mutate(draft)}
              />
              <Action
                label="Cancel"
                enabled={!busy}
                tone="var(--color-ink-muted)"
                onClick={() => setDraft(null)}
              />
            </div>
          )}
        </section>
      )}
    </>
  );
}

function StateBadge({ artifact }: { artifact: ArtifactSummary }) {
  return (
    <div className="flex items-center gap-2 text-sm">
      <span
        aria-hidden
        className="inline-block size-2 rounded-full"
        style={{ background: artifactStateColor(artifact.state) }}
      />
      <span>{humanise(artifact.state)}</span>
      {artifact.stale_since ? (
        <span style={{ color: "var(--color-state-review)" }}>
          · stale since {new Date(artifact.stale_since).toLocaleString()}
        </span>
      ) : null}
    </div>
  );
}

function Action({
  label,
  enabled,
  tone,
  onClick,
}: {
  label: string;
  enabled: boolean;
  tone: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={!enabled}
      className="rounded-md px-4 py-2 text-sm font-medium disabled:opacity-30"
      style={{ border: `1px solid ${tone}`, color: tone }}
    >
      {label}
    </button>
  );
}

function Muted({ children }: { children: React.ReactNode }) {
  return (
    <p className="text-sm" style={{ color: "var(--color-ink-muted)" }}>
      {children}
    </p>
  );
}

function Failed({ children }: { children: React.ReactNode }) {
  return (
    <p className="text-sm" style={{ color: "var(--color-state-failed)" }}>
      {children}
    </p>
  );
}
