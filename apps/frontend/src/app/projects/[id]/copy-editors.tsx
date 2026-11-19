"use client";

/** Editors for the two published-copy stages: the caption and the cover.
 *
 * The caption is three fields, not one textarea, because a reviewer comes to
 * change any of the three — and a single textarea over `caption` silently
 * dropped the hook and hashtags. Limits mirror
 * `videoforge_domain.post_copy`; the server is the authority and refuses (422)
 * rather than rewrites, so what is saved is exactly what was typed here.
 */

export const MAX_CAPTION_CHARACTERS = 2200;
/** Cover text: 40 reads well at profile-grid size; past it the renderer
 * shrinks the font, so it is a warning. 100 is the hard ceiling. */
export const RECOMMENDED_HOOK_CHARACTERS = 40;
export const MAX_HOOK_CHARACTERS = 100;
export const MAX_HASHTAGS = 30;

import type { DirectionScene } from "./voice-direction";

export type Draft =
  | { type: "script"; text: string }
  | { type: "voice_direction"; scenes: DirectionScene[] }
  | { type: "caption"; hook: string; caption: string; hashtags: string }
  | { type: "thumbnail"; text: string };

/** "#one two, #three" → ["one", "two", "three"]. Splits on whitespace and
 * commas; the leading `#` is optional and stored bare (the packager adds it). */
