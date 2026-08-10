"use client";

import { useEffect, useRef, useState } from "react";

/** The finished video, with the timeline's own scene marks (M4-11).
 *
 * **What a reviewer is judging** is the whole thing at once — pacing, sync,
 * whether a caption sits over the wrong picture. So this is a plain `<video>`
 * element rather than anything clever: the browser's own controls are better
 * than a reimplementation, and the file is already `+faststart` so it plays
 * before it has finished arriving.
 *
 * What the browser cannot give is **where the scenes are**. A reviewer who
 * spots a problem at 0:47 has to find which scene that was, and scrubbing a
 * 98-second bar to look for a cut is the ergonomic failure R9 is about, one
 * modality further on than the contact sheet. The marks below come from the
 * timeline artifact — the same offsets the renderer encoded — so the strip
 * and the video cannot disagree.
 *
 * Driven by `timeupdate` deliberately, unlike the narration player: the unit
 * here is a *scene*, which is seconds long, so a quarter-second sampling
 * interval is far finer than the highlight needs. (In the narration player it
 * was not — words are ~280 ms and that clock was visibly behind.)
 */
export interface RenderMark {
  scene_index: number;
  /** When this scene is the only thing on screen — the clip's window minus
   * the halves of the blends it shares with its neighbours. */
  start_ms: number;
  end_ms: number;
  kind: string;
}

export function RenderPlayer({
  videoUrl,
  marks,
  durationMs,
}: {
  videoUrl: string;
  marks: RenderMark[];
  durationMs: number;
}) {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const [positionMs, setPositionMs] = useState(0);

  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;
    const onTime = () => setPositionMs(Math.round(video.currentTime * 1000));
    video.addEventListener("timeupdate", onTime);
    video.addEventListener("seeked", onTime);
    return () => {
      video.removeEventListener("timeupdate", onTime);
      video.removeEventListener("seeked", onTime);
    };
  }, []);

  // Derived from the clock rather than stored: two pieces of state that could
  // disagree would drift the moment a reviewer scrubbed.
  const active = marks.find(
    (mark) => positionMs >= mark.start_ms && positionMs < mark.end_ms,
  );

  const seek = (ms: number) => {
    const video = videoRef.current;
    if (!video) return;
    video.currentTime = ms / 1000;
    setPositionMs(ms);
  };

  return (
    <section className="flex flex-col gap-3" data-testid="render-player">
      {/* No <track>: the captions are burned into the picture by libass, and
          a text track would show every line twice. */}
      <video
        ref={videoRef}
        src={videoUrl}
        controls
        preload="metadata"
        playsInline
        data-testid="render-video"
        className="mx-auto w-full max-w-[320px] rounded-md"
        style={{ background: "var(--color-surface)" }}
      />

      <ol className="flex flex-wrap gap-1" aria-label="Scenes">
        {marks.map((mark) => {
          const isActive = mark.scene_index === active?.scene_index;
          return (
            <li key={mark.scene_index}>
              <button
                type="button"
                onClick={() => seek(mark.start_ms)}
                data-testid={`seek-scene-${mark.scene_index}`}
                aria-current={isActive}
                title={`Scene ${mark.scene_index} · ${(
                  (mark.end_ms - mark.start_ms) /
                  1000
                ).toFixed(1)}s${mark.kind === "card" ? " · card" : ""}`}
                className="rounded px-2 py-1 text-xs"
                style={{
                  border: `1px solid ${
                    isActive
                      ? "var(--color-state-ok)"
                      : "var(--color-border-subtle)"
                  }`,
                  color: isActive
                    ? "var(--color-state-ok)"
                    : "var(--color-ink-muted)",
                  // Cards are marked, because "scene 7 looks wrong" has a very
                  // different answer when scene 7 was never drawn.
                  fontStyle: mark.kind === "card" ? "italic" : undefined,
                }}
              >
                {mark.scene_index}
              </button>
            </li>
          );
        })}
      </ol>

      <p className="text-xs" style={{ color: "var(--color-ink-muted)" }}>
        {(durationMs / 1000).toFixed(1)}s · {marks.length} scenes ·{" "}
        {active ? `scene ${active.scene_index}` : "—"}
      </p>
    </section>
  );
}
