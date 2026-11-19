"use client";

import { useRef, useState } from "react";
import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";

import {
  api,
  type StudioGeneration,
  type StudioOptions,
  type StudioReference,
} from "@/lib/api";

/** Image Studio (ADR-018): a prompt box, a few settings, and a gallery.
 *
 * Nothing here decides what is allowed — models, ratios and limits come from
 * `/studio/options`, and the server validates every request against the same
 * table. The form only offers what the server will accept.
 *
 * Polling, not push (ADR-006/017): the gallery refetches every two seconds
 * while anything on the first page is queued or running, and stops otherwise.
 */
const POLL_MS = 2000;
const IN_FLIGHT = new Set(["QUEUED", "RUNNING"]);

export function Studio() {
  const options = useQuery({
    queryKey: ["studio", "options"],
    queryFn: api.getStudioOptions,
    staleTime: Infinity,
  });

  if (options.isPending) return <Muted>Loading…</Muted>;
  if (options.error) return <Failed>{options.error.message}</Failed>;
  return <StudioReady options={options.data} />;
}

interface FormState {
  prompt: string;
  model: string;
  aspectRatio: string;
  count: number;
  references: StudioReference[];
}

function StudioReady({ options }: { options: StudioOptions }) {
  const queryClient = useQueryClient();
  const promptRef = useRef<HTMLTextAreaElement>(null);
  const [form, setForm] = useState<FormState>({
    prompt: "",
    model: options.models[0]?.id ?? "",
    aspectRatio: options.aspect_ratios[0] ?? "1:1",
    count: 1,
    references: [],
  });
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [uploading, setUploading] = useState(0);

  const gallery = useInfiniteQuery({
    queryKey: ["studio", "generations"],
    queryFn: ({ pageParam }) => api.listStudioGenerations(pageParam),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    refetchInterval: (query) =>
      query.state.data?.pages[0]?.items.some((g) => IN_FLIGHT.has(g.status))
        ? POLL_MS
        : false,
  });

  const generate = useMutation({
    mutationFn: () =>
      api.createStudioGeneration({
        prompt: form.prompt,
        model: form.model,
        aspect_ratio: form.aspectRatio,
        count: form.count,
        references: form.references.map((r) => r.storage_key),
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: ["studio", "generations"],
      });
    },
  });

  const model = options.models.find((m) => m.id === form.model);
  const estimate = model
    ? (Number(model.price_per_image) * form.count).toFixed(2)
    : null;
  const canSubmit =
    form.prompt.trim().length > 0 && !generate.isPending && uploading === 0;

  const submit = () => {
    if (canSubmit) generate.mutate();
  };

  const addFiles = async (files: FileList | null) => {
    if (!files) return;
    setUploadError(null);
    const room = options.max_references - form.references.length;
    const chosen = Array.from(files).slice(0, Math.max(0, room));
    if (files.length > chosen.length) {
      setUploadError(
        `You can attach up to ${options.max_references} reference images.`,
      );
    }
    for (const file of chosen) {
      if (!options.reference_mime_types.includes(file.type)) {
        setUploadError(`${file.name}: use a PNG, JPEG or WebP image.`);
        continue;
      }
      if (file.size > options.max_reference_bytes) {
        const mb = Math.round(options.max_reference_bytes / 1_048_576);
        setUploadError(`${file.name} is larger than ${mb} MB.`);
        continue;
      }
      setUploading((n) => n + 1);
      try {
        const uploaded = await api.uploadStudioReference(
          file.type,
          await toBase64(file),
        );
        setForm((f) =>
          f.references.some((r) => r.storage_key === uploaded.storage_key)
            ? f
            : { ...f, references: [...f.references, uploaded] },
        );
      } catch (error) {
        setUploadError(
          error instanceof Error ? error.message : "Upload failed.",
        );
      } finally {
        setUploading((n) => n - 1);
      }
    }
  };

  const attachReference = (reference: StudioReference) => {
    setForm((f) =>
      f.references.length >= options.max_references ||
      f.references.some((r) => r.storage_key === reference.storage_key)
        ? f
        : { ...f, references: [...f.references, reference] },
    );
    window.scrollTo({ top: 0, behavior: "smooth" });
  };

  const reuse = (generation: StudioGeneration) => {
    setForm({
      prompt: generation.prompt,
      model: options.models.some((m) => m.id === generation.model)
        ? generation.model
        : form.model,
      aspectRatio: generation.aspect_ratio,
      count: Math.min(generation.count, options.max_count),
      references: generation.references.map((key, i) => ({
        storage_key: key,
        asset_url: generation.reference_urls[i] ?? "",
      })),
    });
    window.scrollTo({ top: 0, behavior: "smooth" });
    promptRef.current?.focus();
  };

  const generations = gallery.data?.pages.flatMap((p) => p.items) ?? [];

  return (
    <div className="flex flex-col gap-10">
      <form
        className="flex flex-col gap-5 rounded-lg p-4 sm:p-5"
        style={{
          background: "var(--color-surface-raised)",
          border: "1px solid var(--color-border-subtle)",
        }}
        onSubmit={(event) => {
          event.preventDefault();
          submit();
        }}
      >
        <label className="flex flex-col gap-2">
          <span className="text-sm font-medium">Prompt</span>
          <textarea
            ref={promptRef}
            value={form.prompt}
            onChange={(e) => setForm({ ...form, prompt: e.target.value })}
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                e.preventDefault();
                submit();
              }
            }}
            rows={4}
            placeholder="A watercolour of a fox asleep in a field of lavender, soft morning light"
            className="w-full resize-y rounded-md px-3 py-2 text-base sm:text-sm"
            style={{
              background: "var(--color-surface)",
              border: "1px solid var(--color-border-subtle)",
              color: "var(--color-ink)",
            }}
          />
        </label>

        <Field label="Model">
          <div className="flex flex-wrap gap-2">
            {options.models.map((m) => (
              <Choice
                key={m.id}
                selected={form.model === m.id}
                onClick={() => setForm({ ...form, model: m.id })}
                title={m.description}
              >
                <span className="font-medium">{m.label}</span>
                <span style={{ color: "var(--color-ink-muted)" }}>
                  {" "}
                  · ~${m.price_per_image}/image
                </span>
              </Choice>
            ))}
          </div>
          {model ? <Hint>{model.description}</Hint> : null}
        </Field>

        <div className="flex flex-col gap-5 sm:flex-row sm:gap-8">
          <Field label="Shape">
            <div className="flex flex-wrap gap-2">
              {options.aspect_ratios.map((ratio) => (
                <Choice
                  key={ratio}
                  selected={form.aspectRatio === ratio}
                  onClick={() => setForm({ ...form, aspectRatio: ratio })}
                  title={describeRatio(ratio)}
                >
                  <RatioIcon ratio={ratio} />
                  <span className="tabular-nums">{ratio}</span>
                </Choice>
              ))}
            </div>
          </Field>

          <Field label="How many">
            <div className="flex flex-wrap gap-2">
              {Array.from({ length: options.max_count }, (_, i) => i + 1).map(
                (n) => (
                  <Choice
                    key={n}
                    selected={form.count === n}
                    onClick={() => setForm({ ...form, count: n })}
                  >
                    <span className="tabular-nums">{n}</span>
                  </Choice>
                ),
              )}
            </div>
          </Field>
        </div>

        <Field label="Reference images (optional)">
          <Hint>
            Attach photos or earlier results to guide the look, a person, or an
            object. Up to {options.max_references}.
          </Hint>
          <div className="flex flex-wrap items-center gap-2">
            {form.references.map((ref) => (
              <div key={ref.storage_key} className="relative">
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img
                  src={ref.asset_url}
                  alt="Reference"
                  className="size-16 rounded-md object-cover"
                  style={{ border: "1px solid var(--color-border-subtle)" }}
                />
                <button
                  type="button"
                  aria-label="Remove reference"
                  onClick={() =>
                    setForm({
                      ...form,
                      references: form.references.filter(
                        (r) => r.storage_key !== ref.storage_key,
                      ),
                    })
                  }
                  className="absolute -top-2 -right-2 grid size-6 place-items-center rounded-full text-xs"
                  style={{
                    background: "var(--color-surface)",
                    border: "1px solid var(--color-border-subtle)",
                  }}
                >
                  ✕
                </button>
              </div>
            ))}
            {uploading > 0 ? (
              <div
                className="grid size-16 place-items-center rounded-md text-xs"
                style={{
                  border: "1px dashed var(--color-border-subtle)",
                  color: "var(--color-ink-muted)",
                }}
              >
                Uploading…
              </div>
            ) : null}
            {form.references.length < options.max_references ? (
              <label
                className="grid size-16 cursor-pointer place-items-center rounded-md text-2xl"
                style={{
                  border: "1px dashed var(--color-border-subtle)",
                  color: "var(--color-ink-muted)",
                }}
              >
                +<span className="sr-only">Add reference images</span>
                <input
                  type="file"
                  accept={options.reference_mime_types.join(",")}
                  multiple
                  className="sr-only"
                  onChange={(e) => {
                    void addFiles(e.target.files);
                    e.target.value = "";
                  }}
                />
              </label>
            ) : null}
          </div>
          {uploadError ? <Failed>{uploadError}</Failed> : null}
        </Field>

        <div className="flex flex-wrap items-center gap-4">
          <button
            type="submit"
            disabled={!canSubmit}
            className="rounded-md px-5 py-2.5 text-sm font-medium disabled:opacity-40"
            style={{
              background: "var(--color-ink)",
              color: "var(--color-surface)",
            }}
          >
            {generate.isPending
              ? "Sending…"
              : `Generate ${form.count} image${form.count === 1 ? "" : "s"}`}
          </button>
          {estimate ? (
            <span
              className="text-xs"
              style={{ color: "var(--color-ink-muted)" }}
            >
              About ${estimate} · ⌘/Ctrl + Enter
            </span>
          ) : null}
        </div>
        {generate.error ? <Failed>{generate.error.message}</Failed> : null}
      </form>

      <section className="flex flex-col gap-6" aria-label="Your images">
        <h2 className="text-lg font-semibold tracking-tight">Your images</h2>
        {gallery.isPending ? <Muted>Loading…</Muted> : null}
        {gallery.error ? <Failed>{gallery.error.message}</Failed> : null}
        {gallery.data && generations.length === 0 ? (
          <Muted>Nothing yet. Your images will appear here.</Muted>
        ) : null}
        {generations.map((g) => (
          <GenerationCard
            key={g.id}
            generation={g}
            modelLabel={
              options.models.find((m) => m.id === g.model)?.label ?? g.model
            }
            onReuse={() => reuse(g)}
            onAttachReference={attachReference}
          />
        ))}
        {gallery.hasNextPage ? (
          <button
            type="button"
            onClick={() => void gallery.fetchNextPage()}
            disabled={gallery.isFetchingNextPage}
            className="self-center rounded-md px-4 py-2 text-sm"
            style={{ border: "1px solid var(--color-border-subtle)" }}
          >
            {gallery.isFetchingNextPage ? "Loading…" : "Show older"}
          </button>
        ) : null}
      </section>
    </div>
  );
}

