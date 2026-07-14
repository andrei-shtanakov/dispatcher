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

export interface SyncReportSummary {
  current_host: string;
  top_line: string; // ok | pull-first | no-data | unknown
  top_reason: string | null;
}

export interface SyncStatusResponse {
  report: SyncReportSummary;
  fetch_in_flight: boolean;
  last_fetch_at: string | null;
  last_fetch_error: string | null;
}

const TIMEOUT_MS = 3000;

export class ApiClient {
  constructor(private readonly baseUrl: string) {}

  private async get<T>(path: string): Promise<T> {
    const resp = await fetch(`${this.baseUrl}${path}`, {
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
    if (!resp.ok) {
      throw new Error(`GET ${path}: HTTP ${resp.status}`);
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
}
