import { ProjectList } from "./project-list";

// Server shell, client interactivity, BFF data — the shape M0 established.

export const dynamic = "force-dynamic";

export default function ProjectsPage() {
  return (
    <main className="mx-auto flex min-h-screen max-w-3xl flex-col gap-8 px-6 py-16">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">Projects</h1>
        <p className="mt-1 text-sm" style={{ color: "var(--color-ink-muted)" }}>
          A topic in, a reviewable script out.
        </p>
      </header>

      <ProjectList />
    </main>
  );
}
