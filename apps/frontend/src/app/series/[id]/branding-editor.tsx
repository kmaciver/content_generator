"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  api,
  type BrandingStatus,
  type CharacterSummary,
  type StyleSummary,
} from "@/lib/api";

/** Series branding: character editor, reference gallery, style editor (M3-13b).
 *
 * **A separate surface from the project review screen**, for ADR-016's reason:
 * a character and a style are not stages a project produces, they are
 * preconditions it consumes. Putting them in the project screen would suggest
 * editing them affects *this* video, when in fact a project pins its branding
 * on its first image job and never moves.
 *
 * **Versions, not fields.** Every save here creates a new version and approves
 * nothing. That is not ceremony: an approved character is what twenty images
 * were generated against, and an in-place edit would silently change the
 * meaning of every one of them. Approving supersedes the incumbent, and the
 * partial unique index in the database is what actually guarantees one winner.
 *
 * Nothing here decides whether a series can generate — `ready` and `missing`
 * come from the server, the same contract `capabilities` follows one level
 * down.
 */
const POLL_MS = 2000;

export function BrandingEditor({ seriesId }: { seriesId: string }) {
  const queryClient = useQueryClient();
  const [jobId, setJobId] = useState<string | null>(null);

  const branding = useQuery({
    queryKey: ["branding", seriesId],
    queryFn: () => api.getBranding(seriesId),
    // Only while a reference run is in flight. Sheets take ~25s for four
    // images, and the gallery has nothing to say the rest of the time.
    refetchInterval: () => (jobId ? POLL_MS : false),
  });

  const job = useQuery({
    queryKey: ["job", jobId],
    queryFn: () => api.getJob(jobId!),
    enabled: Boolean(jobId),
    refetchInterval: (query) =>
      ["QUEUED", "RUNNING"].includes(query.state.data?.status ?? "")
        ? POLL_MS
        : false,
  });

  if (jobId && job.data && !["QUEUED", "RUNNING"].includes(job.data.status)) {
    // Derived, not synchronised — the same rule the project screen follows.
    // Clearing this in an effect would setState during render.
    void queryClient.invalidateQueries({ queryKey: ["branding", seriesId] });
  }

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ["branding", seriesId] });
  };

  const createCharacter = useMutation({
    mutationFn: (body: {
      name: string;
      immutable_traits: Record<string, unknown>;
      variable_traits: Record<string, unknown>;
    }) => api.createCharacter(seriesId, body),
    onSuccess: invalidate,
  });

  const createStyle = useMutation({
    mutationFn: (body: { name: string; fields: Record<string, unknown> }) =>
      api.createStyle(seriesId, body),
    onSuccess: invalidate,
  });

  const approveCharacter = useMutation({
    mutationFn: ({ id, group }: { id: string; group?: string }) =>
      api.approveCharacter(id, group),
    onSuccess: invalidate,
  });

  const approveStyle = useMutation({
    mutationFn: (id: string) => api.approveStyle(id),
    onSuccess: invalidate,
  });

  const generateRefs = useMutation({
    mutationFn: (characterId: string) => api.generateReferences(characterId),
    onSuccess: (result) => {
      setJobId(result.job_id);
      invalidate();
    },
  });

  if (branding.isPending) return <Muted>Loading branding…</Muted>;
  if (branding.error) return <Failed>{branding.error.message}</Failed>;

  const data = branding.data;
  const busy =
    createCharacter.isPending ||
    createStyle.isPending ||
    approveCharacter.isPending ||
    approveStyle.isPending ||
    generateRefs.isPending;
  const error =
    createCharacter.error ??
    createStyle.error ??
    approveCharacter.error ??
    approveStyle.error ??
    generateRefs.error;

  return (
    <div className="flex flex-col gap-8">
      {/* The server's answer to "can this series generate images", verbatim.
          "Waiting on: an approved style" beats a disabled button with no
          explanation — the same reason `unmet` exists on a stage. */}
      <div
        className="flex items-center gap-2 text-sm"
        data-testid="branding-ready"
        aria-live="polite"
      >
        <span
          aria-hidden
          className="inline-block size-2 rounded-full"
          style={{
            background: data.ready
              ? "var(--color-state-ok)"
              : "var(--color-state-review)",
          }}
        />
        <span>
          {data.ready
            ? "Ready to generate images"
            : `Waiting on: ${data.missing.join(", ")}`}
        </span>
      </div>

      {error ? <Failed>{error.message}</Failed> : null}
      {jobId && job.data ? (
        <Muted>
          Reference sheets: {job.data.status.toLowerCase()}
          {job.data.status === "SUCCEEDED" ? " — review them below" : "…"}
        </Muted>
      ) : null}

      <Section title="Character">
        {data.character ? (
          <VersionBadge
            label={`${data.character.name} v${data.character.version_no}`}
            status={data.character.status}
          />
        ) : (
          <Muted>No approved character yet.</Muted>
        )}

        <Gallery
          references={data.references}
          empty="No approved reference sheets. Generate a set from a character version below, then approve the group."
        />

        <History
          items={data.characters}
          busy={busy}
          onApprove={(id, group) => approveCharacter.mutate({ id, group })}
          onGenerate={(id) => generateRefs.mutate(id)}
        />

        <TraitEditor
          busy={busy}
          onSave={(name, immutable, variable) =>
            createCharacter.mutate({
              name,
              immutable_traits: immutable,
              variable_traits: variable,
            })
          }
        />
      </Section>

      <Section title="Style">
        {data.style ? (
          <>
            <VersionBadge
              label={`${data.style.name} v${data.style.version_no}`}
              status={data.style.status}
            />
            {/* What the fields actually compile to. The operator edits fields;
                this is the text the provider receives, so guessing at it is
                exactly the drift the compiled block exists to remove. */}
            <details className="text-xs">
              <summary
                className="cursor-pointer"
                style={{ color: "var(--color-ink-muted)" }}
              >
                Compiled prompt block
              </summary>
              <pre className="mt-2 overflow-x-auto whitespace-pre-wrap">
                {data.style.prompt_block}
              </pre>
            </details>
          </>
        ) : (
          <Muted>No approved style yet.</Muted>
        )}

        <ul className="flex flex-col gap-1 text-xs">
          {data.styles.map((style) => (
            <li key={style.id} className="flex items-center gap-2">
              <span style={{ color: "var(--color-ink-muted)" }}>
                v{style.version_no} · {style.name} · {style.status}
              </span>
              {style.status !== "APPROVED" ? (
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => approveStyle.mutate(style.id)}
                  data-testid={`approve-style-${style.version_no}`}
                  className="rounded px-2 py-0.5 disabled:opacity-30"
                  style={{
                    border: "1px solid var(--color-state-ok)",
                    color: "var(--color-state-ok)",
                  }}
                >
                  Approve
                </button>
              ) : null}
            </li>
          ))}
        </ul>

        <FieldEditor
          busy={busy}
          existing={data.style}
          onSave={(name, fields) => createStyle.mutate({ name, fields })}
        />
      </Section>
    </div>
  );
}

