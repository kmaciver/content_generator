"use client";

import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, type ContactTile } from "@/lib/api";
import { artifactStateColor, humanise } from "@/lib/state-colors";

/** The contact sheet (M3-09, risk R9).
 *
 * **Why this exists.** Twenty scene images means twenty approvals, and at that
 * point the human gate is the bottleneck on the very first video. The per-scene
 * panel is still there for the ones that need a closer look; this is the sweep.
 *
 * Three affordances, in the order they matter:
 *
 * 1. **Approve all remaining** — one decision about a set. It posts the exact
 *    version ids the grid displayed, so a scene that regenerated mid-scroll is
 *    skipped and reported rather than silently swept up.
 * 2. **Keyboard traversal** — arrows move, `a` approves, `r` rejects, `g`
 *    regenerates. A reviewer scanning twenty frames should never have to move
 *    their hand to the mouse.
 * 3. **Per-item regenerate** — the ones that miss are re-run alone, not the
 *    whole set.
 *
 * Nothing here decides whether an action is legal: every button reads the
 * server's `capabilities`, the same payload the single-item panel uses (§11).
 */
const POLL_MS = 1500;

export function ContactSheet({
  projectId,
  kind,
  stageState,
  onOpenScene,
}: {
  projectId: string;
  kind: string;
  /** The *stage's* artifact state, from the project payload.
   *
   * Needed because a tile only knows about its own artifact, and for the first
   * few seconds after Generate there are no per-scene artifacts at all — see
   * the polling rule below. */
  stageState: string | null;
  onOpenScene: (sceneId: string) => void;
}) {
  const queryClient = useQueryClient();
  const [focused, setFocused] = useState(0);
  const [note, setNote] = useState<string | null>(null);
  const tileRefs = useRef<(HTMLDivElement | null)[]>([]);

  const sheet = useQuery({
    queryKey: ["contact-sheet", projectId, kind],
    queryFn: () => api.getContactSheet(projectId, kind),
    // **Both conditions, and the second one is why this was broken.**
    //
    // Polling on tile state alone assumed a tile already had an artifact to
    // report GENERATING. For the first seconds after Generate it does not:
    // the fan-out lives inside one task, so every tile is an empty cell with
    // `state: null` — nothing is generating as far as this query can see, the
    // interval switches off, and the grid stays blank until the reviewer
    // reloads the page. Exactly the M1-09a bug the single-item panel already
    // carries a note about, one component over.
    //
    // The stage's own state does know: it is GENERATING from the moment the
    // job is accepted. It also stops, so an ungenerated stage a reviewer is
    // merely looking at does not poll forever.
    refetchInterval: (query) =>
      stageState === "GENERATING" ||
      query.state.data?.tiles.some((t) => t.state === "GENERATING")
        ? POLL_MS
        : false,
  });

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ["contact-sheet"] });
    void queryClient.invalidateQueries({ queryKey: ["project", projectId] });
    void queryClient.invalidateQueries({ queryKey: ["artifact"] });
  };

  const decide = useMutation({
    mutationFn: ({
      tile,
      approve,
    }: {
      tile: ContactTile;
      approve: boolean;
    }) => {
      const action = approve ? api.approve : api.reject;
      return action(tile.version_id!, tile.version_no!);
    },
    onSuccess: invalidate,
  });

  const regenerate = useMutation({
    mutationFn: (tile: ContactTile) =>
      api.generate(projectId, kind, true, tile.scene_id),
    onSuccess: invalidate,
  });

  const approveAll = useMutation({
    mutationFn: (ids: string[]) => api.approveRemaining(projectId, ids),
    onSuccess: (result) => {
      // Partial success is reported, not swallowed. "Approved 18, 2 skipped"
      // is the difference between a reviewer who knows to look again and one
      // who believes the pass is done.
      setNote(
        result.skipped.length === 0
          ? `Approved ${result.approved}.`
          : `Approved ${result.approved}. ${result.skipped.length} skipped — they changed while you were reviewing.`,
      );
      invalidate();
    },
  });

  const tiles = sheet.data?.tiles ?? [];
  const busy = decide.isPending || regenerate.isPending || approveAll.isPending;

  // Derived, not synchronised — the same rule the stage selection follows.
  // A poll that shrinks the grid must not leave the roving focus past the end,
  // and clamping here costs one comparison; an effect that "kept it in range"
  // would setState during render and cascade.
  const active = tiles.length === 0 ? 0 : Math.min(focused, tiles.length - 1);

  if (sheet.isPending) {
    return (
      <p className="text-sm" style={{ color: "var(--color-ink-muted)" }}>
        Loading scenes…
      </p>
    );
  }
  if (sheet.error) {
    return (
      <p className="text-sm" style={{ color: "var(--color-state-failed)" }}>
        {sheet.error.message}
      </p>
    );
  }
  if (tiles.length === 0) {
    return null;
  }

  const act = (index: number, key: string) => {
    const tile = tiles[index];
    if (!tile || busy) return;
    const caps = tile.capabilities;
    if (key === "a" && caps.can_approve && tile.version_id) {
      decide.mutate({ tile, approve: true });
    } else if (key === "r" && caps.can_reject && tile.version_id) {
      decide.mutate({ tile, approve: false });
    } else if (key === "g" && caps.can_regenerate) {
      regenerate.mutate(tile);
    }
  };

  const move = (from: number, delta: number) => {
    const next = Math.min(tiles.length - 1, Math.max(0, from + delta));
    setFocused(next);
    tileRefs.current[next]?.focus();
  };

  const pending = sheet.data?.pending_version_ids ?? [];

  return (
    <section className="flex flex-col gap-3" data-testid="contact-sheet">
      <div className="flex flex-wrap items-center gap-3">
        <span className="text-sm" style={{ color: "var(--color-ink-muted)" }}>
          {sheet.data?.total} scenes · {sheet.data?.pending} awaiting review
        </span>
        <button
          type="button"
          data-testid="approve-all"
          disabled={pending.length === 0 || busy}
          onClick={() => approveAll.mutate(pending)}
          className="rounded-md px-4 py-2 text-sm font-medium disabled:opacity-30"
          style={{
            border: "1px solid var(--color-state-ok)",
            color: "var(--color-state-ok)",
          }}
        >
          {approveAll.isPending
            ? "Approving…"
            : // Counted from `pending`, not from `pending.length`: the batch
              // also carries the set-level manifest version, which is a real
              // approval and not a picture the reviewer is looking at. Showing
              // 7 above a grid of 6 would read as a miscount.
              //
              // When only the manifest is left — every tile decided one at a
              // time — the count is 0 and the button still has work to do, so
              // it says what that work is rather than offering "(0)".
              (sheet.data?.pending ?? 0) > 0
              ? `Approve all remaining (${sheet.data?.pending})`
              : "Approve the set"}
        </button>
        <span className="text-xs" style={{ color: "var(--color-ink-muted)" }}>
          ← → to move · a approve · r reject · g regenerate
        </span>
      </div>

      {note ? (
        <p
          data-testid="batch-result"
          aria-live="polite"
          className="text-sm"
          style={{ color: "var(--color-ink-muted)" }}
        >
          {note}
        </p>
      ) : null}

      {approveAll.error ? (
        <p className="text-sm" style={{ color: "var(--color-state-failed)" }}>
          {approveAll.error.message}
        </p>
      ) : null}

      {/* `role="grid"` with a roving tabindex: one stop in the tab order for
          the whole sheet, arrows within it. Twenty separate tab stops would
          make the keyboard path slower than the mouse it replaces. */}
      <div
        role="grid"
        aria-label={`${kind} contact sheet`}
        className="grid gap-3"
        style={{ gridTemplateColumns: "repeat(auto-fill, minmax(150px, 1fr))" }}
      >
        {tiles.map((tile, index) => (
          <Tile
            key={tile.scene_id}
            tile={tile}
            focused={index === active}
            busy={busy}
            ref={(node) => {
              tileRefs.current[index] = node;
            }}
            onFocus={() => setFocused(index)}
            onOpen={() => onOpenScene(tile.scene_id)}
            onKeyDown={(event) => {
              const columns = 1; // arrows are linear; the grid reflows freely
              if (event.key === "ArrowRight" || event.key === "ArrowDown") {
                event.preventDefault();
                move(index, columns);
              } else if (event.key === "ArrowLeft" || event.key === "ArrowUp") {
                event.preventDefault();
                move(index, -columns);
              } else if (event.key === "Enter") {
                event.preventDefault();
                onOpenScene(tile.scene_id);
              } else if (["a", "r", "g"].includes(event.key.toLowerCase())) {
                event.preventDefault();
                act(index, event.key.toLowerCase());
              }
            }}
          />
        ))}
      </div>
    </section>
  );
}

