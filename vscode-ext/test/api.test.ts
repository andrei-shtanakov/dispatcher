import { readFileSync } from "node:fs";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiClient } from "../src/api";

function fixture(name: string): unknown {
  return JSON.parse(
    readFileSync(new URL(`./fixtures/${name}`, import.meta.url), "utf-8"),
  );
}

function okResponse(payload: unknown): Response {
  return new Response(JSON.stringify(payload), { status: 200 });
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