function Gallery({
  references,
  empty,
}: {
  references: { id: string; storage_key: string; angle: string }[];
  empty: string;
}) {
  if (references.length === 0) return <Muted>{empty}</Muted>;
  return (
    <div
      className="grid gap-2"
      style={{ gridTemplateColumns: "repeat(auto-fill, minmax(120px, 1fr))" }}
    >
      {references.map((reference) => (
        <figure key={reference.id} className="flex flex-col gap-1">
          {/* Served by nginx (ADR-011); Next's optimiser would put a Node
              process back in the path that design removes. */}
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            src={`/assets/assets/${reference.storage_key}`}
            alt={`${reference.angle} view`}
            loading="lazy"
            className="w-full rounded"
            style={{ border: "1px solid var(--color-border-subtle)" }}
          />
          <figcaption
            className="text-xs"
            style={{ color: "var(--color-ink-muted)" }}
          >
            {reference.angle}
          </figcaption>
        </figure>
      ))}
    </div>
  );
}

function History({
  items,
  busy,
  onApprove,
  onGenerate,
}: {
  items: CharacterSummary[];
  busy: boolean;
  onApprove: (id: string, group?: string) => void;
  onGenerate: (id: string) => void;
}) {
  return (
    <ul className="flex flex-col gap-1 text-xs">
      {items.map((character) => (
        <li key={character.id} className="flex flex-wrap items-center gap-2">
          <span style={{ color: "var(--color-ink-muted)" }}>
            v{character.version_no} · {character.name} · {character.status}
          </span>
          <button
            type="button"
            disabled={busy}
            onClick={() => onGenerate(character.id)}
            data-testid={`generate-refs-${character.version_no}`}
            className="rounded px-2 py-0.5 disabled:opacity-30"
            style={{
              border: "1px solid var(--color-state-generating)",
              color: "var(--color-state-generating)",
            }}
          >
            Generate sheets
          </button>
          {character.status !== "APPROVED" ? (
            <button
              type="button"
              disabled={busy}
              onClick={() =>
                onApprove(
                  character.id,
                  character.approved_reference_group_id ?? undefined,
                )
              }
              data-testid={`approve-character-${character.version_no}`}
              className="rounded px-2 py-0.5 disabled:opacity-30"
              style={{
                border: "1px solid var(--color-state-ok)",
                color: "var(--color-state-ok)",
              }}
            >
              Approve
            </button>
          ) : null}
        </li>
      ))}
    </ul>
  );
}

