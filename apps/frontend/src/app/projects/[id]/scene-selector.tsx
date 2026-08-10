"use client";

import type { ArtifactSummary, SceneSummary } from "@/lib/api";
import { artifactStateColor, humanise } from "@/lib/state-colors";

/** Choosing which scene of a per-scene stage to review (M2-13).
 *
 * Prompts, and later images, produce one artifact *per scene* (§13, and finding
 * S1's `UNIQUE (project_id, kind, scene_ref)` is what keeps twenty of them
 * unambiguous). Without this the stage's manifest said "select a scene to
 * review one" and there was nothing to select with — the twenty artifacts
 * existed and the UI could not reach any of them.
 *
 * "Set" comes first and is the default. §13 batches this stage precisely
 * because the unit a human reviews is the whole set; per-scene review is for
 * the one that came out wrong, not the normal path.
 *
 * Each scene shows its own state, so a reviewer can see which of twenty needs
 * attention without opening them one at a time — the thing that makes a
 * twenty-item review survivable (risk R9).
 */
export function SceneSelector({
  scenes,
  artifacts,
  kind,
  selected,
  onSelect,
}: {
  scenes: SceneSummary[];
  artifacts: ArtifactSummary[];
  kind: string;
  /** `null` means the set-level artifact. */
  selected: string | null;
  onSelect: (sceneRef: string | null) => void;
}) {
  const perScene = artifacts.filter(
    (a) => a.kind === kind && a.scene_ref !== null,
  );
  if (perScene.length === 0) {
    return null;
  }

  const byScene = new Map(perScene.map((a) => [a.scene_ref, a]));

  return (
    <nav
      aria-label="Scenes"
      data-testid="scene-selector"
      className="flex flex-wrap gap-2"
    >
      <Chip
        label="Set"
        title="Review the whole set"
        active={selected === null}
        onClick={() => onSelect(null)}
      />
      {scenes.map((scene) => {
        const artifact = byScene.get(scene.id);
        // M4-01. A card scene has no per-scene artifact, so it was already
        // disabled — but disabled-and-unexplained reads as "this one failed".
        // Saying "Card" is the difference between a gap and a decision.
        const card = scene.kind === "card";
        return (
          <Chip
            key={scene.id}
            label={`${scene.index}`}
            // The narration is what a reviewer recognises; "Scene 4" alone is
            // a number they have to go and look up.
            title={card && scene.card_text ? scene.card_text : scene.narration}
            active={selected === scene.id}
            tone={artifact ? artifactStateColor(artifact.state) : undefined}
            state={
              card
                ? "Card — rendered locally"
                : artifact
                  ? humanise(artifact.state)
                  : undefined
            }
            onClick={() => onSelect(scene.id)}
            disabled={!artifact}
          />
        );
      })}
    </nav>
  );
}

function Chip({
  label,
  title,
  active,
  tone,
  state,
  onClick,
  disabled,
}: {
  label: string;
  title: string;
  active: boolean;
  tone?: string;
  state?: string;
  onClick: () => void;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      title={state ? `${title} — ${state}` : title}
      aria-current={active ? "true" : undefined}
      className="flex items-center gap-1.5 rounded-md px-2.5 py-1 text-xs disabled:opacity-30"
      style={{
        background: active ? "var(--color-surface-raised)" : "transparent",
        border: `1px solid ${active ? "var(--color-border)" : "var(--color-border-subtle)"}`,
      }}
    >
      {tone ? (
        <span
          aria-hidden
          className="inline-block size-1.5 rounded-full"
          style={{ background: tone }}
        />
      ) : null}
      {label}
    </button>
  );
}
