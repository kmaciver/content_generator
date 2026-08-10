"use client";

import { useEffect, useRef, useState } from "react";

/** The narration review player (M3-12).
 *
 * **What a reviewer is actually judging** is whether the narration sounds like
 * one continuous read and whether each scene's words land while the right image
 * is on screen. So this plays the real audio against the real timings and shows
 * the caption exactly as the renderer will: one word at a time, centred, in the
 * caption band — §1.0.2 measured that as the format, and a player that showed a
 * scrolling transcript instead would let a mistimed word through unnoticed.
 *
 * It is **not** the renderer. FFmpeg burns an ASS subtitle track at encode time
 * from these same spans; this is a preview of that, in the browser, before
 * anything is encoded. The two agree because they read one source — the spans
 * stored on the voice artifact — rather than each deriving their own.
 *
 * Driven by `timeupdate` rather than a `requestAnimationFrame` loop: the word
 * granularity here is ~0.4s and the audio element is the clock that matters, so
 * a 60fps loop would burn battery to re-render the same word sixty times.
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
  durationMs,
  frames,
}: {
  audioUrl: string;
  spans: VoiceSpan[];
  durationMs: number;
  /** Scene id → image URL, so the preview shows the frame the word lands on. */
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
  // disagree — "which scene" and "which word" — would drift the moment a
  // reviewer scrubbed.
  const span = spans.find(
    (s) => positionMs >= s.start_ms && positionMs < s.end_ms,
  );
  const word = span?.words.find(
    (w) => positionMs >= w.start_ms && positionMs < w.end_ms,
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
            rather than a box, matching the reference. */}
        {word ? (
          <span
            data-testid="caption-word"
            aria-live="off"
            className="absolute left-1/2 -translate-x-1/2 -translate-y-1/2 text-center font-bold"
            style={{
              top: "57%",
              fontSize: "clamp(18px, 7cqw, 34px)",
              color: "#FFFFFF",
              textShadow:
                "0 0 6px #000, 2px 2px 0 #000, -2px 2px 0 #000, 2px -2px 0 #000, -2px -2px 0 #000",
            }}
          >
            {word.text}
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
