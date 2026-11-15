"use client";

import type { BudgetStatus } from "@/lib/api";

/** Today's spend against the cap, on the screen where money is spent (M5-07).
 *
 * The cap (S10) has existed since M3-11 and was invisible: it stopped work in
 * a worker, from a number no screen had ever shown, so the first sign of it
 * was a stage failing for a reason that read like a provider outage.
 *
 * **Two numbers, always together.** `spent` is deployment-wide, because the
 * cap is — `provider_usage` has no project column and the limit is a daily
 * total across everything. One figure on a project screen would be read as
 * that project's cost, so this project's own spend is shown beside it and both
 * are labelled. Showing only the capped number would have been a smaller
 * component and a wrong one.
 */
export function BudgetBar({ budget }: { budget: BudgetStatus | null }) {
  if (!budget) {
    return null;
  }

  // No cap configured means unlimited, not zero — the same reading
  // `check_budget` takes of `limit <= 0`. A progress bar against no limit is
  // not a thing, so this collapses to the one fact that still means something.
  if (!budget.capped) {
    return (
      <p
        data-testid="budget"
        className="text-xs"
        style={{ color: "var(--color-ink-muted)" }}
      >
        This video {money(budget.project_spent, budget.currency)} · today{" "}
        {money(budget.spent, budget.currency)} · no daily limit set
      </p>
    );
  }

  const used = fraction(budget.spent, budget.limit);

  return (
    <div data-testid="budget" className="flex flex-col gap-1">
      <p className="text-xs" style={{ color: "var(--color-ink-muted)" }}>
        This video{" "}
        <span style={{ color: "var(--color-ink)" }}>
          {money(budget.project_spent, budget.currency)}
        </span>{" "}
        · today{" "}
        <span
          data-testid="budget-spent"
          style={{
            color: budget.exhausted
              ? "var(--color-state-failed)"
              : "var(--color-ink)",
          }}
        >
          {money(budget.spent, budget.currency)}
        </span>{" "}
        of {money(budget.limit, budget.currency)}
        {budget.exhausted ? " — limit reached, resets at UTC midnight" : null}
      </p>
      {/* A bar rather than a percentage: the question is "how much room is
          left", which is a length, not a number to read. */}
      <div
        className="h-1 w-full overflow-hidden rounded"
        style={{ background: "var(--color-border-subtle)" }}
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(used * 100)}
        aria-label="Daily provider spend"
      >
        <div
          className="h-full"
          style={{
            // Clamped at 100%: the cap is checked before each call and several
            // workers can pass it concurrently, so a burst overshoots by about
            // one round of in-flight calls by design. A bar wider than its
            // track would render that deliberate slack as a layout bug.
            width: `${Math.min(100, used * 100)}%`,
            background: budget.exhausted
              ? "var(--color-state-failed)"
              : "var(--color-state-generating)",
          }}
        />
      </div>
    </div>
  );
}

function money(amount: string, currency: string): string {
  return `${amount} ${currency}`;
}

function fraction(spent: string, limit: string): number {
  // Parsed only here, and only to size a bar. Every figure the reader actually
  // sees is the server's own string, so no displayed amount is ever the result
  // of arithmetic in a double.
  const cap = Number(limit);
  if (!Number.isFinite(cap) || cap <= 0) {
    return 0;
  }
  return Math.max(0, Number(spent) / cap);
}
