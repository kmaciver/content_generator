"use client";

import type { VersionSummary } from "@/lib/api";
import { humanise, versionStatusColor } from "@/lib/state-colors";

/**
 * The version switcher (SADD §17).
 *
 * Rejected versions stay queryable forever (§10.3 rule 2), so this list is
 * history rather than a picker of "current" things — which is why every entry
 * shows its own derived status and why superseded ones are muted rather than
 * hidden. Hiding them would make the lineage unexplainable at exactly the
 * moment someone asks why a video looks the way it does.
 */
export function VersionSwitcher({
  versions,
  selected,
  onSelect,
}: {
  versions: VersionSummary[];
  selected: number;
  onSelect: (versionNo: number) => void;
}) {
  if (versions.length <= 1) return null;

  return (
    <nav aria-label="Versions" className="flex flex-wrap gap-2">
      {versions.map((version) => {
        const isSelected = version.version_no === selected;
        return (
          <button
            key={version.id}
            type="button"
            onClick={() => onSelect(version.version_no)}
            aria-current={isSelected ? "true" : undefined}
            className="rounded-md px-3 py-1.5 text-xs font-medium"
            style={{
              background: isSelected
                ? "var(--color-surface-raised)"
                : "transparent",
              border: `1px solid ${
                isSelected
                  ? versionStatusColor(version.status)
                  : "var(--color-border-subtle)"
              }`,
              color: isSelected ? "var(--color-ink)" : "var(--color-ink-muted)",
            }}
          >
            v{version.version_no}
            <span
              className="ml-2"
              style={{ color: versionStatusColor(version.status) }}
            >
              {humanise(version.status)}
            </span>
            {version.origin === "human_edit" ? (
              // The one place origin is surfaced: mechanically a human edit is
              // identical to a generation, and the audit distinction is the
              // only reason it exists (§10.3 rule 3).
              <span
                className="ml-2"
                style={{ color: "var(--color-ink-muted)" }}
              >
                edited
              </span>
            ) : null}
          </button>
        );
      })}
    </nav>
  );
}
