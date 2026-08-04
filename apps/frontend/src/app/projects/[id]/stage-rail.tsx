"use client";

import type { StageSummary } from "@/lib/api";
import { artifactStateColor, humanise } from "@/lib/state-colors";

/** The pipeline, as a rail (M2-13).
 *
 * Every stage is listed, including ones that cannot run yet — a pipeline whose
 * later steps are invisible until they become available gives a reviewer no
 * idea what they are working towards.
 *
 * A disabled Generate button always says *why*. "Waiting on: script" is an
 * answer; a greyed-out control is a puzzle, and the server already computed the
 * reason (`unmet`), so withholding it would be a deliberate choice to be less
 * helpful.
 */
export function StageRail({
  stages,
  selected,
  onSelect,
  onGenerate,
  busy,
}: {
  stages: StageSummary[];
  selected: string;
  onSelect: (kind: string) => void;
  onGenerate: (kind: string, regenerate: boolean) => void;
  busy: boolean;
}) {
  return (
    <nav aria-label="Pipeline" className="flex flex-col gap-2">
      {stages.map((stage) => {
        const isSelected = stage.kind === selected;
        const reviewable = stage.artifact_id !== null;
        return (
          <div
            key={stage.kind}
            className="flex items-center gap-3 rounded-md px-3 py-2"
            style={{
              background: isSelected
                ? "var(--color-surface-raised)"
                : "transparent",
              border: `1px solid ${
                isSelected ? "var(--color-border)" : "transparent"
              }`,
            }}
          >
            <button
              type="button"
              onClick={() => onSelect(stage.kind)}
              disabled={!reviewable}
              className="flex-1 text-left text-sm disabled:opacity-40"
              aria-current={isSelected ? "true" : undefined}
            >
              <span className="font-medium">{humanise(stage.kind)}</span>
              {stage.state ? (
                <span
                  data-testid={`stage-state-${stage.kind}`}
                  className="ml-2 text-xs"
                  style={{ color: artifactStateColor(stage.state) }}
                >
                  {humanise(stage.state)}
                </span>
              ) : (
                <span
                  className="ml-2 text-xs"
                  style={{ color: "var(--color-ink-muted)" }}
                >
                  Not started
                </span>
              )}
              {stage.stale_since ? (
                // *When*, not just "stale" — which is why finding S2 made this
                // a nullable timestamp rather than a boolean.
                <span
                  data-testid={`stage-stale-${stage.kind}`}
                  className="ml-2 text-xs"
                  style={{ color: "var(--color-state-rejected)" }}
                  title={`Inputs changed at ${stage.stale_since}`}
                >
                  Stale since {new Date(stage.stale_since).toLocaleString()}
                </span>
              ) : null}
            </button>

            {stage.unmet.length > 0 ? (
              <span
                className="text-xs"
                style={{ color: "var(--color-ink-muted)" }}
              >
                Waiting on: {stage.unmet.map(humanise).join(", ")}
              </span>
            ) : (
              <button
                type="button"
                onClick={() => onGenerate(stage.kind, reviewable)}
                disabled={!stage.can_generate || busy}
                className="rounded-md px-3 py-1 text-xs font-medium disabled:opacity-40"
                style={{
                  background: "var(--color-state-generating)",
                  color: "var(--color-surface)",
                }}
              >
                {reviewable ? "Regenerate" : "Generate"} {humanise(stage.kind)}
              </button>
            )}
          </div>
        );
      })}
    </nav>
  );
}