function Tile({
  tile,
  focused,
  busy,
  ref,
  onFocus,
  onOpen,
  onKeyDown,
}: {
  tile: ContactTile;
  focused: boolean;
  busy: boolean;
  ref: (node: HTMLDivElement | null) => void;
  onFocus: () => void;
  onOpen: () => void;
  onKeyDown: (event: React.KeyboardEvent) => void;
}) {
  const state = tile.state ?? "PENDING";
  return (
    <div
      ref={ref}
      role="gridcell"
      // Roving tabindex: only the focused cell is reachable by Tab.
      tabIndex={focused ? 0 : -1}
      onFocus={onFocus}
      onKeyDown={onKeyDown}
      onDoubleClick={onOpen}
      data-testid={`tile-${tile.scene_index}`}
      data-state={state}
      aria-label={`Scene ${tile.scene_index}: ${humanise(state)}`}
      className="flex flex-col gap-1 rounded-md p-2 outline-none"
      style={{
        background: "var(--color-surface-raised)",
        border: `1px solid ${
          focused ? artifactStateColor(state) : "var(--color-border-subtle)"
        }`,
        opacity: busy ? 0.6 : 1,
      }}
    >
      <div
        className="relative flex aspect-[9/16] items-center justify-center overflow-hidden rounded"
        style={{ background: "var(--color-surface)" }}
      >
        {tile.asset_url ? (
          // A plain <img>: these are content-addressed and served by nginx via
          // X-Accel-Redirect (ADR-011), so Next's image optimiser would put a
          // Node process back in the path the whole design removes.
          // eslint-disable-next-line @next/next/no-img-element
          <img
            src={tile.asset_url}
            alt={`Scene ${tile.scene_index}`}
            loading="lazy"
            className="size-full object-cover"
          />
        ) : (
          <span className="text-xs" style={{ color: "var(--color-ink-muted)" }}>
            {state === "GENERATING" ? "Generating…" : "No image"}
          </span>
        )}
      </div>

      <div className="flex items-center gap-1 text-xs">
        <span
          aria-hidden
          className="inline-block size-2 shrink-0 rounded-full"
          style={{ background: artifactStateColor(state) }}
        />
        <span style={{ color: "var(--color-ink-muted)" }}>
          {tile.scene_index}
          {tile.stale_since ? " · stale" : ""}
        </span>
      </div>
      <p
        className="line-clamp-2 text-xs"
        style={{ color: "var(--color-ink-muted)" }}
      >
        {tile.narration}
      </p>
    </div>
  );
}
