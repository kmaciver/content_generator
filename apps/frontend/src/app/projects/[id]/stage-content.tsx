"use client";

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
}: {
  kind: string;
  content: Record<string, unknown> | null | undefined;
  /** Media stages carry their reviewable substance in `meta`, not `content` —
   * the version's content column holds a storage key, not JSON. */
  meta?: Record<string, unknown>;
  assetUrl?: string | null;
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

  if (!content) {
    return null;
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
