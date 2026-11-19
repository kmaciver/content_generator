"use client";

import { Fragment, useRef, useState } from "react";

/** Voice direction (ADR-019): emotion tags as positions over the narration.
 *
 * The words are the approved scene set's and are never editable here — a tag
 * is placed *before* a word (or after the last one) and has no end, because
 * ElevenLabs v3 has no closing tag. Selecting text attaches a tag to the start
 * of the selection; clicking a word does the same for that word.
 *
 * The tag list mirrors `videoforge_domain.voice_direction.AUDIO_TAGS`. The
 * server is the authority and refuses (422) anything else.
 */

export const AUDIO_TAGS = [
  "excited",
  "curious",
  "serious",
  "sad",
  "sarcastic",
  "whispers",
  "shouts",
  "laughs",
  "sighs",
  "short pause",
] as const;

export const MAX_TAGS_PER_SCENE = 12;

export type DirectionTag = { word: number; tag: string };

export type DirectionScene = {
  scene_id: string;
  scene_index: number;
  narration: string;
  tags: DirectionTag[];
};

export function directionScenes(content: unknown): DirectionScene[] {
  const raw = (content as { scenes?: unknown } | null)?.scenes;
  if (!Array.isArray(raw)) return [];
  return raw.map((entry) => {
    const scene = entry as Record<string, unknown>;
    return {
      scene_id: String(scene.scene_id ?? ""),
      scene_index: Number(scene.scene_index ?? 0),
      narration: String(scene.narration ?? ""),
      tags: Array.isArray(scene.tags)
        ? (scene.tags as Record<string, unknown>[]).map((t) => ({
            word: Number(t.word ?? 0),
            tag: String(t.tag ?? ""),
          }))
        : [],
    };
  });
}

function words(narration: string): string[] {
  return narration.split(/\s+/).filter(Boolean);
}

function tagsAt(tags: DirectionTag[], word: number): DirectionTag[] {
  return tags.filter((t) => t.word === word);
}

function Chip({ tag, onRemove }: { tag: string; onRemove?: () => void }) {
  return (
    <span
      className="mx-0.5 inline-flex items-center gap-1 rounded-full px-2 py-px align-baseline text-xs"
      style={{
        background: "var(--color-surface-sunken, transparent)",
        border: "1px solid var(--color-state-review)",
        color: "var(--color-state-review)",
      }}
      data-testid="direction-tag"
    >
      {tag}
      {onRemove ? (
        <button
          type="button"
          onClick={onRemove}
          aria-label={`Remove ${tag}`}
          className="leading-none"
        >
          ×
        </button>
      ) : null}
    </span>
  );
}

/** Read-only: the narration with its tags inline, as the reviewer will hear it. */
export function VoiceDirectionView({ content }: { content: unknown }) {
  const scenes = directionScenes(content);
  const total = scenes.reduce((sum, s) => sum + s.tags.length, 0);
  return (
    <div className="flex flex-col gap-3" data-testid="voice-direction-review">
      <p className="text-xs" style={{ color: "var(--color-ink-muted)" }}>
        {total} {total === 1 ? "tag" : "tags"} across {scenes.length} scenes ·
        performed by ElevenLabs v3, never shown in captions
      </p>
      <ol className="flex flex-col gap-3">
        {scenes.map((scene) => {
          const list = words(scene.narration);
          return (
            <li key={scene.scene_id} className="flex flex-col gap-1">
              <span
                className="text-xs"
                style={{ color: "var(--color-ink-muted)" }}
              >
                Scene {scene.scene_index}
              </span>
              <p className="leading-loose">
                {[...list, null].map((word, i) => (
                  <Fragment key={i}>
                    {tagsAt(scene.tags, i).map((t, j) => (
                      <Chip key={`${t.tag}-${j}`} tag={t.tag} />
                    ))}
                    {word !== null ? <>{word} </> : null}
                  </Fragment>
                ))}
              </p>
            </li>
          );
        })}
      </ol>
    </div>
  );
}

