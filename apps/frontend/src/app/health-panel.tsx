"use client";

// The component-map panel — M0's proof that the whole chain works end to end,
// and the first user of the polling pattern every review screen will reuse.

import { useQuery } from "@tanstack/react-query";

interface ComponentStatus {
  ok: boolean;
  latency_ms: number;
  error: string | null;
}

interface DeepHealth {
  status: string;
  components: Record<string, ComponentStatus>;
  service?: string;
  version?: string;
  python?: string;
  detail?: string;
}

async function fetchHealth(): Promise<DeepHealth> {
  // Same-origin BFF route. No token here — that is the entire point (S6):
  // there is nothing secret in this bundle to leak.
  const response = await fetch("/api/bff/health");
  return (await response.json()) as DeepHealth;
}

function StatusDot({ ok }: { ok: boolean }) {
  return (
    <span
      aria-hidden
      className="inline-block size-2.5 rounded-full"
      style={{
        background: ok ? "var(--color-state-ok)" : "var(--color-state-failed)",
      }}
    />
  );
}

export function HealthPanel() {
  const { data, isPending, isError, dataUpdatedAt } = useQuery({
    queryKey: ["health", "deep"],
    queryFn: fetchHealth,
    // The shape M1's job hooks take: poll while it matters, and let React
    // Query own the lifecycle rather than hand-rolling setInterval.
    refetchInterval: 5_000,
  });

  if (isPending) {
    return <p style={{ color: "var(--color-ink-muted)" }}>Checking…</p>;
  }
  if (isError || !data) {
    return (
      <p style={{ color: "var(--color-state-failed)" }}>
        Could not reach the BFF route.
      </p>
    );
  }

  const healthy = data.status === "ok";
  const components = Object.entries(data.components);

  return (
    <section className="w-full max-w-xl">
      <div className="mb-4 flex items-center gap-3">
        <StatusDot ok={healthy} />
        <h2 className="text-lg font-semibold">
          {healthy ? "All systems operational" : "Degraded"}
        </h2>
        <span
          className="ml-auto font-mono text-xs"
          style={{ color: "var(--color-ink-muted)" }}
        >
          {data.service ?? "videoforge-api"} v{data.version ?? "?"}
        </span>
      </div>

      <ul
        className="divide-y overflow-hidden rounded-lg border"
        style={{
          borderColor: "var(--color-border-subtle)",
          background: "var(--color-surface-raised)",
        }}
      >
        {components.length === 0 && (
          <li className="px-4 py-3 text-sm">
            {data.detail ?? "No components reported."}
          </li>
        )}
        {components.map(([name, status]) => (
          <li
            key={name}
            className="flex items-center gap-3 px-4 py-3"
            style={{ borderColor: "var(--color-border-subtle)" }}
          >
            <StatusDot ok={status.ok} />
            <span className="font-medium">{name}</span>
            {status.error && (
              <span
                className="truncate text-xs"
                style={{ color: "var(--color-state-failed)" }}
                title={status.error}
              >
                {status.error}
              </span>
            )}
            <span
              className="ml-auto font-mono text-xs"
              style={{ color: "var(--color-ink-muted)" }}
            >
              {status.latency_ms}ms
            </span>
          </li>
        ))}
      </ul>

      <p
        className="mt-3 font-mono text-xs"
        style={{ color: "var(--color-ink-muted)" }}
      >
        polling every 5s · updated{" "}
        {new Date(dataUpdatedAt).toLocaleTimeString()}
      </p>
    </section>
  );
}
