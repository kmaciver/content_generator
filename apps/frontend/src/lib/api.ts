// Client-side API types and fetch helpers.
//
// These mirror the backend DTOs (apps/backend/src/videoforge/dto). They are
// hand-written rather than generated: finding S8's codegen toolchain was
// withdrawn (ADR-008) once the renderer became Python, and for a handful of
// read-mostly shapes a generator is more machinery than the drift it prevents.
// The API tests are what actually pin the contract.
//
// Note what is NOT here: any rule about when a button is enabled. That lives
// in `capabilities`, which the server computes from the domain FSM (§11).
// Reimplementing it in TypeScript is how the two drift, and a disabled-looking
// Approve button that 409s is worse than no button.

export interface Capabilities {
  can_approve: boolean;
  can_reject: boolean;
  can_regenerate: boolean;
  can_edit: boolean;
}

export type VersionStatus =
  "AWAITING_APPROVAL" | "APPROVED" | "REJECTED" | "SUPERSEDED";

export interface VersionSummary {
  id: string;
  version_no: number;
  origin: string;
  status: VersionStatus;
  created_at: string;
  created_by: string | null;
  prompt_template_ref: string | null;
  provider_ref: string | null;
}

export interface VersionDetail extends VersionSummary {
  content: Record<string, unknown> | null;
  storage_key: string | null;
  content_hash: string;
  meta: Record<string, unknown>;
  parent_version_id: string | null;
  /** Where the bytes are, when the version has any. Built by the server
   * (ADR-011): which bucket a kind lives in is a server fact, and a client
   * composing that path is a second place that has to change when it moves —
   * which is exactly how the render's URL was wrong before M4-11. */
  asset_url: string | null;
  /** Captions as the render will burn them, for versions that carry word
   * timings. Grouped by the server from one implementation (`group_into_cues`)
   * that the timeline compiler also reads — grouping here in TypeScript would
   * be a second set of rules, and the preview drifting from the finished video
   * is exactly what that costs. Empty for kinds that have no timings. */
  caption_cues: CaptionCue[];
}

export interface CaptionCue {
  text: string;
  start_ms: number;
  end_ms: number;
}

/** One entry of a publishing package's manifest (M5-03).
 *
 * The `sha256` is the point: a zip already lists its own names, so what the
 * manifest adds is the ability to *verify* a downloaded archive rather than
 * trust it — ADR-004's rule carried past the boundary where the bytes leave
 * the system. */
export interface PackageFile {
  path: string;
  sha256: string;
  bytes: number;
}

export interface ArtifactSummary {
  id: string;
  kind: string;
  scene_ref: string | null;
  state: string;
  current_version_no: number;
  stale_since: string | null;
  capabilities: Capabilities;
  /** Which rejection reasons apply to this *kind* of artifact — server-owned,
   * like `capabilities`, for the same reason: the rule lives in the domain and
   * the client renders what it is given. Empty means comment-only. Every
   * reason in the vocabulary describes a picture, so a narration, a script and
   * a timeline all get none; rendering one hardcoded list offered "Anatomy"
   * and "Text in image" on a voice take. */
  rejection_reasons: RejectionReason[];
}

export interface ArtifactDetail extends ArtifactSummary {
  versions: VersionSummary[];
}

export interface ProjectSummary {
  id: string;
  topic: string;
  title: string | null;
  phase: string;
  created_at: string;
}

/** One pipeline stage, as the server computes it (M2-13).
 *
 * The DAG lives on the server (ADR-009) and stays there. Reimplementing the
 * dependency graph here to decide whether "Generate scenes" is enabled is the
 * same drift `capabilities` exists to prevent, one level up — so the server
 * sends the answer *and* the reason.
 */
export interface StageSummary {
  kind: string;
  queue: string;
  state: string | null;
  artifact_id: string | null;
  stale_since: string | null;
  requires: string[];
  /** Not-yet-approved requirements. Empty means runnable. */
  unmet: string[];
  can_generate: boolean;
}

