// Thin client for the Tax Leak API. No tax logic lives here: amounts are the
// exact strings the API returns, and the browser never sends an amount except
// a sourced fact edit typed by a person.
const BASE = "/api/v1";
const REQUESTED_WITH = { "X-Requested-With": "taxleak" };
let csrfToken = null;

export class ApiError extends Error {
  constructor(status, body) {
    super(body?.error?.message || `Request failed with ${status}`);
    this.status = status;
    this.code = body?.error?.code || null;
  }
}

function newKey() {
  return globalThis.crypto?.randomUUID
    ? globalThis.crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

async function parse(res) {
  const text = await res.text();
  const body = text ? JSON.parse(text) : {};
  if (!res.ok) throw new ApiError(res.status, body);
  return body;
}

export async function get(path, params) {
  const query = params ? `?${new URLSearchParams(params)}` : "";
  const res = await fetch(`${BASE}${path}${query}`, {
    headers: REQUESTED_WITH,
    credentials: "same-origin",
  });
  return parse(res);
}

export async function post(path, body = {}) {
  const headers = {
    ...REQUESTED_WITH,
    "Content-Type": "application/json",
    "Idempotency-Key": newKey(),
  };
  if (csrfToken) headers["X-CSRF-Token"] = csrfToken;
  const res = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers,
    credentials: "same-origin",
    body: JSON.stringify(body),
  });
  return parse(res);
}

export async function login() {
  const body = await post("/demo/login", {});
  csrfToken = body.csrf_token || null;
  return body;
}

// Every purchase across pages (the list endpoint returns at most 100).
export async function allPurchases(params = {}) {
  const rows = [];
  let cursor = null;
  do {
    const page = await get("/purchases", cursor ? { ...params, cursor } : params);
    rows.push(...(page.purchases || []));
    cursor = page.next_cursor || null;
  } while (cursor);
  return rows;
}

export function money(amount) {
  // Exact API string, never parsed or reformatted.
  if (!amount || typeof amount.value !== "string") return null;
  return `${amount.value} ${amount.currency || "USD"}`;
}