function GenerationCard({
  generation: g,
  modelLabel,
  onReuse,
  onAttachReference,
}: {
  generation: StudioGeneration;
  modelLabel: string;
  onReuse: () => void;
  onAttachReference: (ref: StudioReference) => void;
}) {
  const busy = IN_FLIGHT.has(g.status);
  const [w, h] = g.aspect_ratio.split(":").map(Number);
  const ratio = w && h ? `${w} / ${h}` : "1 / 1";

  return (
    <article
      className="flex flex-col gap-3 rounded-lg p-4"
      style={{
        background: "var(--color-surface-raised)",
        border: "1px solid var(--color-border-subtle)",
      }}
    >
      <div className="flex flex-col gap-1 sm:flex-row sm:items-start sm:justify-between sm:gap-4">
        <p className="text-sm whitespace-pre-wrap">{g.prompt}</p>
        <button
          type="button"
          onClick={onReuse}
          className="shrink-0 self-start text-xs underline underline-offset-2"
          style={{ color: "var(--color-ink-muted)" }}
        >
          Reuse prompt & settings
        </button>
      </div>
      <p className="text-xs" style={{ color: "var(--color-ink-muted)" }}>
        {modelLabel} · {g.aspect_ratio} · {g.count} image
        {g.count === 1 ? "" : "s"}
        {g.references.length
          ? ` · ${g.references.length} reference${g.references.length === 1 ? "" : "s"}`
          : ""}{" "}
        · {new Date(g.created_at).toLocaleString()}
      </p>

      {g.status === "FAILED" ? (
        <Failed>
          This didn&rsquo;t work: {g.error ?? "the generation did not complete"}
          . If it mentions safety, try rewording the prompt.
        </Failed>
      ) : null}

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        {g.images.map((image) => (
          <figure key={image.id} className="flex flex-col gap-1.5">
            <a href={image.asset_url} target="_blank" rel="noreferrer">
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img
                src={image.asset_url}
                alt={`${g.prompt.slice(0, 80)} (${image.index})`}
                loading="lazy"
                className="w-full rounded-md object-cover"
                style={{ aspectRatio: ratio }}
              />
            </a>
            <figcaption className="flex gap-3 text-xs">
              <a
                href={image.asset_url}
                download={`studio-${g.id}-${image.index}${extension(image.mime_type)}`}
                className="underline underline-offset-2"
              >
                Download
              </a>
              <button
                type="button"
                className="underline underline-offset-2"
                style={{ color: "var(--color-ink-muted)" }}
                onClick={() =>
                  onAttachReference({
                    storage_key: image.storage_key,
                    asset_url: image.asset_url,
                  })
                }
              >
                Use as reference
              </button>
            </figcaption>
          </figure>
        ))}
        {busy
          ? Array.from(
              { length: Math.max(0, g.count - g.images.length) },
              (_, i) => (
                <div
                  key={`pending-${i}`}
                  className="grid animate-pulse place-items-center rounded-md text-xs"
                  style={{
                    aspectRatio: ratio,
                    background: "var(--color-surface)",
                    border: "1px dashed var(--color-border-subtle)",
                    color: "var(--color-ink-muted)",
                  }}
                >
                  {g.status === "QUEUED" ? "Waiting…" : "Drawing…"}
                </div>
              ),
            )
          : null}
      </div>
    </article>
  );
}