/** A scene of the approved scene set, for the per-scene review selector. */
export interface SceneSummary {
  id: string;
  index: number;
  narration: string;
  /** M4-01 (§1.0.3). A `card` scene has no generated image — it renders
   * locally from `card_text` — so the UI must not offer it a Regenerate that
   * the worker refuses. */
  kind: "illustration" | "card";
  card_text: string | null;
}

/** One cell of the contact sheet (M3-09). */
export interface ContactTile {
  scene_id: string;
  scene_index: number;
  narration: string;
  artifact_id: string | null;
  state: string | null;
  stale_since: string | null;
  version_id: string | null;
  version_no: number | null;
  /** Already a servable path — the client never composes bucket names. */
  asset_url: string | null;
  capabilities: Partial<Capabilities>;
}

/** The per-scene set of one kind, as a grid.
 *
 * `pending_version_ids` is the batch "approve all remaining" submits. It comes
 * from the server for the same reason `capabilities` does: which versions may
 * be approved is the FSM's answer, and filtering the list here would be a
 * second copy of that rule.
 */
export interface ContactSheet {
  kind: string;
  tiles: ContactTile[];
  total: number;
  pending: number;
  pending_version_ids: string[];
}

/** The rejection vocabulary (M3-10).
 *
 * Mirrors `videoforge_domain.rejection.RejectionReason`. Hand-written like
 * every other type here; the API rejects an unknown value with a 400, which is
 * what actually pins the contract.
 */
export const REJECTION_REASONS = [
  "character_drift",
  "style_drift",
  "composition",
  "text_artifacts",
  "anatomy",
  "extra_subjects",
  "off_brief",
  "quality",
  "other",
] as const;

export type RejectionReason = (typeof REJECTION_REASONS)[number];

/** Short labels for the chips. The server owns the *meaning*; these are only
 * how it reads on a button. */
export const REJECTION_LABELS: Record<RejectionReason, string> = {
  character_drift: "Character wrong",
  style_drift: "Wrong style",
  composition: "Framing",
  text_artifacts: "Text in image",
  anatomy: "Anatomy",
  extra_subjects: "Extra things",
  off_brief: "Off brief",
  quality: "Poor quality",
  other: "Other",
};

/** Series branding (M3-13). A separate surface from the project review screen
 * for ADR-016's reason: character and style are not stages a project produces,
 * they are preconditions it consumes. */
export interface SeriesSummary {
  id: string;
  title: string;
  created_at: string;
}

export interface ReferenceSummary {
  id: string;
  index: number;
  /** A key, never bytes — assets come from nginx via X-Accel-Redirect. */
  storage_key: string;
  pose: string;
  angle: string;
  expression: string;
  shot_type: string;
}

export type BrandingStatus =
  "PENDING" | "AWAITING_APPROVAL" | "APPROVED" | "SUPERSEDED";

export interface CharacterSummary {
  id: string;
  version_no: number;
  name: string;
  status: BrandingStatus;
  immutable_traits: Record<string, unknown>;
  variable_traits: Record<string, unknown>;
  approved_reference_group_id: string | null;
  created_at: string;
}

export interface StyleSummary {
  id: string;
  version_no: number;
  name: string;
  status: BrandingStatus;
  fields: Record<string, unknown>;
  prompt_block: string;
  created_at: string;
}

/** `ready` and `missing` are the server's answer to "can this series generate
 * images yet". Same contract as `capabilities`: the UI renders the decision,
 * it does not re-derive it. */
export interface BrandingDetail {
  series_id: string;
  character: CharacterSummary | null;
  style: StyleSummary | null;
  references: ReferenceSummary[];
  characters: CharacterSummary[];
  styles: StyleSummary[];
  ready: boolean;
  missing: string[];
}

