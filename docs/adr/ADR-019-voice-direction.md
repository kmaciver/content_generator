# ADR-019 — Voice direction: emotion tags as a stage between scenes and voice

- **Status:** Accepted. The alignment and `language_code` assumptions were
  verified against the live API on 2026-11-18 (see Consequences).
- **Date:** 2026-11-18
- **Deciders:** kmaciver
- **Related:** B3 revised (one synthesis per script), ADR-009 (pipeline as DAG),
  M3-12 (written alignment, per-character timings), M5-01 (an editable text
  stage with server-side validation)

## Context

ElevenLabs `eleven_v3` performs inline *audio tags* — `[whispers]`,
`[excited]`, `[laughs]` — as delivery rather than words. The v2 models read
them aloud or ignore them. We want a reviewer to see the narration, select
words, attach an emotion, approve, and get a narration that performs it, with
captions that never show the tags.

Three facts in this codebase shape where the tags can live:

1. **Scenes are immutable.** Editing `scene.narration_text` creates a new scene
   set, which creates new scene rows, which regenerates every image. That is the
   most expensive operation in the system, and it would happen for a change only
   the voice can hear.
2. **`narration_text` has many readers**: captions, `scene_spans`, the timeline
   compiler, post copy, prompts. Inline tags there would need stripping in every
   one, and a missed one puts `[excited]` on screen or into a published caption.
3. **v3 tags have no closing tag.** A tag colours what follows it, roughly to
   the end of the sentence. A "range" in the UI would promise something the
   provider does not do.

## Decision

**A new `voice_direction` artifact kind and pipeline stage.** It requires
`scene_set`, and `voice` requires it. Only `voice` descends from it, so editing a
tag stales the narration and nothing else.

- **Tags are positions, not text.** Content is
  `{scene_set_version_id, scenes: [{scene_id, scene_index, narration, tags:
  [{word, tag}]}], tag_count}`. `word` is the index (in `narration.split()`) of
  the word the tag precedes; `len(words)` means after the last word. The words
  always come from the approved scene set and never from a request, so a tag can
  move but cannot change what is said.
- **A closed vocabulary** of ten tags (`videoforge_domain.voice_direction.AUDIO_TAGS`).
  A misspelt free-text tag is read aloud or ignored rather than refused, so the
  list is fixed on purpose.
- **Generation suggests; a human decides.** `voice_direction.generate` (LLM, on
  `llm`) asks for each scene's narration back with tags inserted, then recovers
  positions. A scene whose *words* changed is kept untagged rather than matched
  fuzzily. Generated tags are capped at two per scene. Human edits are validated
  (closed vocabulary, valid positions, this scene set's scene ids), are refused
  with a 422 rather than repaired, and are capped at twelve per scene.
- **The voice stage composes and strips.** It re-validates the approved direction
  against the scenes it is about to speak, inserts `[tag] ` at word starts
  (preserving the narration byte for byte when there are no tags), and sends the
  result to `eleven_v3`. Before grouping words, it removes the inserted tag
  characters from the alignment **by offset**, because the composer knows exactly
  which ranges it inserted. If the alignment is not one-to-one with the request,
  it falls back to removing bracketed runs and logs a warning. Either way,
  `scene_spans` counts the clean narration exactly as before.
- **`eleven_v3` is the default voice model** for every series. There is no
  per-series opt-in: an untagged direction sends exactly the text it always did.
- The voice stage now **enforces `VoiceCaps.max_characters`** before spending,
  per model. v3's limit is much lower than turbo's.

## Alternatives rejected

- **Tags inline in `scene.narration_text`.** Every tag edit would regenerate all
  images (fact 1), and every reader would need to strip (fact 2).
- **A second "voice text" column on `scene`.** Same immutability cost, plus two
  copies of the narration that can drift apart.
- **Tags in the voice stage's request with no review step.** The reviewer would
  hear the performance only after paying for it, with nowhere to correct a tag.
- **Free-text tags.** Fails silently, as described above.
- **Per-series opt-in.** Declined by the operator; an untagged direction is
  already a no-op.

## Consequences

- **Verified 2026-11-18** with `scripts/elevenlabs-v3-probe.py`: v3's
  `/with-timestamps` returns the written alignment one-to-one with a tagged
  request (tags included), so tags are stripped by offset, and the stripped
  words match the narration exactly. v3 accepts `language_code`. The
  documented 5,000-character limit holds: 3,000 characters returned 200 in
  83 s and 5,000 in 122 s, both inside the voice request's 300 s timeout. A
  90-second Reel is ~1,500 characters with tags. Voice versions record
  `meta.tag_strip_method` (`none` / `offset` / `bracket_scan`), so production
  shows which path it took.
- `voice` cannot run until a direction is approved, so even a video with no tags
  needs one extra approval. "Approve remaining" and series auto-approval cover it.
- `eleven_v3` reads about 20% slower than turbo (measured on one 157-word
  Portuguese script: 45.7 s → 54.8 s of speech; the four tags added only
  ~1.7 s of pauses). A probe of `voice_settings.speed` on v3 gave inconsistent
  results (1.1 → 89%, 1.2 → 92%, and 1.3 was accepted), so pace is set after
  synthesis instead: `PROVIDERS__VOICE__TEMPO` runs ffmpeg `atempo` and scales
  the alignment by the same factor before anything is stored.
- `eleven_v3` costs more per character than `eleven_turbo_v2_5` and has higher
  latency. `_PER_CHARACTER_USD` carries a placeholder like the other entries.
- Approving a new scene set stales the direction, and the voice stage refuses a
  direction whose scene ids do not match. The fix is to regenerate it, and the
  error says so.
- Postgres cannot drop the new `artifact_kind` label, so the migration's
  downgrade is a documented no-op.
