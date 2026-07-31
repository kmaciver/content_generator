import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Standalone output keeps the production image small: Next traces the
  // modules actually reached and emits a self-contained server, so the runtime
  // stage copies a bundle instead of the whole node_modules tree.
  output: "standalone",
  // The build runs from apps/frontend but the Docker context is the repo root;
  // this stops Next inferring a workspace root further up and mis-tracing.
  outputFileTracingRoot: __dirname,
  reactStrictMode: true,
};

export default nextConfig;