/** Traits as JSON, deliberately.
 *
 * A per-key form would be friendlier and wrong: the trait vocabulary is not
 * fixed — `head_outline_rule` and `profile_rule` were both added by measurement
 * on 2026-08-08 — and a form with a fixed set of rows would quietly prevent the
 * operator from adding the next one. The compiler already carries unknown keys
 * through, so the editor should too.
 */
function TraitEditor({
  busy,
  onSave,
}: {
  busy: boolean;
  onSave: (
    name: string,
    immutable: Record<string, unknown>,
    variable: Record<string, unknown>,
  ) => void;
}) {
  const [name, setName] = useState("");
  const [immutable, setImmutable] = useState("{}");
  const [variable, setVariable] = useState("{}");
  const [parseError, setParseError] = useState<string | null>(null);

  const save = () => {
    try {
      const i = JSON.parse(immutable) as Record<string, unknown>;
      const v = JSON.parse(variable) as Record<string, unknown>;
      setParseError(null);
      onSave(name.trim(), i, v);
    } catch (err) {
      // Caught here rather than sent: a 400 round-trip to learn about a
      // missing brace is a slow way to find a typo.
      setParseError(err instanceof Error ? err.message : String(err));
    }
  };

  return (
    <details className="flex flex-col gap-2 text-sm">
      <summary className="cursor-pointer">New character version</summary>
      <div className="mt-2 flex flex-col gap-2">
        <Input
          value={name}
          onChange={setName}
          placeholder="Name"
          label="Character name"
        />
        <Area
          value={immutable}
          onChange={setImmutable}
          label="Immutable traits (JSON)"
          hint="Name a colour on every element. The palette says what may appear; traits say where."
        />
        <Area
          value={variable}
          onChange={setVariable}
          label="Variable traits (JSON)"
          hint="What a scene may change: pose, expression, framing."
        />
        {parseError ? <Failed>{parseError}</Failed> : null}
        <Save busy={busy} disabled={!name.trim()} onClick={save} />
      </div>
    </details>
  );
}

