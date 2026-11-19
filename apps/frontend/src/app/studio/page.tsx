import Link from "next/link";

import { Studio } from "./studio";

export const dynamic = "force-dynamic";

export default function StudioPage() {
  return (
    <main className="mx-auto flex min-h-screen max-w-5xl flex-col gap-8 px-4 py-10 sm:px-6 sm:py-16">
      <header className="flex flex-col gap-1">
        <Link
          href="/"
          className="text-xs"
          style={{ color: "var(--color-ink-muted)" }}
        >
          ← Home
        </Link>
        <h1 className="text-2xl font-semibold tracking-tight">Image Studio</h1>
        <p className="text-sm" style={{ color: "var(--color-ink-muted)" }}>
          Describe an image and generate it. Your prompt is sent exactly as you
          write it. No series character or style is added.
        </p>
      </header>
      <Studio />
    </main>
  );
}
