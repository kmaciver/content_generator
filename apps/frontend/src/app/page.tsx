import { HealthPanel } from "./health-panel";

// A server component shell wrapping a client component (SADD §7.6). The
// dashboard and /projects/[id] rail replace this body in M1/M5; the shape —
// server shell, client interactivity, BFF data — stays.

export default function Home() {
  return (
    <main className="mx-auto flex min-h-screen max-w-3xl flex-col gap-8 px-6 py-16">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">VideoForge</h1>
        <p className="mt-1 text-sm" style={{ color: "var(--color-ink-muted)" }}>
          Short-form educational video orchestration — M0 foundation
        </p>
      </header>

      <HealthPanel />
    </main>
  );
}
