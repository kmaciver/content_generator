## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

Rules:
- **Read files directly for ordinary work.** Measured on this corpus (2026-08-01): `graphify query` cost ~1,600 tokens and returned a truncated node/edge list that named the right ADRs but did not answer the question; reading those two ADRs cost ~1,360 tokens and did. The whole decision record is ~28k tokens and the filenames are self-describing, so the index costs about as much as the thing it indexes. Grep and Read are the default.
- **Use graphify for architecture review and reachability**, not for lookup — "what else touches this", "what did decision X kill", "is anything still referencing the superseded design". It earns its keep on questions where you don't know which file to open, and on *deliberate absences* (e.g. `worker-render` not receiving provider secrets) that grep cannot see.
- `graphify path "<A>" "<B>"` and `graphify explain "<concept>"` are the useful entry points; `GRAPH_REPORT.md` (~6k tokens) for a broad sweep.
- Revisit this if the corpus grows ~10×. The break-even is when the corpus stops fitting in context.
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).
