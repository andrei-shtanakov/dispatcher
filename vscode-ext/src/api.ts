/** Typed client for the dispatcher HTTP API. Must stay vscode-free. */

import type { OnboardingView } from "./onboarding";
import type { ProductProposalsReport } from "./productProposals";
import type { RunsSnapshot } from "./runs";

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
  // status with the `(attested)` marker folded in (ADR-ECO-005 D3); render
  // this, not computed_status, so attestation provenance stays visible.
  status_label: string;
  // implementation rests only on owner attestation, never machine verification.
  implementation_is_attested_only: boolean;
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
  top_line: string; // ok | sync-first | no-data | unknown
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

export interface BenchmarkInfo {
  id: number;
  name: string;
  description: string;
  tasks_count: number;
  tags: string[];
  version: string;
  family_tag: string | null;
  created_at: string;
}

export interface LeaderboardRow {
  user_id: number;
  agent_name: string;
  best_score: number;
  run_count: number;
}

export interface LeaderboardState {
  status: string; // ok | unavailable | unreadable
  rows: LeaderboardRow[];
  error: string | null;
}

export interface BenchmarksReport {
  status: string; // unconfigured | ok | unavailable | unreadable
  url: string | null;
  fetched_at: string | null;
  error: string | null;
  benchmarks: BenchmarkInfo[];
  leaderboards: Record<string, LeaderboardState>;
}

export interface BenchmarksStatusResponse {
  report: BenchmarksReport;
  fetch_in_flight: boolean;
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

// Human queue (GET /api/human-queue — spec 2026-09-29-human-queue-a1-design §4).
export type SourceState =
  | "ok"
  | "partial"
  | "unavailable"
  | "not_configured"
  | "not_connected";

export interface SourceStatus {
  state: SourceState;
  detail: string | null;
}

export type WaitReason =
  | "launch_unknown"
  | "run_needs_review"
  | "run_awaiting_approval"
  | "loop_needs_human"
  | "proposal_gate"
  | "backlog_gate"
  | "pr_human_merge";

export type WaitAct =
  | { kind: "run_view"; request_id: string }
  | {
      kind: "maestro_verb";
      verb: "retry" | "approve";
      task_id: string;
      run_id: string;
      repo_key: string;
      maestro_home?: string;
      atp_catalog?: string | null;
      maestro_cli?: string | null;
    }
  | { kind: "open_artifact"; path: string }
  | {
      kind: "human_merge";
      repo: string;
      number: number;
      url: string;
      head_sha: string | null;
    };

export interface HumanWait {
  key: string;
  reasons: WaitReason[];
  source: string;
  repo: string | null;
  ref: string;
  title: string;
  since: string | null;
  since_basis: string | null;
  act: WaitAct;
}

export interface HumanQueueView {
  waits: HumanWait[];
  sources: Record<string, SourceStatus>;
  complete: boolean;
  generated_at: string;
}

// Factory floor (GET /api/factory-floor — spec 2026-09-30-factory-floor-c1-design).
export interface RunEndAct {
  kind: "maestro_run_end";
  run_id: string;
  repo_key: string;
  maestro_home: string;
  maestro_cli: string | null;
}

export interface InFlightRun {
  repo_key: string;
  run_id: string;
  status: "running" | "suspended" | "interrupted";
  started_at: string | null;
  last_activity_at: string | null;
  request_id: string | null;
  work_id: string | null;
  stale: boolean;
  act: RunEndAct | null;
}

/** A PR the agent merged inside the window (slice C2). */
export interface AgentMerge {
  repo: string;
  number: number;
  title: string;
  url: string;
  merged_at: string;
}

export interface FactoryFloorView {
  in_flight: InFlightRun[];
  // Optional: a server older than C2 does not send them.
  agent_merges?: AgentMerge[];
  agent_merge_login?: string | null;
  merges_window_hours?: number;
  sources: Record<string, SourceStatus>;
  complete: boolean;
  generated_at: string;
}

// Halt (GET/POST /api/halt — spec 2026-09-30-halt-d1-design).
export type HaltState = "on" | "off" | "missing" | "misconfigured" | "unknown";

export interface RepoHalt {
  repo: string;
  state: HaltState;
  ruleset_id: number | null;
  detail: string | null;
  read_at?: string | null;
}

export interface HaltResult {
  repo: string;
  ok: boolean;
  changed: boolean | null;
  state: HaltState;
  error: string | null;
}

export interface HaltRequest {
  request_id: string;
  at: string;
  target: "on" | "off";
  scope: "fleet" | "repos";
  repos: string[];
  fleet_at_request: string[];
  reason: string;
  principal: string;
  results: HaltResult[];
}

export interface HaltView {
  fleet: RepoHalt[];
  applying: string | null;
  halted: number;
  unhealthy: string[];
  deviations: string[];
  last_request: HaltRequest | null;
  sources: Record<string, SourceStatus>;
  complete: boolean;
  generated_at: string;
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

  benchmarks(): Promise<BenchmarksStatusResponse> {
    return this.get("/api/benchmarks");
  }

  humanQueue(): Promise<HumanQueueView> {
    return this.get("/api/human-queue");
  }

  factoryFloor(): Promise<FactoryFloorView> {
    return this.get("/api/factory-floor");
  }

  halt(): Promise<HaltView> {
    return this.get("/api/halt");
  }

  /** Halt or lift (`repos` null = the whole fleet). Recorded, then applied
   * in the background — the answer carries no results yet. */
  setHalt(
    state: "on" | "off",
    repos: string[] | null,
    reason: string,
  ): Promise<HaltRequest> {
    return this.postWithToken("/api/halt", { state, repos, reason });
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

  async getOnboarding(name: string): Promise<OnboardingView> {
    return this.get<OnboardingView>(
      `/api/projects/${encodeURIComponent(name)}/onboarding`,
    );
  }

  /** `null` means 404 — «not this kind of project / unknown project» —
   * the caller hides the section (web-panel parity). Any other failure
   * throws: unknown must not look like «no waits». */
  async getProductProposals(
    name: string,
  ): Promise<ProductProposalsReport | null> {
    const path = `/api/projects/${encodeURIComponent(name)}/product-proposals`;
    const resp = await fetch(`${this.baseUrl}${path}`, {
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
    if (resp.status === 404) {
      return null;
    }
    if (!resp.ok) {
      await this.raise(resp, `GET ${path}`);
    }
    return (await resp.json()) as ProductProposalsReport;
  }

  /** Snapshot subset for the Orchestration-runs section (TODO
   * maestro-runs-panel-parity). `null` means 404 — unknown project, the
   * caller omits the section. Any other failure throws: unknown must not
   * look like «no runs». */
  async getProjectRuns(name: string): Promise<RunsSnapshot | null> {
    const path = `/api/projects/${encodeURIComponent(name)}`;
    const resp = await fetch(`${this.baseUrl}${path}`, {
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
    if (resp.status === 404) {
      return null;
    }
    if (!resp.ok) {
      await this.raise(resp, `GET ${path}`);
    }
    return (await resp.json()) as RunsSnapshot;
  }
}
