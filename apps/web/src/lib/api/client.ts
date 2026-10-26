import createClient from "openapi-fetch";
import type { paths, components } from "./generated/schema";

const responseGuards = new WeakMap<Response, () => void>();

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
  ) {
    super(message);
  }
}

export function createTransport(
  options: {
    baseUrl?: string;
    fetch?: typeof fetch;
    onExpired?: () => void;
  } = {},
) {
  let csrfToken: string | null = null;
  let generation = 0;
  const requests = new Map<
    string,
    { generation: number; controller: AbortController }
  >();
  function advance() {
    generation++;
    for (const pending of requests.values()) pending.controller.abort();
  }
  function assertCurrent(value: number) {
    if (value !== generation)
      throw new ApiError(
        409,
        "session_changed",
        "Your session changed. Read the current workspace before trying again.",
      );
  }
  const client = createClient<paths>({
    baseUrl:
      options.baseUrl ?? globalThis.location?.origin ?? "http://localhost",
    credentials: "same-origin",
    ...(options.fetch ? { fetch: options.fetch } : {}),
  });
  client.use({
    onRequest({ request, id }) {
      const controller = new AbortController();
      requests.set(id, { generation, controller });
      request = new Request(request, {
        signal: AbortSignal.any([request.signal, controller.signal]),
      });
      if (!["GET", "HEAD"].includes(request.method) && csrfToken) {
        request.headers.set("X-CSRF-Token", csrfToken);
      }
      return request;
    },
    onError({ id, error }) {
      const started = requests.get(id)!.generation;
      requests.delete(id);
      assertCurrent(started);
      return error instanceof Error ? error : new Error("The request failed.");
    },
    onResponse({ request, response, id }) {
      const started = requests.get(id)!.generation;
      requests.delete(id);
      assertCurrent(started);
      if (
        response.status === 401 &&
        new URL(request.url).pathname !== "/api/login"
      ) {
        const hadSession = csrfToken !== null;
        if (hadSession) advance();
        csrfToken = null;
        if (hadSession) options.onExpired?.();
      }
      const received = generation;
      // Parsing a response body is asynchronous too. Check again at consumption.
      responseGuards.set(response, () => assertCurrent(received));
      return response;
    },
  });
  return {
    client,
    setCsrfToken(value: string | null) {
      if (value !== csrfToken) advance();
      csrfToken = value;
    },
    clearSession() {
      advance();
      csrfToken = null;
    },
  };
}

export function unwrap<T>(result: {
  data?: T;
  error?: unknown;
  response: Response;
}): T {
  responseGuards.get(result.response)?.();
  if (!result.response.ok) {
    const error = result.error as
      components["schemas"]["ErrorEnvelope"] | undefined;
    throw new ApiError(
      result.response.status,
      error?.error?.code ?? "request_failed",
      error?.error?.message ??
        "The request could not be completed. Try again explicitly.",
    );
  }
  return result.data as T;
}

export const transport = createTransport({
  onExpired: () => {
    if (typeof window !== "undefined")
      window.dispatchEvent(new Event("cloudops:session-expired"));
  },
});
export const api = transport.client;
export function message(error: unknown): string {
  if (error instanceof TypeError)
    return "Cannot reach the local API. Check the API and database services, then retry.";
  return error instanceof Error
    ? error.message
    : "The service is unavailable. Check local services and retry.";
}
