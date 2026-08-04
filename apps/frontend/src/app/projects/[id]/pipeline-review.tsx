"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, type ArtifactSummary } from "@/lib/api";
import { artifactStateColor, humanise } from "@/lib/state-colors";
import { StageContent } from "./stage-content";
import { StageRail } from "./stage-rail";
import { VersionSwitcher } from "./version-switcher";

// Polling, not SSE. ADR-006 commits to polling first, and finding S7 defers the
// event consumer to M5 — the outbox and its drain exist, but nothing subscribes
// yet. 1.5s while a job is in flight, off otherwise: the interval only matters
// when something is actually moving.
const POLL_MS = 1500;

/** The project screen (M2-13).
 *
 * M1-09 shipped this as a script-only view because script was the only stage.
 * Generalising it — rather than adding three more near-identical components —
 * is what keeps the review contract in one place: every stage is an artifact
 * with versions, a capabilities payload and a review decision, and the moment
 * that stops being true for one of them the design has drifted.
 *
 * Which stage is *editable as text* is the one real difference, and it is
 * derived from the content shape rather than hardcoded per kind.
 */
export function PipelineReview({ projectId }: { projectId: string }) {
  const queryClient = useQueryClient();
  const [selectedKind, setSelectedKind] = useState<string | null>(null);
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

  const stages = project.data?.stages ?? [];
  // Derived, not synchronised: default to the furthest stage that has anything
  // to look at, and let an explicit choice override. An effect that "kept the
  // selection up to date" would move the reviewer's focus every time a poll
  // landed.
  const defaultKind =
    [...stages].reverse().find((s) => s.artifact_id)?.kind ?? null;
  const activeKind = selectedKind ?? defaultKind;
  const artifactSummary = project.data?.artifacts.find(
    (a) => a.kind === activeKind && a.scene_ref === null,
  );

  const artifact = useQuery({
    queryKey: ["artifact", artifactSummary?.id],
    queryFn: () => api.getArtifact(artifactSummary!.id),
    enabled: Boolean(artifactSummary),
    // Polls on ITS OWN state, not on the project's view of it.
    //
    // Keying this off the project's copy was a real bug (M1-09a): the project
    // query polls too, sees AWAITING_APPROVAL first, and that switches this
    // query's interval off — so `capabilities` stayed frozen at the GENERATING
    // values and Approve never enabled. The generation had finished in 222ms.
    refetchInterval: (query) =>
      (query.state.data?.state ?? artifactSummary?.state) === "GENERATING"
        ? POLL_MS
        : false,
  });

  const versions = artifact.data?.versions ?? [];
  const current =
    versions.find((v) => v.version_no === selectedVersion) ?? versions[0];

  const detail = useQuery({
    queryKey: ["version", artifactSummary?.id, current?.version_no],
    queryFn: () => api.getVersion(artifactSummary!.id, current!.version_no),
    enabled: Boolean(artifactSummary && current),
  });

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ["project", projectId] });
    // The whole ["artifact"] prefix rather than one id: on a first Generate
    // there is no artifact yet, so a targeted invalidation would address
    // ["artifact", undefined] — a key nothing uses.
    void queryClient.invalidateQueries({ queryKey: ["artifact"] });
    void queryClient.invalidateQueries({ queryKey: ["version"] });
  };

  const generate = useMutation({
    mutationFn: ({ kind, regenerate }: { kind: string; regenerate: boolean }) =>
      api.generate(projectId, kind, regenerate),
    onSuccess: (_data, variables) => {
      setSelectedKind(variables.kind);
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

  const editableField =
    detail.data?.content?.script !== undefined ? "script" : null;

  const saveEdit = useMutation({
    mutationFn: (text: string) =>
      api.edit(artifactSummary!.id, {
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
        <p
          data-testid="project-phase"
          className="mt-1 text-sm"
          style={{ color: "var(--color-ink-muted)" }}
        >
          {humanise(project.data.phase)}
        </p>
      </header>

      {error ? <Failed>{error.message}</Failed> : null}

      <StageRail
        stages={stages}
        selected={activeKind ?? ""}
        onSelect={(kind) => {
          setSelectedKind(kind);
          setSelectedVersion(null);
          setDraft(null);
        }}
        onGenerate={(kind, regenerate) => generate.mutate({ kind, regenerate })}
        busy={busy}
      />

      {artifactSummary ? (
        <section className="flex flex-col gap-5">
          <StateBadge artifact={artifact.data ?? artifactSummary} />

          {artifactSummary.state === "GENERATING" ? (
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
                <StageContent
                  kind={activeKind ?? ""}
                  content={detail.data.content}
                />
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
                  onClick={() =>
                    generate.mutate({ kind: activeKind!, regenerate: true })
                  }
                />
                <Action
                  // Only where free text is the content. A scene set is a table
                  // of rows the voice stage synthesises verbatim; a textarea
                  // over its JSON is an invitation to desynchronise captions
                  // from audio with no way to detect it.
                  label="Edit"
                  enabled={
                    Boolean(caps?.can_edit) &&
                    !busy &&
                    Boolean(detail.data) &&
                    editableField !== null
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
      ) : (
        <Muted>Nothing generated yet. Start with the first stage above.</Muted>
      )}
    </>
  );
}

function StateBadge({ artifact }: { artifact: ArtifactSummary }) {
  return (
    // `aria-live` so a screen reader announces the transition when the poll
    // lands. The testid exists because an artifact's state and a version's
    // status legitimately share words — "Approved" appears in both — and a text
    // locator cannot tell them apart.
    <div
      className="flex items-center gap-2 text-sm"
      data-testid="artifact-state"
      aria-live="polite"
    >
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
