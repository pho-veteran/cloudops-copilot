// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Workspace } from "./FoundationApp";

const mocks = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  csrf: vi.fn(),
}));
vi.mock("@/lib/api/client", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/api/client")>();
  return {
    ...original,
    api: { GET: mocks.get, POST: mocks.post },
    transport: { setCsrfToken: mocks.csrf },
  };
});
vi.mock("@/features/incidents/WorkQueue", () => ({
  WorkQueue: () => <h1>Work queue</h1>,
}));
const session = {
  principal: { id: "user", username: "operator", role: "operator" },
  csrf_token: "token",
  expires_at: "2026-10-22T20:00:00Z",
};
const success = (data: unknown) => ({
  data,
  response: new Response(null, { status: 200 }),
});
function mount() {
  const cache = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={cache}>
      <Workspace />
    </QueryClientProvider>,
  );
  return cache;
}
beforeEach(() => {
  vi.clearAllMocks();
  window.history.replaceState(null, "", "/");
  window.scrollTo = vi.fn();
});
afterEach(cleanup);

describe("application session boundary", () => {
  it("renders the authenticated shell immediately after sign-in", async () => {
    mocks.get.mockResolvedValue({
      error: { error: { code: "session_expired", message: "Sign in" } },
      response: new Response(null, { status: 401 }),
    });
    mocks.post.mockResolvedValue(success(session));
    const cache = mount();
    await screen.findByRole("heading", { name: "Sign in to your workspace" });
    cache.setQueryData(["incidents", "old-user"], { private: true });
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Username"), "operator");
    await user.type(screen.getByLabelText("Password"), "local-test-password");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    await screen.findByRole("heading", { name: "Work queue" });
    expect(cache.getQueryData(["incidents", "old-user"])).toBeUndefined();
    expect(mocks.csrf).toHaveBeenCalledWith("token");
    expect(mocks.post).toHaveBeenCalledTimes(1);
    cache.clear();
  });
  it("removes protected records and requires sign-in after expiry without replay", async () => {
    mocks.get.mockResolvedValue(success(session));
    const cache = mount();
    await screen.findByRole("heading", { name: "Work queue" });
    cache.setQueryData(["incidents", "saved"], { private: true });
    act(() => {
      window.dispatchEvent(new Event("cloudops:session-expired"));
    });
    await screen.findByRole("heading", { name: "Sign in to your workspace" });
    expect(cache.getQueryData(["incidents", "saved"])).toBeUndefined();
    expect(screen.getByRole("status").textContent).toContain("Your session ended.");
    expect(mocks.post).not.toHaveBeenCalled();
    cache.clear();
  });
  it("distinguishes intentional sign-out and allows a fresh sign-in", async () => {
    mocks.get.mockResolvedValue(success(session));
    mocks.post.mockImplementation((path) =>
      Promise.resolve(success(path === "/api/login" ? session : {})),
    );
    const cache = mount();
    await screen.findByRole("heading", { name: "Work queue" });
    cache.setQueryData(["incidents", "saved"], { private: true });
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Sign out" }));
    await screen.findByRole("heading", { name: "Sign in to your workspace" });
    expect(screen.getByRole("status").textContent).toContain("You signed out.");
    expect(screen.queryByText(/Your session ended/)).toBeNull();
    expect(cache.getQueryData(["incidents", "saved"])).toBeUndefined();
    expect(mocks.csrf).toHaveBeenCalledWith(null);
    expect(mocks.post).toHaveBeenCalledTimes(1);
    await user.type(screen.getByLabelText("Username"), "operator");
    await user.type(screen.getByLabelText("Password"), "local-test-password");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    await screen.findByRole("heading", { name: "Work queue" });
    expect(screen.queryByText(/You signed out/)).toBeNull();
    expect(mocks.post.mock.calls.map(([path]) => path)).toEqual([
      "/api/logout",
      "/api/login",
    ]);
    cache.clear();
  });
  it("hides mutation controls for a Viewer", async () => {
    mocks.get.mockResolvedValue(
      success({
        ...session,
        principal: { ...session.principal, role: "viewer" },
      }),
    );
    const cache = mount();
    await screen.findByRole("heading", { name: "Work queue" });
    expect(screen.queryByRole("button", { name: "New incident" })).toBeNull();
    cache.clear();
  });
});
