import Link from "next/link";

import { HealthPanel } from "./health-panel";

// A server component shell wrapping a client component (SADD §7.6). The full
// dashboard and pipeline rail arrive in M5; the shape — server shell, client
// interactivity, BFF data — stays.

export default function Home() {
  return (
    <main className="mx-auto flex min-h-screen max-w-3xl flex-col gap-8 px-6 py-16">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">VideoForge</h1>
        <p className="mt-1 text-sm" style={{ color: "var(--color-ink-muted)" }}>
          Short-form educational video orchestration
        </p>
      </header>

      <Link
        href="/projects"
        className="self-start rounded-md px-4 py-2 text-sm font-medium"
        style={{
          border: "1px solid var(--color-border-subtle)",
          color: "var(--color-ink)",
        }}
      >
        Projects →
      </Link>

      <HealthPanel />
    </main>
  );
}
