// Mocked API layer for component tests. Each route is "METHOD /path" mapped
// to a response body, a function of the request, or withStatus(status, body).
import { vi } from "vitest";

export function withStatus(status, body) {
  return { __status: status, body };
}

function respond(result) {
  const status = result && result.__status ? result.__status : 200;
  const body = result && result.__status ? result.body : result;
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    text: async () => (typeof body === "string" ? body : JSON.stringify(body)),
  };
}

export function mockApi(routes) {
  const calls = [];
  const table = {
    "POST /api/v1/demo/login": { csrf_token: "csrf-test", person_name: "Demo Reviewer" },
    ...routes,
  };
  const fetchMock = vi.fn(async (url, options = {}) => {
    const method = (options.method || "GET").toUpperCase();
    const call = {
      url,
      method,
      headers: options.headers || {},
      body: options.body ? JSON.parse(options.body) : undefined,
    };
    calls.push(call);
    const handler = table[`${method} ${url}`];
    if (handler === undefined) throw new Error(`unexpected fetch: ${method} ${url}`);
    return respond(typeof handler === "function" ? handler(call) : handler);
  });
  vi.stubGlobal("fetch", fetchMock);
  return { fetchMock, calls };
}

export function mutations(calls) {
  return calls.filter((call) => call.method !== "GET" && call.url !== "/api/v1/demo/login");
}
