import Link from "next/link";

import { BrandingEditor } from "./branding-editor";

export const dynamic = "force-dynamic";

export default async function SeriesPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  return (
    <main className="mx-auto flex min-h-screen max-w-3xl flex-col gap-8 px-6 py-16">
      <header className="flex flex-col gap-1">
        <Link
          href="/"
          className="text-xs"
          style={{ color: "var(--color-ink-muted)" }}
        >
          ← Projects
        </Link>
        <h1 className="text-2xl font-semibold tracking-tight">
          Series branding
        </h1>
        <p className="text-sm" style={{ color: "var(--color-ink-muted)" }}>
          The character and style every episode in this series is generated
          against. A project pins these on its first image job and never moves,
          so editing here affects future videos — not ones already made.
        </p>
      </header>
      <BrandingEditor seriesId={id} />
    </main>
  );
}
