import { readFileSync } from "node:fs";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiClient, ApiError } from "../src/api";

function fixture(name: string): unknown {
  return JSON.parse(
    readFileSync(new URL(`./fixtures/${name}`, import.meta.url), "utf-8"),
  );
}

function okResponse(payload: unknown): Response {
  return new Response(JSON.stringify(payload), { status: 200 });
}

function jsonResponse(payload: unknown, status: number): Response {
  return new Response(JSON.stringify(payload), { status });
}

afterEach(() => vi.unstubAllGlobals());

describe("ApiClient", () => {
  it("fetches and parses the overview", async () => {
    const fetchMock = vi.fn().mockResolvedValue(okResponse(fixture("overview.json")));
    vi.stubGlobal("fetch", fetchMock);
    const overview = await new ApiClient("http://127.0.0.1:8787").overview();
    expect(fetchMock.mock.calls[0][0]).toBe("http://127.0.0.1:8787/api/overview");
    expect(overview.projects[0].name).toBe("arbiter");
    expect(overview.projects[1].detected).toBe(false);
  });

  it("requests errors with the web-parity query", async () => {
    const fetchMock = vi.fn().mockResolvedValue(okResponse(fixture("errors.json")));
    vi.stubGlobal("fetch", fetchMock);
    const events = await new ApiClient("http://x").errors();
    expect(fetchMock.mock.calls[0][0]).toBe("http://x/api/errors?days=14&limit=50");
    expect(events).toHaveLength(2);
  });

  it("fetches and parses the roadmap", async () => {
    const fetchMock = vi.fn().mockResolvedValue(okResponse(fixture("roadmap.json")));
    vi.stubGlobal("fetch", fetchMock);
    const roadmap = await new ApiClient("http://x").roadmap();
    expect(fetchMock.mock.calls[0][0]).toBe("http://x/api/roadmap");
    expect(roadmap.roadmaps).toEqual(["ecosystem-2026"]);
    expect(roadmap.items).toHaveLength(5);
    expect(roadmap.items[0].evidence[0].passed).toBe(true);
  });

  it("URL-encodes project names", async () => {
    const fetchMock = vi.fn().mockResolvedValue(okResponse(fixture("project.json")));
    vi.stubGlobal("fetch", fetchMock);
    await new ApiClient("http://x").project("a b");
    expect(fetchMock.mock.calls[0][0]).toBe("http://x/api/projects/a%20b");
  });

  it("throws on non-200", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("nope", { status: 500 })));
    await expect(new ApiClient("http://x").overview()).rejects.toThrow("HTTP 500");
  });

  it("passes an abort signal (timeout)", async () => {
    const fetchMock = vi.fn().mockResolvedValue(okResponse(fixture("overview.json")));
    vi.stubGlobal("fetch", fetchMock);
    await new ApiClient("http://x").overview();
    expect(fetchMock.mock.calls[0][1]?.signal).toBeInstanceOf(AbortSignal);
  });
});

describe("ApiClient sync shape", () => {
  it("parses hosts, verdicts and proposals", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(okResponse(fixture("sync_full.json"))),
    );
    const sync = await new ApiClient("http://x").sync();
    expect(sync.report.hosts).toHaveLength(2);
    expect(sync.report.hosts[0].verdicts[0].ahead).toBe(2);
    expect(sync.report.proposals).toEqual(["newrepo"]);
  });
});

describe("ApiError", () => {
  it("carries the body detail on non-ok responses", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(
          jsonResponse({ detail: "alpha: update already in flight" }, 409),
        ),
    );
    const err = await new ApiClient("http://x")
      .sync()
      .then(() => null)
      .catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(409);
    expect((err as ApiError).detail).toBe("alpha: update already in flight");
  });

  it("falls back to HTTP status text when the body is not JSON", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response("nope", { status: 500 })),
    );
    const err = await new ApiClient("http://x")
      .sync()
      .then(() => null)
      .catch((e: unknown) => e);
    expect((err as ApiError).detail).toContain("HTTP 500");
  });
});

describe("action POSTs and the token cache", () => {
  it("fetches the token once and reuses it", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(okResponse({ token: "tok1" }))
      .mockResolvedValueOnce(okResponse(fixture("action_outcome_ok.json")))
      .mockResolvedValueOnce(okResponse(fixture("action_outcome_ok.json")));
    vi.stubGlobal("fetch", fetchMock);
    const api = new ApiClient("http://x");
    await api.pull("alpha");
    await api.createPr("alpha");
    const calls = fetchMock.mock.calls;
    expect(calls[0][0]).toBe("http://x/api/actions/session");
    expect(calls[1][0]).toBe("http://x/api/actions/pull");
    expect((calls[1][1] as RequestInit).headers).toMatchObject({
      "X-Action-Token": "tok1",
    });
    expect(calls[2][0]).toBe("http://x/api/actions/create-pr");
    expect(calls).toHaveLength(3); // token fetched once, reused
  });

  it("on 403 refetches the token exactly once and retries", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(okResponse({ token: "stale" }))
      .mockResolvedValueOnce(jsonResponse({ detail: "bad token" }, 403))
      .mockResolvedValueOnce(okResponse({ token: "fresh" }))
      .mockResolvedValueOnce(okResponse(fixture("action_outcome_ok.json")));
    vi.stubGlobal("fetch", fetchMock);
    const outcome = await new ApiClient("http://x").pull("alpha");
    expect(outcome.ok).toBe(true);
    expect(fetchMock.mock.calls).toHaveLength(4);
  });

  it("fails after the second 403", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(okResponse({ token: "stale" }))
      .mockResolvedValueOnce(jsonResponse({ detail: "bad token" }, 403))
      .mockResolvedValueOnce(okResponse({ token: "still-stale" }))
      .mockResolvedValueOnce(jsonResponse({ detail: "bad token" }, 403));
    vi.stubGlobal("fetch", fetchMock);
    const err = await new ApiClient("http://x")
      .pull("alpha")
      .then(() => null)
      .catch((e: unknown) => e);
    expect((err as ApiError).status).toBe(403);
    expect(fetchMock.mock.calls).toHaveLength(4); // no third refetch
  });

  it("track posts without a token", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(okResponse({ tracked: ["newrepo"], ignored: [] }));
    vi.stubGlobal("fetch", fetchMock);
    await new ApiClient("http://x").track("newrepo", "track");
    expect(fetchMock.mock.calls[0][0]).toBe("http://x/api/sync/track");
    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(
      (init.headers as Record<string, string>)["X-Action-Token"],
    ).toBeUndefined();
  });
});

describe("specRunnerConfigs degradation", () => {
  it("surfaces 404 as ApiError(status=404) for the old-server branch", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse({ detail: "Not Found" }, 404)),
    );
    const err = await new ApiClient("http://x")
      .specRunnerConfigs()
      .then(() => null)
      .catch((e: unknown) => e);
    expect((err as ApiError).status).toBe(404);
  });
});
