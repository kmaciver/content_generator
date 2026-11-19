"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "@/lib/api";

/** "Approve all remaining" for any per-scene stage (prompts, images).
 *
 * The image contact sheet already has this button; prompts had none, so
 * approving a prompt set meant opening every scene. This reuses the same
 * server pieces rather than adding new ones: the contact-sheet endpoint says
 * which versions may be approved (the FSM's answer, not a TypeScript copy of
 * it), and `approve-remaining` approves exactly those ids — so a scene that
 * regenerated while the reviewer was looking is skipped and reported, never
 * swept up.
 *
 * Shares the contact sheet's query key, so either one's approvals refresh the
 * other.
 */
export function ApproveAllBar({
  projectId,
  kind,
  stateKey,
  onDone,
}: {
  projectId: string;
  kind: string;
  /** The per-scene artifacts' states, joined. Part of the query key so the
   * count refetches when the project poll sees a scene finish generating —
   * nothing else would tell this query the set had changed. */
  stateKey: string;
  /** Called after a batch lands, so the review panel refetches too. */
  onDone: () => void;
}) {
  const queryClient = useQueryClient();
  const [note, setNote] = useState<string | null>(null);

  const sheet = useQuery({
    queryKey: ["contact-sheet", projectId, kind, stateKey],
    queryFn: () => api.getContactSheet(projectId, kind),
  });

  const approveAll = useMutation({
    mutationFn: (ids: string[]) => api.approveRemaining(projectId, ids),
    onSuccess: (result) => {
      setNote(
        result.skipped.length === 0
          ? `Approved ${result.approved}.`
          : `Approved ${result.approved}. ${result.skipped.length} skipped — they changed while you were reviewing.`,
      );
      void queryClient.invalidateQueries({ queryKey: ["contact-sheet"] });
      onDone();
    },
  });

  const pendingIds = sheet.data?.pending_version_ids ?? [];
  const pendingScenes = sheet.data?.pending ?? 0;
  if (!sheet.data || (pendingIds.length === 0 && note === null)) {
    return null;
  }

  return (
    <div
      className="flex flex-wrap items-center gap-3"
      data-testid="approve-all-bar"
    >
      {pendingIds.length > 0 ? (
        <button
          type="button"
          data-testid="approve-all-scenes"
          disabled={approveAll.isPending}
          onClick={() => {
            setNote(null);
            approveAll.mutate(pendingIds);
          }}
          className="rounded-md px-4 py-2 text-sm font-medium disabled:opacity-30"
          style={{
            border: "1px solid var(--color-state-ok)",
            color: "var(--color-state-ok)",
          }}
        >
          {approveAll.isPending
            ? "Approving…"
            : // The batch can also carry the set-level row, which is not a
              // scene — so the count shown is scenes, and "the set" when only
              // that row is left.
              pendingScenes > 0
              ? `Approve all remaining (${pendingScenes} of ${sheet.data.total})`
              : "Approve the set"}
        </button>
      ) : null}
      {note ? (
        <span
          aria-live="polite"
          className="text-sm"
          style={{ color: "var(--color-ink-muted)" }}
        >
          {note}
        </span>
      ) : null}
      {approveAll.error ? (
        <span
          className="text-sm"
          style={{ color: "var(--color-state-failed)" }}
        >
          {approveAll.error.message}
        </span>
      ) : null}
    </div>
  );
}
