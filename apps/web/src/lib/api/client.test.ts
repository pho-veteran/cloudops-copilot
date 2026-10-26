import { describe, expect, it, vi } from "vitest";
import { ApiError, createTransport, unwrap } from "./client";
import { workloadFixture } from "@/features/workloads/fixtures";
import { QueryClient } from "@tanstack/react-query";

describe("session transport", () => {
  it("keeps an authenticated permission denial separate from expiry without retrying", async () => {
    const expired = vi.fn();
    const fetch = vi.fn<typeof globalThis.fetch>(async () =>
      Response.json(
        {
          error: {
            code: "permission_denied",
            message: "An Operator account is required.",
          },
        },
        { status: 403 },
      ),
    );
    const transport = createTransport({ fetch, onExpired: expired });
    transport.setCsrfToken("viewer-csrf");
    const result = await transport.client.POST("/api/logout");
    expect(() => unwrap(result)).toThrow("An Operator account is required.");
    expect(expired).not.toHaveBeenCalled();
    expect(fetch).toHaveBeenCalledOnce();
  });
  it.each([200, 401, 403])(
    "discards a delayed account A response (%s) after account B signs in",
    async (status) => {
      let release!: (value: Response) => void;
      const expired = vi.fn();
      const sent: Request[] = [];
      const transport = createTransport({
        onExpired: expired,
        fetch: async (request) => {
          sent.push(request as Request);
          if (sent.length === 1)
            return new Promise<Response>((resolve) => {
              release = resolve;
            });
          return Response.json({});
        },
      });
      const cache = new QueryClient({
        defaultOptions: { queries: { retry: false } },
      });
      transport.setCsrfToken("account-a");
      const network = transport.client.GET("/api/session");
      const networkRejected = expect(network).rejects.toMatchObject({
        code: "session_changed",
      });
      const pending = cache.fetchQuery({
        queryKey: ["private-a"],
        queryFn: async () => unwrap(await network),
      });
      // Install rejection handling before releasing the delayed network response.
      const rejected = expect(pending).rejects.toBeDefined();
      await vi.waitFor(() => expect(sent).toHaveLength(1));
      transport.clearSession();
      expect(sent[0].signal.aborted).toBe(true);
      cache.clear();
      transport.setCsrfToken("account-b");
      cache.setQueryData(["session"], { csrf_token: "account-b" });
      release(
        Response.json(
          { csrf_token: "account-a", private: "account-a-record" },
          { status },
        ),
      );
      await rejected;
      await networkRejected;
      expect(cache.getQueryData(["private-a"])).toBeUndefined();
      expect(cache.getQueryData(["session"])).toEqual({
        csrf_token: "account-b",
      });
      expect(expired).not.toHaveBeenCalled();
      await transport.client.POST("/api/logout");
      expect(sent[1].headers.get("X-CSRF-Token")).toBe("account-b");
      cache.clear();
    },
  );
  it("rejects an old response whose body finishes parsing after the session changes", async () => {
    let finish!: () => void;
    let started!: () => void;
    const parsing = new Promise<void>((resolve) => {
      started = resolve;
    });
    const response = Response.json({ csrf_token: "old" });
    response.json = async () => {
      started();
      await new Promise<void>((resolve) => {
        finish = resolve;
      });
      return { csrf_token: "old" };
    };
    const transport = createTransport({ fetch: async () => response });
    transport.setCsrfToken("old");
    const request = transport.client.GET("/api/session");
    await parsing;
    transport.clearSession();
    transport.setCsrfToken("new");
    finish();
    const result = await request;
    expect(() => unwrap(result)).toThrowError(
      expect.objectContaining({ code: "session_changed" }),
    );
  });
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
