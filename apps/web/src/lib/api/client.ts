import createClient from "openapi-fetch";
import type { paths, components } from "./generated/schema";

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
  const client = createClient<paths>({
    baseUrl:
      options.baseUrl ?? globalThis.location?.origin ?? "http://localhost",
    credentials: "same-origin",
    ...(options.fetch ? { fetch: options.fetch } : {}),
  });
  client.use({
    onRequest({ request }) {
      if (!["GET", "HEAD"].includes(request.method) && csrfToken) {
        request.headers.set("X-CSRF-Token", csrfToken);
      }
      return request;
    },
    onResponse({ request, response }) {
      if (
        response.status === 401 &&
        new URL(request.url).pathname !== "/api/login"
      ) {
        const hadSession = csrfToken !== null;
        csrfToken = null;
        if (hadSession) options.onExpired?.();
      }
      return response;
    },
  });
  return {
    client,
    setCsrfToken(value: string | null) {
      csrfToken = value;
    },
  };
}

export function unwrap<T>(result: {
  data?: T;
  error?: unknown;
  response: Response;
}): T {
  if (!result.response.ok) {
    const error = result.error as
      | components["schemas"]["ErrorEnvelope"]
      | undefined;
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
