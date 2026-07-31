"use client";

// React Query provider — the backbone of the async-job UX (SADD §7.6/§20).
//
// The defaults here are chosen for a pipeline whose state changes because a
// *worker* did something, not because this tab did: nothing is fresh for long,
// and refetching on focus is usually the fastest way to learn a render
// finished. From M1 the job/artifact hooks add their own `refetchInterval`
// until a terminal state, per SADD §19.2.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";

export function Providers({ children }: { children: ReactNode }) {
  // useState, not a module-level client: on the server a module-level instance
  // would be shared across requests and across users.
  const [queryClient] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            staleTime: 5_000,
            refetchOnWindowFocus: true,
            // A failed poll is usually a restarting container; retry briefly
            // rather than surfacing a scary error on the first blip.
            retry: 2,
          },
        },
      }),
  );

  return (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
}