export interface BatchReviewResult {
  approved: number;
  skipped: { version_id: string; reason: string }[];
}

export interface ProjectDetail extends ProjectSummary {
  series_id: string | null;
  active_pointers: Record<string, string>;
  artifacts: ArtifactSummary[];
  stages: StageSummary[];
  scenes: SceneSummary[];
}

export interface JobResponse {
  id: string;
  status: string;
  task_name: string;
  queue: string;
  attempt: number;
  max_attempts: number;
  error: Record<string, unknown> | null;
}

/** RFC-9457 problem+json — the single error shape the API and BFF both emit. */
export interface Problem {
  title: string;
  status: number;
  detail?: string;
  correlation_id?: string;
}

export class ApiError extends Error {
  readonly status: number;
  readonly correlationId?: string;

  constructor(problem: Problem) {
    super(problem.detail ?? problem.title);
    this.status = problem.status;
    this.correlationId = problem.correlation_id;
  }
}

async function call<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`/api/bff${path}`, {
    ...init,
    headers: {
      Accept: "application/json",
      ...(init.body ? { "Content-Type": "application/json" } : {}),
      ...init.headers,
    },
  });

  // 204 has no body, and `response.json()` on an empty one throws
  // `Unexpected end of JSON input` — which would surface as a parse error on a
  // request that succeeded. Checked before parsing rather than caught after,
  // so a genuinely malformed body still fails loudly.
  const payload =
    response.status === 204 || response.headers.get("content-length") === "0"
      ? null
      : ((await response.json()) as unknown);

  if (!response.ok) {
    // Surface the server's own message. Inventing a friendlier one client-side
    // would discard the correlation id, which is the only thing that connects
    // a user's screenshot to the server log that explains it.
    throw new ApiError(payload as Problem);
  }
  return payload as T;
}

