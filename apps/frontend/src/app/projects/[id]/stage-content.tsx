"use client";

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
}: {
  kind: string;
  content: Record<string, unknown> | null | undefined;
}) {
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
          {prompts.length} scene prompts generated. Select a scene to review
          one.
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

  return (
    <pre className="overflow-x-auto text-xs">
      {JSON.stringify(content, null, 2)}
    </pre>
  );
}
