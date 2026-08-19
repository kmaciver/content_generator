"use client";

import { useEffect, useRef, useState } from "react";

import type { CaptionCue } from "@/lib/api";

/** The narration review player (M3-12, re-captioned in M4).
 *
 * **What a reviewer is actually judging** is whether the narration sounds like
 * one continuous read and whether each scene's words land while the right image
 * is on screen. So this plays the real audio against the real timings and shows
 * the caption exactly as the renderer will: centred, in the caption band
 * §1.0.2 measured. A player that showed a scrolling transcript instead would
 * let a mistimed caption through unnoticed.
 *
 * It is **not** the renderer. FFmpeg burns an ASS subtitle track at encode
 * time; this is a preview of that, in the browser, before anything is encoded.
 *
 * **Cues, not words, and they come from the server.** This player originally
 * showed one word at a time from the raw spans, which was right when M3-12 was
 * written and became wrong the moment M4-04 grouped captions into phrases: the
 * preview and the finished video then disagreed about where a caption starts.
 * They agree again because both read one implementation — `group_into_cues` in
 * `videoforge_domain.captions`, called by the timeline compiler and by the
 * version endpoint. Regrouping here in TypeScript would have restored the
 * agreement and reintroduced the cause.
 *
 * Spans are still passed, and still carry their words: they are what the scene
 * strip and the frame preview are keyed on, and they are the *measurement* the
 * cues are grouped from.
 *
 * Driven by `timeupdate` rather than a `requestAnimationFrame` loop: cue
 * granularity is most of a second and the audio element is the clock that
 * matters, so a 60fps loop would burn battery re-rendering the same phrase.
 */
export interface VoiceWord {
  text: string;
  start_ms: number;
  end_ms: number;
}

export interface VoiceSpan {
  scene_index: number;
  scene_id: string;
  start_ms: number;
  end_ms: number;
  words: VoiceWord[];
}

export function NarrationPlayer({
  audioUrl,
  spans,
  cues,
  durationMs,
  frames,
}: {
  audioUrl: string;
  spans: VoiceSpan[];
  /** Server-grouped captions. Empty is a real answer — an older voice version
   * whose stored spans predate the grouping still plays, with no caption,
   * rather than falling back to per-word text the render would not burn. */
  cues: CaptionCue[];
  durationMs: number;
  /** Scene id → image URL, so the preview shows the frame the cue lands on. */
  frames?: Record<string, string>;
}) {
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const [positionMs, setPositionMs] = useState(0);

  useEffect(() => {
    const audio = audioRef.current;
    if (!audio) return;
    const onTime = () => setPositionMs(Math.round(audio.currentTime * 1000));
    audio.addEventListener("timeupdate", onTime);
    audio.addEventListener("seeked", onTime);
    return () => {
      audio.removeEventListener("timeupdate", onTime);
      audio.removeEventListener("seeked", onTime);
    };
  }, []);

  // Derived from the clock, not stored. Two pieces of state that could
  // disagree — "which scene" and "which caption" — would drift the moment a
  // reviewer scrubbed.
  const span = spans.find(
    (s) => positionMs >= s.start_ms && positionMs < s.end_ms,
  );
  // Searched flat rather than within the span. Cues are grouped per scene on
  // the server, so one cannot straddle a boundary and the two searches give
  // the same answer — but the flat one does not go blank when the caption is
  // right and the span lookup happens to miss.
  const cue = cues.find(
    (c) => positionMs >= c.start_ms && positionMs < c.end_ms,
  );
  const frame = span && frames ? frames[span.scene_id] : undefined;

  const seek = (ms: number) => {
    const audio = audioRef.current;
    if (!audio) return;
    audio.currentTime = ms / 1000;
    setPositionMs(ms);
  };

  return (
    <section className="flex flex-col gap-3" data-testid="narration-player">
      <div
        className="relative mx-auto flex aspect-[9/16] w-full max-w-[280px] items-center justify-center overflow-hidden rounded-md"
        style={{
          background: "var(--color-surface)",
          border: "1px solid var(--color-border-subtle)",
          // So the caption below can size in `cqw` against *this* box rather
          // than the viewport. Without it the preview's caption scales with
          // the browser window, which is not what the render does.
          containerType: "inline-size",
        }}
      >
        {frame ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img
            src={frame}
            alt={`Scene ${span?.scene_index}`}
            className="size-full object-cover"
          />
        ) : (
          <span className="text-xs" style={{ color: "var(--color-ink-muted)" }}>
            {span ? `Scene ${span.scene_index}` : "—"}
          </span>
        )}

        {/* 57% down the frame, centred: the caption band §1.0.2 measured, and
            the same position `ass_document` writes into \pos(). Heavy outline
            rather than a box, matching the reference. `balance` so a two-line
            cue splits evenly, which is what libass's WrapStyle 0 does. */}
        {cue ? (
          <span
            data-testid="caption-cue"
            aria-live="off"
            className="absolute left-1/2 -translate-x-1/2 -translate-y-1/2 text-center font-bold"
            style={{
              top: "57%",
              // 85% of the frame: the ASS style's 80px margins at PlayResX
              // 1080, so a cue that wraps here wraps in the render too.
              maxWidth: "85%",
              textWrap: "balance",
              // `_FONT_SIZE_RATIO` exactly: 6.67% of frame width is the 72px
              // the ASS writer uses at 1080, so a cue that fits on one line
              // here fits on one line in the burn.
              fontSize: "6.67cqw",
              lineHeight: 1.15,
              color: "#FFFFFF",
              textShadow:
                "0 0 6px #000, 2px 2px 0 #000, -2px 2px 0 #000, 2px -2px 0 #000, -2px -2px 0 #000",
            }}
          >
            {cue.text}
          </span>
        ) : null}
      </div>

      <audio
        ref={audioRef}
        src={audioUrl}
        controls
        preload="metadata"
        className="w-full"
        data-testid="narration-audio"
      />

      {/* Scene strip: click to jump. A reviewer who hears a problem needs to
          get back to it, and scrubbing a 60-second waveform to find scene 14
          is the ergonomic failure R9 is about, one modality over. */}
      <ol className="flex flex-wrap gap-1">
        {spans.map((s) => {
          const active = s.scene_index === span?.scene_index;
          return (
            <li key={s.scene_id}>
              <button
                type="button"
                onClick={() => seek(s.start_ms)}
                data-testid={`seek-${s.scene_index}`}
                aria-current={active}
                className="rounded px-2 py-1 text-xs"
                style={{
                  border: `1px solid ${
                    active
                      ? "var(--color-state-ok)"
                      : "var(--color-border-subtle)"
                  }`,
                  color: active
                    ? "var(--color-state-ok)"
                    : "var(--color-ink-muted)",
                }}
              >
                {s.scene_index}
              </button>
            </li>
          );
        })}
      </ol>

      <p className="text-xs" style={{ color: "var(--color-ink-muted)" }}>
        {(durationMs / 1000).toFixed(1)}s · {spans.length} scenes ·{" "}
        {spans.reduce((n, s) => n + s.words.length, 0)} words
      </p>
    </section>
  );
}
