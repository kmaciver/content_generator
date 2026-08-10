import type { Metadata } from "next";

import { Providers } from "./providers";
import "./globals.css";

export const metadata: Metadata = {
  title: "VideoForge",
  description: "Short-form educational video orchestration",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  // `suppressHydrationWarning`: extensions (password managers, recorders,
  // theme switchers) inject attributes onto <html> before React hydrates, and
  // the server HTML cannot know about them. Suppression is **one level deep** —
  // it covers this element's own attributes and nothing inside it — so a real
  // hydration bug anywhere in the app still surfaces.
  return (
    <html lang="en" suppressHydrationWarning>
      <body className="min-h-screen antialiased">
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
