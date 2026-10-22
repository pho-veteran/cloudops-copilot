import { describe, expect, it, vi } from "vitest";
import { ApiError, createTransport, unwrap } from "./client";
import { workloadFixture } from "@/features/workloads/fixtures";

describe("session transport", () => {
  it("keeps the CSRF token in memory and sends it only on mutations", async () => {
    const requests: Request[] = [];
    const transport = createTransport({
      fetch: async (request) => {
        requests.push(request as Request);
        return Response.json(workloadFixture);
      },
    });
    transport.setCsrfToken("session-bound-token");
    await transport.client.GET("/api/workloads");
    await transport.client.POST("/api/logout");
    expect(requests[0].headers.has("X-CSRF-Token")).toBe(false);
    expect(requests[1].headers.get("X-CSRF-Token")).toBe("session-bound-token");
    expect(requests[1].credentials).toBe("same-origin");
  });
  it("clears expired authority without replaying the request", async () => {
    const expired = vi.fn();
    const fetch = vi.fn<typeof globalThis.fetch>(async () =>
      Response.json(
        { error: { code: "session_expired", message: "Sign in again." } },
        { status: 401 },
      ),
    );
    const transport = createTransport({ fetch, onExpired: expired });
    transport.setCsrfToken("old-token");
    const response = await transport.client.POST("/api/logout");
    expect(() => unwrap(response)).toThrow(ApiError);
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(expired).toHaveBeenCalledOnce();
    fetch.mockImplementationOnce(async (request) => {
      expect((request as Request).headers.has("X-CSRF-Token")).toBe(false);
      return Response.json({});
    });
    await transport.client.POST("/api/logout");
  });
  it("keeps invalid login separate from session expiry", async () => {
    const expired = vi.fn();
    const transport = createTransport({
      onExpired: expired,
      fetch: async () =>
        Response.json(
          {
            error: {
              code: "invalid_credentials",
              message: "Incorrect credentials.",
            },
          },
          { status: 401 },
        ),
    });
    const response = await transport.client.POST("/api/login", {
      body: { username: "operator", password: "wrong" },
    });
    expect(() => unwrap(response)).toThrow("Incorrect credentials.");
    expect(expired).not.toHaveBeenCalled();
  });
});
