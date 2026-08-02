// Mapping from domain state to the role-named design tokens M0 installed.
//
// The tokens were named by ROLE (`--color-state-review`, not `--color-amber`)
// precisely so this file could exist: the artifact lifecycle's vocabulary maps
// onto them one-to-one, and a theme change never touches this module.

import type { VersionStatus } from "./api";

const ARTIFACT_STATE_TOKENS: Record<string, string> = {
  PENDING: "var(--color-state-pending)",
  GENERATING: "var(--color-state-generating)",
  AWAITING_APPROVAL: "var(--color-state-review)",
  APPROVED: "var(--color-state-ok)",
  REJECTED: "var(--color-state-failed)",
  FAILED: "var(--color-state-failed)",
};

const VERSION_STATUS_TOKENS: Record<VersionStatus, string> = {
  AWAITING_APPROVAL: "var(--color-state-review)",
  APPROVED: "var(--color-state-ok)",
  REJECTED: "var(--color-state-failed)",
  // Superseded is deliberately muted rather than coloured: it is not a
  // problem, it is just history, and giving it a status colour would draw the
  // eye to the versions that matter least.
  SUPERSEDED: "var(--color-ink-muted)",
};

export function artifactStateColor(state: string): string {
  return ARTIFACT_STATE_TOKENS[state] ?? "var(--color-ink-muted)";
}

export function versionStatusColor(status: VersionStatus): string {
  return VERSION_STATUS_TOKENS[status] ?? "var(--color-ink-muted)";
}

/** Human-facing label. The wire format is SCREAMING_CASE; the UI is not. */
export function humanise(value: string): string {
  return value
    .toLowerCase()
    .split("_")
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(" ");
}