function FieldEditor({
  busy,
  existing,
  onSave,
}: {
  busy: boolean;
  existing: StyleSummary | null;
  onSave: (name: string, fields: Record<string, unknown>) => void;
}) {
  const [name, setName] = useState(existing?.name ?? "");
  // Pre-filled from the approved style: a new version is almost always an edit
  // of the current one, and retyping nine fields to change `line` is how an
  // operator ends up editing the database instead.
  const [fields, setFields] = useState(
    JSON.stringify(existing?.fields ?? {}, null, 2),
  );
  const [parseError, setParseError] = useState<string | null>(null);

  const save = () => {
    try {
      const parsed = JSON.parse(fields) as Record<string, unknown>;
      setParseError(null);
      onSave(name.trim(), parsed);
    } catch (err) {
      setParseError(err instanceof Error ? err.message : String(err));
    }
  };

  return (
    <details className="flex flex-col gap-2 text-sm">
      <summary className="cursor-pointer">New style version</summary>
      <div className="mt-2 flex flex-col gap-2">
        <Input
          value={name}
          onChange={setName}
          placeholder="Name"
          label="Style name"
        />
        <Area
          value={fields}
          onChange={setFields}
          label="Style fields (JSON)"
          hint="`avoid` becomes the negative prompt and `cast` describes how every figure is built; neither appears in the positive block."
        />
        {parseError ? <Failed>{parseError}</Failed> : null}
        <Save busy={busy} disabled={!name.trim()} onClick={save} />
      </div>
    </details>
  );
}

function Section({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section className="flex flex-col gap-3">
      <h2 className="text-lg font-semibold">{title}</h2>
      {children}
    </section>
  );
}

function VersionBadge({
  label,
  status,
}: {
  label: string;
  status: BrandingStatus;
}) {
  return (
    <p className="text-sm">
      {label}{" "}
      <span style={{ color: "var(--color-ink-muted)" }}>· {status}</span>
    </p>
  );
}

function Input({
  value,
  onChange,
  placeholder,
  label,
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder: string;
  label: string;
}) {
  return (
    <input
      value={value}
      onChange={(event) => onChange(event.target.value)}
      placeholder={placeholder}
      aria-label={label}
      className="rounded-md px-3 py-2 text-sm"
      style={{
        background: "var(--color-surface-raised)",
        border: "1px solid var(--color-border-subtle)",
        color: "var(--color-ink)",
      }}
    />
  );
}

function Area({
  value,
  onChange,
  label,
  hint,
}: {
  value: string;
  onChange: (v: string) => void;
  label: string;
  hint: string;
}) {
  return (
    <label className="flex flex-col gap-1">
      <span className="text-xs" style={{ color: "var(--color-ink-muted)" }}>
        {label} — {hint}
      </span>
      <textarea
        value={value}
        onChange={(event) => onChange(event.target.value)}
        rows={8}
        aria-label={label}
        className="w-full resize-y rounded-md px-3 py-2 font-mono text-xs"
        style={{
          background: "var(--color-surface-raised)",
          border: "1px solid var(--color-border-subtle)",
          color: "var(--color-ink)",
        }}
      />
    </label>
  );
}

function Save({
  busy,
  disabled,
  onClick,
}: {
  busy: boolean;
  disabled: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      disabled={busy || disabled}
      onClick={onClick}
      className="self-start rounded-md px-4 py-2 text-sm font-medium disabled:opacity-30"
      style={{
        border: "1px solid var(--color-state-ok)",
        color: "var(--color-state-ok)",
      }}
    >
      Save as new version
    </button>
  );
}

function Muted({ children }: { children: React.ReactNode }) {
  return (
    <p className="text-sm" style={{ color: "var(--color-ink-muted)" }}>
      {children}
    </p>
  );
}

function Failed({ children }: { children: React.ReactNode }) {
  return (
    <p className="text-sm" style={{ color: "var(--color-state-failed)" }}>
      {children}
    </p>
  );
}