export function VoiceDirectionEditor({
  scenes,
  onChange,
}: {
  scenes: DirectionScene[];
  onChange: (scenes: DirectionScene[]) => void;
}) {
  // Where the next tag goes: a scene and the word it precedes.
  const [target, setTarget] = useState<{ scene: string; word: number } | null>(
    null,
  );

  const update = (sceneId: string, tags: DirectionTag[]) =>
    onChange(scenes.map((s) => (s.scene_id === sceneId ? { ...s, tags } : s)));

  return (
    <div className="flex flex-col gap-4" data-testid="voice-direction-editor">
      <p className="text-xs" style={{ color: "var(--color-ink-muted)" }}>
        Select some words, or click one, then pick an emotion. A tag affects the
        words after it, roughly to the end of the sentence. The words themselves
        come from the approved scenes and cannot be changed here.
      </p>
      {scenes.map((scene) => (
        <SceneEditor
          key={scene.scene_id}
          scene={scene}
          target={target?.scene === scene.scene_id ? target.word : null}
          onTarget={(word) =>
            setTarget(word === null ? null : { scene: scene.scene_id, word })
          }
          onTags={(tags) => update(scene.scene_id, tags)}
        />
      ))}
    </div>
  );
}

function SceneEditor({
  scene,
  target,
  onTarget,
  onTags,
}: {
  scene: DirectionScene;
  target: number | null;
  onTarget: (word: number | null) => void;
  onTags: (tags: DirectionTag[]) => void;
}) {
  const container = useRef<HTMLParagraphElement>(null);
  const list = words(scene.narration);
  const full = scene.tags.length >= MAX_TAGS_PER_SCENE;

  /** A drag-selection attaches to its first word. */
  const onMouseUp = () => {
    const selection = window.getSelection();
    if (!selection || selection.isCollapsed || !container.current) return;
    const range = selection.getRangeAt(0);
    const spans =
      container.current.querySelectorAll<HTMLElement>("[data-word]");
    for (const span of spans) {
      if (range.intersectsNode(span)) {
        onTarget(Number(span.dataset.word));
        selection.removeAllRanges();
        return;
      }
    }
  };

  const add = (tag: string) => {
    if (target === null) return;
    const exists = scene.tags.some((t) => t.word === target && t.tag === tag);
    if (!exists) {
      onTags(
        [...scene.tags, { word: target, tag }].sort((a, b) => a.word - b.word),
      );
    }
    onTarget(null);
  };

  const remove = (index: number) =>
    onTags(scene.tags.filter((_, i) => i !== index));

  return (
    <div className="flex flex-col gap-2">
      <span className="text-xs" style={{ color: "var(--color-ink-muted)" }}>
        Scene {scene.scene_index}
        {scene.tags.length > 0 ? ` · ${scene.tags.length} tags` : ""}
      </span>
      <p
        ref={container}
        onMouseUp={onMouseUp}
        className="cursor-text leading-loose"
      >
        {[...list, null].map((word, i) => (
          <Fragment key={i}>
            {scene.tags.map((t, j) =>
              t.word === i ? (
                <Chip
                  key={`${t.tag}-${j}`}
                  tag={t.tag}
                  onRemove={() => remove(j)}
                />
              ) : null,
            )}
            {word !== null ? (
              <>
                <span
                  data-word={i}
                  onClick={() => onTarget(target === i ? null : i)}
                  className="rounded px-px"
                  style={
                    target === i
                      ? {
                          outline: "1px solid var(--color-state-review)",
                          outlineOffset: "1px",
                        }
                      : undefined
                  }
                >
                  {word}
                </span>{" "}
              </>
            ) : (
              <button
                type="button"
                onClick={() => onTarget(target === i ? null : i)}
                className="rounded px-1 text-xs"
                aria-label="Add a tag after the last word"
                style={{
                  color: "var(--color-ink-muted)",
                  outline:
                    target === i
                      ? "1px solid var(--color-state-review)"
                      : undefined,
                }}
              >
                + end
              </button>
            )}
          </Fragment>
        ))}
      </p>
      {target !== null ? (
        <div
          className="flex flex-wrap items-center gap-2"
          data-testid="tag-picker"
        >
          <span className="text-xs" style={{ color: "var(--color-ink-muted)" }}>
            {target < list.length ? `Before “${list[target]}”:` : "At the end:"}
          </span>
          {AUDIO_TAGS.map((tag) => (
            <button
              key={tag}
              type="button"
              disabled={full}
              onClick={() => add(tag)}
              className="rounded-full px-3 py-1 text-xs disabled:opacity-40"
              style={{
                border: "1px solid var(--color-border-subtle)",
                color: "var(--color-ink)",
              }}
            >
              {tag}
            </button>
          ))}
          <button
            type="button"
            onClick={() => onTarget(null)}
            className="text-xs"
            style={{ color: "var(--color-ink-muted)" }}
          >
            Cancel
          </button>
          {full ? (
            <span
              className="text-xs"
              style={{ color: "var(--color-state-failed)" }}
            >
              At most {MAX_TAGS_PER_SCENE} tags in a scene.
            </span>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
