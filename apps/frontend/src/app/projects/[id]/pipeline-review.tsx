"use client";

import Link from "next/link";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  api,
  REJECTION_LABELS,
  type ArtifactSummary,
  type RejectionReason,
} from "@/lib/api";
import { artifactStateColor, humanise } from "@/lib/state-colors";
import { ContactSheet } from "./contact-sheet";
import { SceneSelector } from "./scene-selector";
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
  // `null` means the stage's set-level artifact; a scene id narrows the
  // review panel to that scene's own artifact.
  //
  // Stored **with the kind it was chosen under**. The rail already resets it
  // when a reviewer picks a different stage, and that was only half the rule:
  // `activeKind` also moves on its own as the default advances to the furthest
  // stage with an artifact. So reviewing prompts scene by scene and then
  // generating images left the selection pointing at scene N of a stage nobody
  // was looking at any more — and since the contact sheet only renders for the
  // whole set, the reviewer got a single picture instead of the grid, with no
  // visible reason. Found by M4-12.
  const [selectedScene, setSelectedScene] = useState<{
    kind: string;
    id: string;
  } | null>(null);
  const [selectedVersion, setSelectedVersion] = useState<number | null>(null);
  const [draft, setDraft] = useState<string | null>(null);
  const [comment, setComment] = useState("");
  // Cleared on every decision: reasons describe *this* version, and carrying
  // them to the next one would attribute a complaint nobody made.
  const [reasons, setReasons] = useState<RejectionReason[]>([]);

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
  // Derived rather than cleared in an effect, for the same reason `activeKind`
  // is derived: an effect that reset it would fire a render late and after the
  // wrong panel had already been drawn.
  const activeScene =
    selectedScene && selectedScene.kind === activeKind
      ? selectedScene.id
      : null;
  const artifactSummary = project.data?.artifacts.find(
    (a) => a.kind === activeKind && a.scene_ref === activeScene,
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
    // The contact sheet too, and its absence was a real bug: regenerating one
    // scene from this panel produced a v2 the *grid* never showed, because
    // `ContactSheet` only invalidated its own key from its own buttons. The
    // reviewer saw a new version in the switcher and the old picture in the
    // sheet, which reads as the regeneration having silently done nothing.
    //
    // Listed here rather than made the sheet's problem: this component owns
    // the actions that change a scene's latest version, so it owns telling
    // everything that displays one.
    void queryClient.invalidateQueries({ queryKey: ["contact-sheet"] });
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

  // Lands the artifact in FAILED, which is retryable — so the ordinary
  // Regenerate button is the next step, and there is no second retry path to
  // keep in step with the first.
  const release = useMutation({
    mutationFn: (artifactId: string) => api.release(artifactId),
    onSuccess: invalidate,
  });

  const decide = useMutation({
    mutationFn: ({ approve }: { approve: boolean }) =>
      approve
        ? api.approve(current!.id, current!.version_no, comment || undefined)
        : api.reject(
            current!.id,
            current!.version_no,
            comment || undefined,
            reasons,
          ),
    onSuccess: () => {
      setComment("");
      setReasons([]);
      invalidate();
    },
  });

  // Which single text field this stage's editor writes to.
  //
  // Derived from the content shape rather than keyed on kind, which is what
  // let `caption` (M5-01) become editable without touching this rule — and it
  // had to, because the caption is the one artifact here that is *published*
  // verbatim and so the one people will most often want to reword.
  //
  // Deliberately one field. A caption's hook and hashtags are editable only by
  // regenerating today; giving each its own input is a real editor and a
  // different ticket, and pretending a textarea covers them would silently
  // drop two of the three things a reviewer came to change.
  const editableField =
    detail.data?.content?.script !== undefined
      ? "script"
      : detail.data?.content?.caption !== undefined
        ? "caption"
        : null;

  const saveEdit = useMutation({
    mutationFn: (text: string) =>
      api.edit(artifactSummary!.id, {
        ...(detail.data?.content ?? {}),
        [editableField ?? "script"]: text,
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
  // Server-owned, like `caps` (M3-10 fix): every reason in the vocabulary
  // describes a picture, so the list is empty for a narration, a script or a
  // timeline and the reviewer gets the comment box alone. Rendering one
  // hardcoded list here offered "Anatomy" on a voice take.
  const available = artifact.data?.rejection_reasons ?? [];
  const busy = generate.isPending || decide.isPending || saveEdit.isPending;
  const error = generate.error ?? decide.error ?? saveEdit.error;

  return (
    <>
      <header className="flex flex-col gap-1">
        {/* The way out. This screen was reachable and not leavable — the only
            route back to the list was the browser's own Back button, or
            editing the URL. Matches the idiom `series/[id]` already uses
            rather than inventing a second one: a small muted link above the
            title, not a global chrome bar. */}
        <Link
          href="/projects"
          className="text-xs"
          style={{ color: "var(--color-ink-muted)" }}
        >
          ← Projects
        </Link>
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
          // Scene selection does not survive a stage change: scene 4 of the
          // prompts is not scene 4 of anything else, and silently carrying it
          // across would show the reviewer a different artifact than the one
          // they think they picked.
          setSelectedScene(null);
          setSelectedVersion(null);
          setDraft(null);
        }}
        onGenerate={(kind, regenerate) => generate.mutate({ kind, regenerate })}
        busy={busy}
      />

      <SceneSelector
        scenes={project.data.scenes}
        artifacts={project.data.artifacts}
        kind={activeKind ?? ""}
        selected={activeScene}
        onSelect={(sceneRef) => {
          setSelectedScene(
            sceneRef && activeKind ? { kind: activeKind, id: sceneRef } : null,
          );
          setSelectedVersion(null);
          setDraft(null);
        }}
      />

      {/* Only for kinds whose review unit is a picture. A contact sheet of
          twenty prompts is twenty paragraphs in a grid — harder to read than
          the list, not easier — so this is not "any per-scene kind". */}
      {activeKind === "image" && activeScene === null ? (
        <ContactSheet
          projectId={projectId}
          kind={activeKind}
          // The sheet cannot see the stage from its own tiles while the
          // fan-out is still running — every cell is empty until artifacts
          // exist. Passed rather than fetched again: this component already
          // polls the project.
          stageState={stages.find((s) => s.kind === activeKind)?.state ?? null}
          onOpenScene={(sceneId) => {
            setSelectedScene({ kind: "image", id: sceneId });
            setSelectedVersion(null);
            setDraft(null);
          }}
        />
      ) : null}

      {artifactSummary ? (
        <section className="flex flex-col gap-5">
          <StateBadge artifact={artifact.data ?? artifactSummary} />

          {artifactSummary.state === "GENERATING" ? (
            // The escape hatch (M5-05), offered *here* because this is the
            // screen someone is looking at when a stage stops moving. Without
            // it a job the broker lost leaves the stage unrunnable forever —
            // the idempotency key stays held by the parked row, so even
            // Regenerate deduplicates onto it, and the only cure was psql.
            <div className="flex flex-wrap items-center gap-3">
              <Muted>Generating… this page updates itself.</Muted>
              <button
                type="button"
                data-testid="release-stage"
                disabled={release.isPending}
                onClick={() => release.mutate(artifactSummary.id)}
                className="rounded px-3 py-1 text-xs disabled:opacity-40"
                style={{
                  border: "1px solid var(--color-border-subtle)",
                  color: "var(--color-ink-muted)",
                }}
              >
                {release.isPending ? "Releasing…" : "Not moving? Release it"}
              </button>
            </div>
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
                  meta={detail.data.meta}
                  // Server-built (M4-11). This used to compose
                  // `/assets/assets/{key}` here, which was right for images
                  // and voice and wrong for a render — those live in the
                  // artifacts bucket, so the guess would have 403'd on the
                  // first video the pipeline ever produced.
                  assetUrl={detail.data.asset_url ?? null}
                  // Grouped by the server from the same function the timeline
                  // compiler calls, so the preview's captions are the ones the
                  // render burns rather than a second guess at them.
                  captionCues={detail.data.caption_cues}
                />
              ) : (
                <textarea
                  value={draft}
                  onChange={(event) => setDraft(event.target.value)}
                  rows={12}
                  // Labelled by the field it edits, not "Script": the e2e
                  // finds this control by label, and a caption editor calling
                  // itself Script is a lie to a screen reader as well.
                  aria-label={
                    editableField === "caption" ? "Caption" : "Script"
                  }
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
              {/* Why, from a fixed vocabulary (M3-10). These become the
                  correction block the next attempt carries, so a rejection
                  with none tells the model nothing it did not already know.
                  Shown only when rejecting is legal — offering them next to a
                  disabled Reject would be a control that does nothing. */}
              {caps?.can_reject && available.length > 0 ? (
                <fieldset className="flex flex-wrap items-center gap-2">
                  <legend className="sr-only">Rejection reasons</legend>
                  <span
                    className="text-xs"
                    style={{ color: "var(--color-ink-muted)" }}
                  >
                    What is wrong?
                  </span>
                  {available.map((reason) => {
                    const on = reasons.includes(reason);
                    return (
                      <button
                        key={reason}
                        type="button"
                        aria-pressed={on}
                        data-testid={`reason-${reason}`}
                        onClick={() =>
                          setReasons((current) =>
                            current.includes(reason)
                              ? current.filter((r) => r !== reason)
                              : [...current, reason],
                          )
                        }
                        className="rounded-full px-3 py-1 text-xs"
                        style={{
                          border: `1px solid ${
                            on
                              ? "var(--color-state-failed)"
                              : "var(--color-border-subtle)"
                          }`,
                          color: on
                            ? "var(--color-state-failed)"
                            : "var(--color-ink-muted)",
                        }}
                      >
                        {REJECTION_LABELS[reason]}
                      </button>
                    );
                  })}
                </fieldset>
              ) : null}
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
                    setDraft(
                      String(
                        detail.data?.content?.[editableField ?? "script"] ?? "",
                      ),
                    )
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
