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
}

export interface ArtifactSummary {
  id: string;
  kind: string;
  scene_ref: string | null;
  state: string;
  current_version_no: number;
  stale_since: string | null;
  capabilities: Capabilities;
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

export interface ProjectDetail extends ProjectSummary {
  series_id: string | null;
  active_pointers: Record<string, string>;
  artifacts: ArtifactSummary[];
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

  const payload = (await response.json()) as unknown;
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

  createProject: (topic: string) =>
    call<ProjectSummary>("/projects", {
      method: "POST",
      body: JSON.stringify({ topic }),
    }),

  getProject: (id: string) => call<ProjectDetail>(`/projects/${id}`),

  getArtifact: (id: string) => call<ArtifactDetail>(`/artifacts/${id}`),

  getVersion: (artifactId: string, versionNo: number) =>
    call<VersionDetail>(`/artifacts/${artifactId}/versions/${versionNo}`),

  generate: (projectId: string, stage: string, regenerate = false) =>
    call<{ job_id: string; created: boolean }>(
      `/projects/${projectId}/generations`,
      { method: "POST", body: JSON.stringify({ stage, regenerate }) },
    ),

  getJob: (id: string) => call<JobResponse>(`/jobs/${id}`),

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

  reject: (versionId: string, expectedVersionNo: number, comment?: string) =>
    call<ArtifactSummary>(`/artifact-versions/${versionId}/reviews/reject`, {
      method: "POST",
      body: JSON.stringify({
        expected_version_no: expectedVersionNo,
        comment,
      }),
    }),

  edit: (artifactId: string, content: Record<string, unknown>) =>
    call<VersionDetail>(`/artifacts/${artifactId}/content`, {
      method: "PUT",
      body: JSON.stringify({ content }),
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
