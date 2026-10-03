import { describe, expect, it, vi } from "vitest";
import { checkReadiness } from "./health";

describe("readiness contract", () => {
  it.each([
    [200, "ready", "ready"],
    [503, "not_ready", "not_ready"],
    [500, "ready", "unreachable"],
    [200, "not_ready", "unreachable"],
  ])("maps HTTP %s and %s honestly", async (code, status, expected) => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({ status, scope: "database_schema" }), { status: code }),
    );
    expect(await checkReadiness(fetcher)).toBe(expected);
  });

  it("rejects non-API HTML and network failure", async () => {
    const html = vi.fn<typeof fetch>().mockResolvedValue(new Response("<!doctype html>"));
    expect(await checkReadiness(html)).toBe("unreachable");
    const offline = vi.fn<typeof fetch>().mockRejectedValue(new TypeError("Failed to fetch"));
    expect(await checkReadiness(offline)).toBe("unreachable");
  });
});
