"use client";

import type { CaptionCue, PackageFile } from "@/lib/api";

import { NarrationPlayer, type VoiceSpan } from "./narration-player";
import { RenderPlayer, type RenderMark } from "./render-player";

/** Rendering one stage's content (M2-13).
 *
 * Each stage produces a different shape, and this is the one place that knows
 * which. The alternative — a generic JSON viewer — would technically display
 * everything and let a reviewer judge none of it: a scene breakdown is a table
 * with durations, and reading it as pretty-printed JSON is how a pacing problem
 * goes unnoticed.
 *
 * Unknown kinds fall back to JSON deliberately, so a stage added in M3 is
 * visible before its viewer is written rather than showing a blank panel.
 */
export function StageContent({
  kind,
  content,
  meta,
  assetUrl,
  captionCues,
}: {
  kind: string;
  content: Record<string, unknown> | null | undefined;
  /** Media stages carry their reviewable substance in `meta`, not `content` —
   * the version's content column holds a storage key, not JSON. */
  meta?: Record<string, unknown>;
  assetUrl?: string | null;
  /** Server-grouped captions, for the kinds that have word timings. Passed in
   * rather than dug out of `meta`, because unlike everything else here they
   * are derived at read time and not stored on the version. */
  captionCues?: CaptionCue[];
}) {
  if (kind === "voice") {
    // The narration is judged by listening, so the player *is* the viewer.
    // Falls through to nothing rather than to a JSON dump: a reviewer shown
    // 200 word timings as pretty-printed JSON cannot hear a mistimed word.
    const spans = Array.isArray(meta?.spans)
      ? (meta.spans as unknown as VoiceSpan[])
      : [];
    if (!assetUrl || spans.length === 0) {
      return null;
    }
    return (
      <NarrationPlayer
        audioUrl={assetUrl}
        spans={spans}
        cues={captionCues ?? []}
        durationMs={Number(meta?.duration_ms ?? 0)}
      />
    );
  }

  if (kind === "render") {
    // Like `voice`, the substance is the media and the reviewable detail is in
    // `meta` — the version's content column holds a storage key. Falls through
    // to nothing rather than JSON: a reviewer shown an ffmpeg filter graph
    // cannot tell whether the video is any good.
    const marks = Array.isArray(meta?.scene_marks)
      ? (meta.scene_marks as unknown as RenderMark[])
      : [];
    if (!assetUrl) {
      return null;
    }
    return (
      <RenderPlayer
        videoUrl={assetUrl}
        marks={marks}
        durationMs={Number(meta?.duration_ms ?? 0)}
      />
    );
  }

  if (kind === "package") {
    // **The review unit is the manifest, not a preview.** There is nothing to
    // watch here — the video was reviewed at the render stage — and the
    // questions left are "does it contain what it says" and "can I get it".
    // So: every entry with its hash and size, and a download.
    if (!assetUrl) {
      return null;
    }
    const manifest = (meta?.manifest ?? {}) as Record<string, unknown>;
    const files = Array.isArray(manifest.files)
      ? (manifest.files as PackageFile[])
      : [];
    return (
      <section className="flex flex-col gap-3" data-testid="package-review">
        <div className="flex flex-wrap items-center gap-3">
          <a
            href={assetUrl}
            download
            data-testid="package-download"
            className="rounded-md px-4 py-2 text-sm font-medium"
            style={{
              border: "1px solid var(--color-state-ok)",
              color: "var(--color-state-ok)",
            }}
          >
            Download package
          </a>
          <span className="text-xs" style={{ color: "var(--color-ink-muted)" }}>
            {files.length} files · {formatBytes(Number(meta?.bytes ?? 0))}
          </span>
        </div>

        {/* Hashes shown, not hidden behind a "verify" button. They are the
            reason the manifest exists: a recipient can check the archive
            rather than trust it (ADR-004, carried past the boundary where the
            bytes leave the system). */}
        <div className="overflow-x-auto">
          <table className="w-full text-left text-xs">
            <thead style={{ color: "var(--color-ink-muted)" }}>
              <tr>
                <th className="py-1 pr-4 font-normal">File</th>
                <th className="py-1 pr-4 font-normal">Size</th>
                <th className="py-1 font-normal">sha256</th>
              </tr>
            </thead>
            <tbody>
              {files.map((file) => (
                <tr key={file.path}>
                  <td className="py-1 pr-4">{file.path}</td>
                  <td
                    className="py-1 pr-4 tabular-nums"
                    style={{ color: "var(--color-ink-muted)" }}
                  >
                    {formatBytes(file.bytes)}
                  </td>
                  <td
                    className="py-1 font-mono"
                    style={{ color: "var(--color-ink-muted)" }}
                    // The full digest, in a title: truncation is what makes a
                    // hash unverifiable, and a reviewer who wants to check one
                    // needs all of it.
                    title={file.sha256}
                  >
                    {file.sha256.slice(0, 12)}…
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    );
  }

  if (kind === "image") {
    // **One scene's frame — and this branch was simply missing.** Every other
    // kind had one; `image` fell through to `if (!content) return null`, and
    // an image version has no inline content (the CHECK permits a storage key
    // *or* JSON, never both), so opening a scene showed an empty panel.
    //
    // It went unnoticed because the contact sheet renders pictures at the set
    // level, so the *grid* always worked. The failure only appears where a
    // reviewer opens one scene — which is exactly what they do after asking
    // for a regeneration, to see whether the new version is any better.
    if (!assetUrl) {
      return null;
    }
    const dimensions =
      meta?.width && meta?.height ? `${meta.width}×${meta.height}` : null;
    return (
      <section className="flex flex-col gap-2" data-testid="scene-frame">
        {/* Big. This is where character drift, anatomy and stray text are
            judged, and every one of those is invisible at thumbnail size —
            the reasons in the rejection vocabulary are the specification for
            how large this has to be. */}
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img
          src={assetUrl}
          alt={`Scene ${String(meta?.scene_index ?? "")}`}
          className="w-full max-w-[420px] rounded-md"
          style={{ border: "1px solid var(--color-border-subtle)" }}
        />
        <p className="text-xs" style={{ color: "var(--color-ink-muted)" }}>
          {meta?.scene_index ? `Scene ${String(meta.scene_index)}` : null}
          {dimensions ? ` · ${dimensions}` : null}
          {meta?.kind === "card" ? " · card, rendered locally" : null}
        </p>
      </section>
    );
  }

  if (kind === "thumbnail") {
    // The cover, at the size it is actually judged at. A 1080-wide preview
    // makes every cover look fine; the question a reviewer is answering is
    // whether the hook is readable in a feed, so it is shown small — and
    // beside it, cropped to the square the profile grid keeps (M5-02), which
    // is where a hook placed too low disappears.
    if (!assetUrl) {
      return null;
    }
    return (
      <section className="flex flex-wrap gap-4" data-testid="cover-preview">
        <figure className="flex flex-col gap-1">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            src={assetUrl}
            alt="Reels cover"
            className="w-[160px] rounded-md"
            style={{ border: "1px solid var(--color-border-subtle)" }}
          />
          <figcaption
            className="text-xs"
            style={{ color: "var(--color-ink-muted)" }}
          >
            In feed · 9:16
          </figcaption>
        </figure>
        <figure className="flex flex-col gap-1">
          <div
            className="size-[160px] overflow-hidden rounded-md"
            style={{ border: "1px solid var(--color-border-subtle)" }}
          >
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              src={assetUrl}
              alt="Reels cover, cropped to the profile grid"
              data-testid="cover-cropped"
              className="size-full object-cover"
            />
          </div>
          <figcaption
            className="text-xs"
            style={{ color: "var(--color-ink-muted)" }}
          >
            On the grid · 1:1
          </figcaption>
        </figure>
        {meta?.hook ? (
          <p className="self-center text-sm">{String(meta.hook)}</p>
        ) : null}
      </section>
    );
  }

  if (!content) {
    return null;
  }

  if (kind === "caption") {
    const hashtags = Array.isArray(content.hashtags) ? content.hashtags : [];
    const caption = String(content.caption ?? "");
    return (
      <div className="flex flex-col gap-3" data-testid="caption-review">
        {/* What a scroller sees before the "more" link. Server-derived
            (`preview`) so the 125-character rule lives in one place. */}
        {content.preview ? (
          <p className="text-xs" style={{ color: "var(--color-ink-muted)" }}>
            Before “more”: {String(content.preview)}
          </p>
        ) : null}
        <p className="whitespace-pre-wrap">{caption}</p>
        {hashtags.length > 0 ? (
          <p className="text-sm" style={{ color: "var(--color-ink-muted)" }}>
            {hashtags.map((tag) => `#${String(tag)}`).join(" ")}
          </p>
        ) : null}
        <p className="text-xs" style={{ color: "var(--color-ink-muted)" }}>
          {caption.length} / 2200 characters · {hashtags.length} hashtags · hook
          “{String(content.hook ?? "")}”
        </p>
      </div>
    );
  }

  if (kind === "script") {
    return (
      <p className="whitespace-pre-wrap">{String(content.script ?? "")}</p>
    );
  }

  if (kind === "research") {
    const facts = Array.isArray(content.key_facts) ? content.key_facts : [];
    return (
      <div className="flex flex-col gap-3">
        <p className="whitespace-pre-wrap">{String(content.summary ?? "")}</p>
        {facts.length > 0 ? (
          <ul className="list-disc pl-5">
            {facts.map((fact, i) => (
              <li key={i}>{String(fact)}</li>
            ))}
          </ul>
        ) : null}
        {content.surprising_angle ? (
          <p>
            <strong>Most surprising:</strong> {String(content.surprising_angle)}
          </p>
        ) : null}
        {content.misconception ? (
          <p>
            <strong>Common misconception:</strong>{" "}
            {String(content.misconception)}
          </p>
        ) : null}
      </div>
    );
  }

  if (kind === "scene_set") {
    const scenes = Array.isArray(content.scenes) ? content.scenes : [];
    const total = scenes.reduce(
      (sum: number, s) =>
        sum + Number((s as Record<string, unknown>).target_duration_ms ?? 0),
      0,
    );
    return (
      <div className="flex flex-col gap-3">
        <p
          data-testid="scene-total"
          className="text-xs"
          style={{ color: "var(--color-ink-muted)" }}
        >
          {scenes.length} scenes · {(total / 1000).toFixed(1)}s total
        </p>
        <ol className="flex flex-col gap-3">
          {scenes.map((raw, i) => {
            const scene = raw as Record<string, unknown>;
            return (
              <li key={i} className="flex flex-col gap-1">
                <span
                  className="text-xs"
                  style={{ color: "var(--color-ink-muted)" }}
                >
                  Scene {i + 1} ·{" "}
                  {(Number(scene.target_duration_ms ?? 0) / 1000).toFixed(1)}s
                </span>
                <span className="whitespace-pre-wrap">
                  {String(scene.narration_text ?? "")}
                </span>
                <span
                  className="text-xs italic"
                  style={{ color: "var(--color-ink-muted)" }}
                >
                  {String(scene.visual_brief ?? "")}
                </span>
              </li>
            );
          })}
        </ol>
      </div>
    );
  }

  if (kind === "prompt") {
    // The manifest the batched fan-out writes onto its own artifact, versus a
    // single scene's prompt. Distinguished by shape rather than by a flag:
    // adding one would mean the writer and the reader could disagree.
    const prompts = Array.isArray(content.prompts) ? content.prompts : null;
    if (prompts) {
      return (
        <p>
          {prompts.length} scene prompts generated. Approve the set here, or
          pick a scene above to review one on its own.
        </p>
      );
    }
    return (
      <div className="flex flex-col gap-2">
        <p className="whitespace-pre-wrap">
          {String(content.prompt_text ?? "")}
        </p>
        {content.negative_prompt ? (
          <p className="text-xs" style={{ color: "var(--color-ink-muted)" }}>
            Negative: {String(content.negative_prompt)}
          </p>
        ) : null}
      </div>
    );
  }

  if (kind === "timeline") {
    return <TimelineSummary content={content} />;
  }

  return (
    <pre className="overflow-x-auto text-xs">
      {JSON.stringify(content, null, 2)}
    </pre>
  );
}

interface TimelineClip {
  scene_index: number;
  kind: string;
  start_ms: number;
  end_ms: number;
}

interface TimelineTransition {
  kind: string;
  from_clip: number;
  duration_ms: number;
}

/** The timeline artifact, as pacing rather than as JSON (M4-08).
 *
 * What a reviewer is actually judging here is **timing** — whether any scene
 * is held too long or flashes past, and whether the whole thing lands near the
 * target duration. Ninety-odd caption cues pretty-printed is technically the
 * same information and answers none of that.
 *
 * The clip windows overlap by each transition's duration (M4-03), so the
 * per-scene figure shown is the window a clip *owns* — its span minus the half
 * of each blend it shares. Showing the raw window would make every scene look
 * longer than it plays.
 */
function TimelineSummary({ content }: { content: Record<string, unknown> }) {
  const clips = (
    Array.isArray(content.clips) ? content.clips : []
  ) as TimelineClip[];
  const transitions = (
    Array.isArray(content.transitions) ? content.transitions : []
  ) as TimelineTransition[];
  const captions = Array.isArray(content.captions) ? content.captions : [];
  const totalMs = Number(content.total_ms ?? 0);
  const cards = clips.filter((clip) => clip.kind === "card").length;

  const owned = (clip: TimelineClip, index: number) => {
    const incoming = index > 0 ? (transitions[index - 1]?.duration_ms ?? 0) : 0;
    const outgoing = transitions[index]?.duration_ms ?? 0;
    return clip.end_ms - clip.start_ms - incoming / 2 - outgoing / 2;
  };

  return (
    <section className="flex flex-col gap-3" data-testid="timeline-summary">
      <p className="text-xs" style={{ color: "var(--color-ink-muted)" }}>
        {(totalMs / 1000).toFixed(1)}s · {clips.length} scenes
        {cards > 0 ? ` (${cards} cards)` : ""} · {captions.length} captions
      </p>

      <ol className="flex flex-col gap-1">
        {clips.map((clip, index) => {
          const transition = transitions[index];
          return (
            <li
              key={clip.scene_index}
              className="flex items-baseline gap-2 text-xs"
            >
              <span
                className="w-6 shrink-0 text-right"
                style={{ color: "var(--color-ink-muted)" }}
              >
                {clip.scene_index}
              </span>
              {/* A bar, so an outlier is visible without reading the numbers —
                  the whole reason this is not a JSON dump. */}
              <span
                aria-hidden
                className="h-2 shrink-0 rounded-sm"
                style={{
                  width: `${Math.max(2, (owned(clip, index) / totalMs) * 100 * 3)}%`,
                  background:
                    clip.kind === "card"
                      ? "var(--color-ink-muted)"
                      : "var(--color-state-ok)",
                }}
              />
              <span style={{ color: "var(--color-ink-muted)" }}>
                {(owned(clip, index) / 1000).toFixed(1)}s
                {transition
                  ? ` · ${transition.kind === "cut" ? "cut" : `${transition.duration_ms}ms fade`}`
                  : ""}
              </span>
            </li>
          );
        })}
      </ol>
    </section>
  );
}

/** Bytes, at the precision a person actually reads.
 *
 * Binary units (1024) rather than decimal, because that is what every file
 * manager showing this same archive will say — a package listed as 12.6 MB
 * here and 12.0 MB in Finder reads as two different files.
 */
function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  const power = Math.min(
    units.length - 1,
    Math.floor(Math.log(bytes) / Math.log(1024)),
  );
  const value = bytes / 1024 ** power;
  return `${power === 0 ? value : value.toFixed(1)} ${units[power]}`;
}