export const api = {
  listProjects: () => call<{ items: ProjectSummary[] }>("/projects"),

  /** `seriesId` is optional to the server and effectively mandatory to a user
   * who wants images: ADR-016 makes an approved character and style a
   * *precondition* a project consumes, resolved from its series, and a project
   * with no series is refused at image admission with a 409 that points at a
   * screen it cannot reach. Found by M4-12 — the create form sent a topic and
   * nothing else, so no project created through the UI could ever be
   * illustrated. */
  createProject: (topic: string, seriesId?: string | null) =>
    call<ProjectSummary>("/projects", {
      method: "POST",
      body: JSON.stringify(
        seriesId ? { topic, series_id: seriesId } : { topic },
      ),
    }),

  getProject: (id: string) => call<ProjectDetail>(`/projects/${id}`),

  /** **The only destroying call in this client.** Everything else appends —
   * a rejection is a row, a regeneration is a new version. Cascades to every
   * artifact, version, scene, job and package; leaves the audit trail (§10.3)
   * and the stored bytes (shared between projects, ADR-004). 204, no body. */
  deleteProject: (id: string) =>
    call<null>(`/projects/${id}`, { method: "DELETE" }),

  getArtifact: (id: string) => call<ArtifactDetail>(`/artifacts/${id}`),

  getVersion: (artifactId: string, versionNo: number) =>
    call<VersionDetail>(`/artifacts/${artifactId}/versions/${versionNo}`),

  generate: (
    projectId: string,
    stage: string,
    regenerate = false,
    sceneId?: string,
  ) =>
    call<{ job_id: string; created: boolean }>(
      `/projects/${projectId}/generations`,
      {
        method: "POST",
        // `scene_id` narrows a per-scene stage to one tile, which is what the
        // contact sheet's per-item Regenerate needs: the ones that miss should
        // not cost a re-run of the nineteen that landed.
        body: JSON.stringify({
          stage,
          regenerate,
          ...(sceneId ? { scene_id: sceneId } : {}),
        }),
      },
    ),

  getJob: (id: string) => call<JobResponse>(`/jobs/${id}`),

  /** Free a stage whose job will never finish (M5-05).
   *
   * Keyed on the artifact because that is what this client has — a job id
   * never reaches the browser. Lands the artifact in FAILED, which is
   * retryable, so the ordinary Regenerate button is the next step and there is
   * no second retry path to keep in step with the first. */
  release: (artifactId: string) =>
    call<ArtifactSummary>(`/artifacts/${artifactId}/release`, {
      method: "POST",
    }),

  approve: (versionId: string, expectedVersionNo: number, comment?: string) =>
    call<ArtifactSummary>(`/artifact-versions/${versionId}/reviews/approve`, {
      method: "POST",
      // expected_version_no is always sent: it is what stops an approval
      // landing on content that changed while the reviewer was reading.
      body: JSON.stringify({
        expected_version_no: expectedVersionNo,
        comment,
      }),
    }),

  reject: (
    versionId: string,
    expectedVersionNo: number,
    comment?: string,
    reasons: RejectionReason[] = [],
  ) =>
    call<ArtifactSummary>(`/artifact-versions/${versionId}/reviews/reject`, {
      method: "POST",
      body: JSON.stringify({
        expected_version_no: expectedVersionNo,
        comment,
        // Structured reasons (M3-10). These are what the next attempt's
        // correction block is built from, so a rejection with none tells the
        // model nothing it did not already know.
        reasons,
      }),
    }),

  edit: (artifactId: string, content: Record<string, unknown>) =>
    call<VersionDetail>(`/artifacts/${artifactId}/content`, {
      method: "PUT",
      body: JSON.stringify({ content }),
    }),

  getContactSheet: (projectId: string, kind: string) =>
    call<ContactSheet>(`/projects/${projectId}/contact-sheet/${kind}`),

  approveRemaining: (
    projectId: string,
    versionIds: string[],
    comment?: string,
  ) =>
    call<BatchReviewResult>(
      `/projects/${projectId}/reviews/approve-remaining`,
      {
        method: "POST",
        // The ids the grid actually displayed, not "everything pending". A
        // server-side sweep would catch a scene that regenerated mid-scroll —
        // the failure expected_version_no prevents, twenty at a time.
        body: JSON.stringify({ version_ids: versionIds, comment }),
      },
    ),

  listSeries: () => call<SeriesSummary[]>("/series"),

  getBranding: (seriesId: string) =>
    call<BrandingDetail>(`/series/${seriesId}/branding`),

  createCharacter: (
    seriesId: string,
    body: {
      name: string;
      immutable_traits: Record<string, unknown>;
      variable_traits: Record<string, unknown>;
    },
  ) =>
    call<CharacterSummary>(`/series/${seriesId}/characters`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  approveCharacter: (characterId: string, referenceGroupId?: string) =>
    call<CharacterSummary>(`/characters/${characterId}/approve`, {
      method: "POST",
      body: JSON.stringify(
        referenceGroupId ? { reference_group_id: referenceGroupId } : {},
      ),
    }),

  generateReferences: (characterId: string) =>
    call<{ job_id: string; created: boolean; group_id: string }>(
      `/characters/${characterId}/references`,
      { method: "POST" },
    ),

  createStyle: (
    seriesId: string,
    body: { name: string; fields: Record<string, unknown> },
  ) =>
    call<StyleSummary>(`/series/${seriesId}/styles`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  approveStyle: (styleId: string) =>
    call<StyleSummary>(`/styles/${styleId}/approve`, {
      method: "POST",
      body: JSON.stringify({}),
    }),

  addComment: (versionId: string, body: string) =>
    call<{ status: string }>(`/artifact-versions/${versionId}/comments`, {
      method: "POST",
      body: JSON.stringify({ body }),
    }),

  listComments: (versionId: string) =>
    call<{ items: { id: string; body: string; created_at: string }[] }>(
      `/artifact-versions/${versionId}/comments`,
    ),
};
