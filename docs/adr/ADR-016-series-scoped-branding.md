# ADR-016 — Branding assets are series-scoped, and projects pin the version they used

- **Status:** Accepted (design; the `style_preset` drop has shipped, the rest
  lands in M3)
- **Date:** 2026-08-02
- **Deciders:** kmaciver
- **Related:** SADD §10.2 (`series.style_preset`), §11 (`ApprovalPolicy`), §10.3
  rule 4 (reproducibility), finding S1 (artifact uniqueness), risk R7 (style
  consistency across ~20 hard cuts)
- **Supersedes:** `series.style_preset` as a free-form jsonb column

## Context

The product requires a **recurring character** and a consistent **visual style**
across every video, so that episodes are recognisably the same show. At ~20
hard cuts per video (§1.0.1) this is what separates professional output from a
slideshow, which is why R7 already names it as a risk and M3 already carries a
"style consistency machinery" line item.

The concrete workflow: define a character in structured text, generate several
reference-sheet candidates, approve one set, derive a reusable style preset,
and then generate every scene image against the approved character *and* style.

The question this ADR answers is **where those assets live**, because the
answer determines the schema, the pipeline, and what happens to old videos when
the branding changes.

Three placements were considered.

### B — project-scoped artifacts

Add `ArtifactKind.CHARACTER` and `ArtifactKind.STYLE_PRESET`, and let
characters be ordinary artifacts of a `video_project`.

This is by far the cheapest option. It inherits versioning, the artifact FSM,
`review_decision`, the `artifact_version_status` view (finding B1), the
immutability triggers, the audit trail, the `capabilities` payload, and M1-09's
version-switcher UI — all of it, for the cost of two enum labels.

**Rejected because it cannot express the requirement.** A character scoped to
one video is not recurring. Every episode would define its own, and "the same
show" would be a thing the operator maintained by hand — which is the failure
this whole feature exists to prevent.

### C — hybrid: series-scoped identity, artifact-backed content

Keep a thin series-scoped table for identity, but store each version's content
as `artifact_version` rows so approval still runs through `review_decision` and
the status view.

Attractive, and rejected for a concrete reason: **`artifact.project_id` is
`NOT NULL`**, so an artifact cannot be series-scoped. Relaxing it collides with
finding S1:

```sql
UNIQUE (project_id, kind, scene_ref) NULLS NOT DISTINCT
```

`NULLS NOT DISTINCT` treats NULLs as **equal**, so `(NULL, 'character', NULL)`
can exist exactly **once in the whole table**. Two characters in two different
series would violate it — and so would two characters in the *same* series.
Adopting C therefore means replacing the constraint M1-01 deliberately
installed and tested, on the table that scripts, scene images, timelines and
renders all hang off.

Two further assumptions behind C did not survive inspection:

- **Characters do not need `stale_since`.** The staleness cascade marks the
  *scene images* stale when their inputs change, and scene images are already
  artifacts. The character itself never needs the column.
- **Candidate groups are not versions.** Four to eight reference sheets
  approved as a *set* does not fit "one artifact, many versions, one approved".
  That mismatch exists under C too, so C buys less than it appears to.

## Decision

**Branding assets are series-scoped tables of their own, and each project pins
the versions it was generated against.**

### Series-scoped, not workspace-scoped

`series` is the narrowest scope that satisfies "every video looks the same".
One series gives you one look today; a second show with different branding
still works tomorrow. Workspace scoping would foreclose that and buys nothing,
since a single series already makes branding effectively global.

New tables hang off `series_id`: the character definition with its immutable
and variable traits, its generated reference sheets, and the style preset.
Reference images go through `StorageClient` as content-addressed objects like
every other binary; the rows hold storage keys, and `artifact` is not touched.

**Status values reuse `ArtifactState`'s vocabulary** — `PENDING`,
`AWAITING_APPROVAL`, `APPROVED`, `SUPERSEDED` — rather than inventing
`draft/review/archived`. Same words, separate column. That is the real cost of
not using the artifact tables, and reusing the vocabulary is what keeps it to
one cost instead of two.

### Projects pin their branding version

When generation starts, a `video_project` records **which character version and
which style version** it is building against, and that pin never moves.

This is the load-bearing half of the decision. Without it, approving character
v2 would retroactively invalidate every episode built from v1 — a staleness
cascade across the entire back catalogue, triggered by an ordinary tweak. With
it, superseding the series-level character affects **new projects only**, and
an existing video keeps producing consistent output forever.

