// eslint-config-next 16 ships NATIVE flat config (each subpath exports a flat
// array), so these are spread directly. An earlier version of this file wrapped
// them in FlatCompat — that double-processes an already-flat config and dies
// with a circular-structure error during schema validation. If you see that
// error again, something reintroduced the compat layer.
import coreWebVitals from "eslint-config-next/core-web-vitals";
import typescriptConfig from "eslint-config-next/typescript";

// Named, not an anonymous default export (import/no-anonymous-default-export).
const config = [
  {
    ignores: [
      ".next/**",
      "node_modules/**",
      "next-env.d.ts",
      // Playwright output: traces, screenshots and generated HTML reports.
      "test-results/**",
      "playwright-report/**",
      "blob-report/**",
    ],
  },
  ...coreWebVitals,
  ...typescriptConfig,
];

export default config;
