export type Readiness = "ready" | "not_ready" | "unreachable";

export async function checkReadiness(fetcher: typeof fetch = fetch): Promise<Readiness> {
  try {
    const response = await fetcher("/health/ready", {
      cache: "no-store",
      signal: AbortSignal.timeout(5000),
    });
    const body: unknown = await response.json();
    if (!body || typeof body !== "object" || !("status" in body) || !("scope" in body)) {
      return "unreachable";
    }
    if (body.scope !== "database_connectivity") return "unreachable";
    if (response.status === 200 && body.status === "ready") return "ready";
    if (response.status === 503 && body.status === "not_ready") return "not_ready";
    return "unreachable";
  } catch {
    return "unreachable";
  }
}