export function parseHashtags(input: string): string[] {
  return input
    .split(/[\s,]+/)
    .map((tag) => tag.replace(/^#+/, "").trim())
    .filter(Boolean);
}

export function formatHashtags(tags: unknown): string {
  return Array.isArray(tags) ? tags.map((t) => `#${String(t)}`).join(" ") : "";
}

/** Letters (any script), digits and underscore — anything else ends a tag. */
const BAD_TAG = /[^\p{L}\p{N}_]/u;

export function invalidHashtags(input: string): string[] {
  return parseHashtags(input).filter((tag) => BAD_TAG.test(tag));
}

const fieldStyle = {
  background: "transparent",
  border: "1px solid var(--color-border-subtle)",
  color: "var(--color-ink)",
} as const;

function Counter({
  value,
  max,
  recommended,
}: {
  value: number;
  max: number;
  /** Soft limit: over it the count turns amber, over `max` red. */
  recommended?: number;
}) {
  const soft = recommended ?? max;
  return (
    <span
      className="text-xs"
      style={{
        color:
          value > max
            ? "var(--color-state-failed)"
            : value > soft
              ? "var(--color-state-review)"
              : "var(--color-ink-muted)",
      }}
    >
      {value} / {soft}
      {recommended !== undefined ? ` recommended · max ${max}` : null}
    </span>
  );
}

function HookLengthNote({ length }: { length: number }) {
  if (length > MAX_HOOK_CHARACTERS) {
    return (
      <span className="text-xs" style={{ color: "var(--color-state-failed)" }}>
        Too long for the cover — {MAX_HOOK_CHARACTERS} characters at most.
      </span>
    );
  }
  if (length > RECOMMENDED_HOOK_CHARACTERS) {
    return (
      <span className="text-xs" style={{ color: "var(--color-state-review)" }}>
        Over {RECOMMENDED_HOOK_CHARACTERS} characters the text is set smaller
        and may be hard to read on the profile grid.
      </span>
    );
  }
  return null;
}

function HookCounter({ length }: { length: number }) {
  return (
    <Counter
      value={length}
      max={MAX_HOOK_CHARACTERS}
      recommended={RECOMMENDED_HOOK_CHARACTERS}
    />
  );
}

function Label({ children }: { children: React.ReactNode }) {
  return (
    <span
      className="text-xs font-medium"
      style={{ color: "var(--color-ink-muted)" }}
    >
      {children}
    </span>
  );
}

export function CaptionEditor({
  draft,
  onChange,
}: {
  draft: Extract<Draft, { type: "caption" }>;
  onChange: (draft: Draft) => void;
}) {
  const tags = parseHashtags(draft.hashtags);
  const bad = invalidHashtags(draft.hashtags);
  return (
    <div className="flex flex-col gap-4" data-testid="caption-editor">
      <label className="flex flex-col gap-1">
        <span className="flex items-center justify-between">
          <Label>Cover hook (the text on the thumbnail)</Label>
          <HookCounter length={draft.hook.trim().length} />
        </span>
        <input
          value={draft.hook}
          onChange={(e) => onChange({ ...draft, hook: e.target.value })}
          aria-label="Hook"
          className="rounded px-2 py-1"
          style={fieldStyle}
        />
        <HookLengthNote length={draft.hook.trim().length} />
      </label>
      <label className="flex flex-col gap-1">
        <span className="flex items-center justify-between">
          <Label>Caption</Label>
          <Counter
            value={draft.caption.trim().length}
            max={MAX_CAPTION_CHARACTERS}
          />
        </span>
        <textarea
          value={draft.caption}
          onChange={(e) => onChange({ ...draft, caption: e.target.value })}
          rows={10}
          aria-label="Caption"
          className="w-full resize-y rounded px-2 py-1"
          style={fieldStyle}
        />
      </label>
      <label className="flex flex-col gap-1">
        <span className="flex items-center justify-between">
          <Label>Hashtags (separate with spaces or commas)</Label>
          <Counter value={tags.length} max={MAX_HASHTAGS} />
        </span>
        <textarea
          value={draft.hashtags}
          onChange={(e) => onChange({ ...draft, hashtags: e.target.value })}
          rows={2}
          aria-label="Hashtags"
          className="w-full resize-y rounded px-2 py-1"
          style={fieldStyle}
        />
        {bad.length > 0 ? (
          <span
            className="text-xs"
            style={{ color: "var(--color-state-failed)" }}
          >
            Only letters, digits and underscores:{" "}
            {bad.map((t) => `#${t}`).join(" ")}
          </span>
        ) : null}
      </label>
    </div>
  );
}

export function captionDraftProblem(
  draft: Extract<Draft, { type: "caption" }>,
): string | null {
  if (!draft.caption.trim()) return "The caption cannot be empty.";
  if (draft.caption.trim().length > MAX_CAPTION_CHARACTERS)
    return "The caption is over Instagram's limit.";
  if (draft.hook.trim().length > MAX_HOOK_CHARACTERS)
    return "The hook is too long for the cover.";
  if (invalidHashtags(draft.hashtags).length > 0)
    return "Fix the highlighted hashtags.";
  if (parseHashtags(draft.hashtags).length > MAX_HASHTAGS)
    return "Too many hashtags.";
  return null;
}

export function coverDraftProblem(
  draft: Extract<Draft, { type: "thumbnail" }>,
): string | null {
  return draft.text.trim().length > MAX_HOOK_CHARACTERS
    ? "The text is too long for the cover."
    : null;
}

export function CoverTextEditor({
  draft,
  onChange,
}: {
  draft: Extract<Draft, { type: "thumbnail" }>;
  onChange: (draft: Draft) => void;
}) {
  return (
    <label className="flex flex-col gap-1" data-testid="cover-text-editor">
      <span className="flex items-center justify-between">
        <Label>Text on the cover (leave empty for the picture alone)</Label>
        <HookCounter length={draft.text.trim().length} />
      </span>
      <input
        value={draft.text}
        onChange={(e) => onChange({ ...draft, text: e.target.value })}
        aria-label="Cover text"
        className="rounded px-2 py-1"
        style={fieldStyle}
      />
      <HookLengthNote length={draft.text.trim().length} />
      <span className="text-xs" style={{ color: "var(--color-ink-muted)" }}>
        Re-renders the cover with this text as a new version for review. The
        caption is not changed.
      </span>
    </label>
  );
}
