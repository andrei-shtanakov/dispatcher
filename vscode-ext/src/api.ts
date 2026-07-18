/** Typed client for the dispatcher HTTP API. Must stay vscode-free. */

export interface Counts {
  tasks: number;
  models: number;
  test_results: number;
  errors: number;
}

export interface OverviewEntry {
  name: string;
  path: string | null;
  detected: boolean;
  freshness: string | null;
  counts: Partial<Counts>;
  warnings: string[];
}

export interface OverviewResponse {
  projects: OverviewEntry[];
  warnings: string[];
}

export interface ErrorEvent {
  timestamp: string | null;
  service: string | null;
  severity: string;
  body: string;
}

export interface SchemaVersionCheck {
  database: string;
  found: string | null;
  expected: string | null;
  ok: boolean | null;
}

export interface EvidenceResult {
  rule: string;
  kind: string; // implementation | verification
  passed: boolean;
  detail: string;
}

export interface RoadmapItemView {
  id: string;
  title: string;
  phase: string | null;
  owner_project: string | null;
  target_contract: string | null;
  depends_on: string[];
  expected_evidence: string[];
  computed_status: string;
  evidence: EvidenceResult[];
  blockers: string[];
  source: string;
}

export interface RoadmapResponse {
  roadmaps: string[];
  items: RoadmapItemView[];
  warnings: string[];
}

export interface ProjectDetail {
  name: string;
  path: string;
  detected: boolean;
  freshness: string | null;
  schema_versions: SchemaVersionCheck[];
  models: unknown[];
  tasks: unknown[];
  test_results: unknown[];
  configs: unknown[];
  errors: ErrorEvent[];
  warnings: string[];
}

export interface RepoVerdict {
  repo: string;
  verdict: string;
  reason: string | null;
  branch: string | null;
  ahead: number | null;
  behind: number | null;
  dirty: boolean;
  is_kb: boolean;
}

export interface HostPanel {
  host: string;
  source: string; // "live" | "kb"
  generated_at: string | null;
  age_seconds: number | null;
  stale: boolean;
  gh_error: string | null;
  error: string | null;
  verdicts: RepoVerdict[];
}

export interface SyncReportSummary {
  current_host: string;
  top_line: string; // ok | pull-first | no-data | unknown
  top_reason: string | null;
  hosts: HostPanel[];
  proposals: string[];
  warnings: string[];
}

export interface SyncStatusResponse {
  report: SyncReportSummary;
  fetch_in_flight: boolean;
  last_fetch_at: string | null;
  last_fetch_error: string | null;
}

export interface ActionOutcome {
  action: string;
  dir: string;
  ok: boolean;
  detail: string | null;
  error: string | null;
  pr_url: string | null;
}

export interface TypedField {
  value: unknown;
  explicit: boolean;
}

export interface SpecRunnerConfigEntry {
  project: string;
  project_yaml_path: string;
  base_mtime: number;
  typed: Record<string, TypedField>;
  extra_executor_config: Record<string, unknown>;
  extra_explicit: boolean;
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly detail: string,
  ) {
    super(detail);
  }
}

const TIMEOUT_MS = 3000;
const ACTION_TIMEOUT_MS = 130_000; // server subprocess cap is 120s

export class ApiClient {
  private token: string | null = null;

  constructor(private readonly baseUrl: string) {}

  private async raise(resp: Response, path: string): Promise<never> {
    let detail = `${path}: HTTP ${resp.status}`;
    try {
      const body = (await resp.json()) as { detail?: unknown };
      if (typeof body.detail === "string") {
        detail = body.detail;
      }
    } catch {
      // non-JSON body: keep the HTTP fallback
    }
    throw new ApiError(resp.status, detail);
  }

  private async get<T>(path: string): Promise<T> {
    const resp = await fetch(`${this.baseUrl}${path}`, {
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
    if (!resp.ok) {
      await this.raise(resp, `GET ${path}`);
    }
    return (await resp.json()) as T;
  }

  private async postJson(
    path: string,
    body: unknown,
    headers: Record<string, string>,
    timeoutMs: number = ACTION_TIMEOUT_MS,
  ): Promise<Response> {
    return fetch(`${this.baseUrl}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...headers },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(timeoutMs),
    });
  }

  private async fetchToken(): Promise<string> {
    const session = await this.get<{ token: string }>("/api/actions/session");
    this.token = session.token;
    return session.token;
  }

  private async postWithToken<T>(path: string, body: unknown): Promise<T> {
    const token = this.token ?? (await this.fetchToken());
    let resp = await this.postJson(path, body, { "X-Action-Token": token });
    if (resp.status === 403) {
      // process token rotated (server restart): refetch EXACTLY once
      const fresh = await this.fetchToken();
      resp = await this.postJson(path, body, { "X-Action-Token": fresh });
    }
    if (!resp.ok) {
      await this.raise(resp, `POST ${path}`);
    }
    return (await resp.json()) as T;
  }

  overview(): Promise<OverviewResponse> {
    return this.get("/api/overview");
  }

  project(name: string): Promise<ProjectDetail> {
    return this.get(`/api/projects/${encodeURIComponent(name)}`);
  }

  errors(): Promise<ErrorEvent[]> {
    return this.get("/api/errors?days=14&limit=50");
  }

  roadmap(): Promise<RoadmapResponse> {
    return this.get("/api/roadmap");
  }

  sync(): Promise<SyncStatusResponse> {
    return this.get("/api/sync");
  }

  pull(dir: string): Promise<ActionOutcome> {
    return this.postWithToken("/api/actions/pull", { dir });
  }

  createPr(dir: string): Promise<ActionOutcome> {
    return this.postWithToken("/api/actions/create-pr", { dir });
  }

  async track(
    dir: string,
    action: "track" | "ignore",
  ): Promise<{ tracked: string[]; ignored: string[] }> {
    // lightweight local TOML write server-side — short GET-class timeout,
    // not the 130s action timeout (no subprocess behind this route)
    const resp = await this.postJson(
      "/api/sync/track",
      { dir, action },
      {},
      TIMEOUT_MS,
    );
    if (!resp.ok) {
      await this.raise(resp, "POST /api/sync/track");
    }
    return (await resp.json()) as { tracked: string[]; ignored: string[] };
  }

  specRunnerConfigs(): Promise<SpecRunnerConfigEntry[]> {
    return this.get("/api/spec-runner-configs");
  }

  updateSpecRunnerConfig(body: {
    dir: string;
    typed: Record<string, unknown>;
    extra_executor_config: null;
    base_mtime: number;
  }): Promise<ActionOutcome> {
    return this.postWithToken("/api/actions/update-spec-runner-config", body);
  }
}