function Field({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <fieldset className="flex flex-col gap-2">
      <legend className="mb-2 text-sm font-medium">{label}</legend>
      {children}
    </fieldset>
  );
}

function Choice({
  selected,
  onClick,
  title,
  children,
}: {
  selected: boolean;
  onClick: () => void;
  title?: string;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      aria-pressed={selected}
      onClick={onClick}
      title={title}
      className="flex min-h-9 items-center gap-1.5 rounded-md px-3 py-1.5 text-sm"
      style={{
        border: `1px solid ${selected ? "var(--color-ink)" : "var(--color-border-subtle)"}`,
        background: selected ? "var(--color-surface)" : "transparent",
      }}
    >
      {children}
    </button>
  );
}

function RatioIcon({ ratio }: { ratio: string }) {
  const [w = 1, h = 1] = ratio.split(":").map(Number);
  const scale = 14 / Math.max(w, h);
  return (
    <span
      aria-hidden
      className="inline-block rounded-[2px]"
      style={{
        width: `${w * scale}px`,
        height: `${h * scale}px`,
        border: "1.5px solid currentColor",
      }}
    />
  );
}

function describeRatio(ratio: string): string {
  const [w = 1, h = 1] = ratio.split(":").map(Number);
  if (w === h) return "Square";
  return w > h ? "Landscape" : "Portrait";
}

function extension(mime: string): string {
  return mime === "image/jpeg"
    ? ".jpg"
    : mime === "image/webp"
      ? ".webp"
      : ".png";
}

function toBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = String(reader.result);
      resolve(result.slice(result.indexOf(",") + 1));
    };
    reader.onerror = () => reject(reader.error ?? new Error("read failed"));
    reader.readAsDataURL(file);
  });
}

function Hint({ children }: { children: React.ReactNode }) {
  return (
    <p className="text-xs" style={{ color: "var(--color-ink-muted)" }}>
      {children}
    </p>
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
    <p
      role="alert"
      className="text-sm"
      style={{ color: "var(--color-state-failed)" }}
    >
      {children}
    </p>
  );
}