The mechanism is the one §10.3 rule 4 already uses: `timeline.input_snapshot`
pins the exact version ids of its inputs so a render can always be explained.
This is the same idea one level up.

It also settles the staleness question cleanly: **series-level supersession does
not cascade.** `stale_since` continues to mean *within-project* invalidation —
a new approved script makes its scenes stale — and a pinned project is never
stale merely because the series moved on. Whether to offer an explicit "upgrade
this video to character v2" action is a separate question, deferred. The
default is pinned.

### Image generation requires a series

`video_project.series_id` is nullable (one-off videos, `ON DELETE SET NULL`).
A project with no series has no branding, so **image generation requires a
series with an approved character and an approved style**, and the API returns
409 otherwise. Workspace-level defaults as a fallback were considered and
rejected as more machinery for a case that has no user yet.

## Consequences

- **Character and style leave the video pipeline, and stay out of the DAG.**
  They are not stages a project produces; they are preconditions a project
  consumes. The obvious move is to give `pipeline.yaml` a second dependency
  field so a stage can require an approved *series* asset. **Decided against.**
  Every name in `requires` today is an `ArtifactKind` resolved against
  `artifact` rows of this project, and a branding dependency differs on all
  four counts that make an edge an edge:

  | | `requires: [scene_set]` | a character dependency |
  |---|---|---|
  | Resolution | `artifact` where `project_id = p.id` | the branding table where `series_id = p.series_id` — and `series_id` is nullable |
  | Staleness | cascades downstream | must **not** cascade (see the pinning section above) |
  | Satisfied by | the current approved version | the project's *pinned* version — project state, not series state |
  | Unmet means | the previous stage is in progress → a phase | this project has no branding → 409, fixed on another screen |

  Four differences out of four is not an edge with extra attributes; it is an
  admission check wearing an edge's clothes. So the DAG stays homogeneous and
  branding is verified in the dispatch service *before* the DAG is consulted.
  This keeps M2's YAML schema, its validator, the phase deriver, the staleness
  cascade, and the UI gating each with one meaning instead of two.
- **The UI splits.** Character editor, reference-candidate gallery and style
  editor live in series settings. The scene contact sheet and per-scene review
  stay with the project.
- **The cost profile improves sharply.** Generating 4–8 candidate sheets is
  expensive per video and negligible per series. Amortised across episodes it
  is noise, which is what makes the candidate-and-approve workflow affordable.
- **`series.style_preset` is superseded, and was dropped immediately** —
  revision `20bbda6ac985`, ahead of the table that replaces it, rather than in
  the same M3 migration as originally planned. Nothing read the column: the
  seed wrote one value and no code path ever read it back. That made removal a
  migration and two call sites. Once a reader attaches — M2's prompt rendering
  being the likely first — removal instead means a data migration out of
  unvalidated jsonb plus a ruling on which source wins when the two disagree.
  Dropping it early also closes the window in which such a reader could
  attach.
- **Versioning, approval and immutability are reimplemented** in these tables
  rather than inherited. That is the price of series scoping. It is bounded:
  three tables that nothing else references, so a wrong character model in M4
  costs a rewrite of three tables rather than surgery under every artifact in
  the system.
- **`state_transition` and `audit_event` still work**, since their subject is a
  polymorphic `(type, id)` pair with no foreign key. They need two new
  `subject_type` labels via an explicit `ALTER TYPE` migration (§10.4).
- **`ImageProvider` needs a capability gate.** Reference-image support varies
  enormously between providers, and a provider that cannot accept reference
  images cannot do character consistency at all. This follows the `VoiceCaps`
  precedent (findings B3/S5): the capability is checked at *configuration* time
  and fails there, not twenty images into a user's first video.

## Not yet verified

This record is largely a design decision taken before implementation, unlike
ADR-002 and ADR-011 which were written against measured results.

Verified: the claims about `artifact.project_id` and the S1 constraint were
checked against the live schema, and the `style_preset` drop is applied —
upgrade, downgrade, and re-upgrade all run, and `alembic check` reports no
drift between the models and the migrations.

Unproven until M3 ships: the branding tables, version pinning, the 409, and
the claim that keeping branding out of the DAG is cheaper than an edge type.
That last one is a prediction about M2 code that does not exist yet.
