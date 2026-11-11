import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import App from "./App.jsx";
import ItemView from "./views/ItemView.jsx";

const META = { data_mode: "synthetic", engine_mode: "mock_reviewed_ruleset", llm_mode: "replay", replay: true };
const ROW = {
  id: "p1", vendor: "Synthetic Cloud Co", invoice_number: "SYN-1001", invoice_date: "2026-11-15",
  queue: "ready", extraction_mode: "replay", stop_reason: "Engine-supported result waiting for a named reviewer.",
  pending_results: [{ determination_id: "d1", jurisdiction: "WA", direction: "LIABILITY", engine_amount: { value: "151.920", currency: "USD" } }],
};
const HELD = { id: "p2", vendor: "Synthetic Team Co", invoice_number: "SYN-1006", invoice_date: "2026-11-20", queue: "held", pending_results: [] };
const TOTALS = {
  ...META,
  approved_liabilities: { by_jurisdiction: [], message: "No approved liabilities" },
  refund_candidates: { label: "not recovered cash", count: 0, total: null, rows: [] },
  held: { count: 1, label: "purchase amount, not tax" },
  open_needs_human_review: { count: 0 }, determination_errors: { count: 0 }, unreviewed: { by_direction: [] },
  hold_rate: { held_count: 1, item_count: 2, percent: "50" },
};

function mockApi(calls) {
  return vi.fn(async (url, init = {}) => {
    calls.push({ url, init });
    const path = String(url).replace("/api/v1", "").split("?")[0];
    const body =
      path === "/demo/login" ? { ...META, person_name: "Avery Chen", csrf_token: "tok" }
      : path === "/demo/scenarios" ? { ...META, scenarios: ["case1", "case6"] }
      : path === "/purchases" ? { ...META, purchases: [ROW, HELD], next_cursor: null }
      : path === "/totals" ? TOTALS
      : {};
    return { ok: true, status: 200, text: async () => JSON.stringify(body) };
  });
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  window.location.hash = "";
});

describe("app shell and queue", () => {
  it("logs in with JSON and shows the banner, exact amounts and hold rate", async () => {
    const calls = [];
    vi.stubGlobal("fetch", mockApi(calls));
    render(<App />);
    expect(await screen.findByText("Synthetic demo data")).toBeTruthy();
    const loginCall = calls.find((c) => String(c.url).endsWith("/demo/login"));
    expect(loginCall.init.headers["Content-Type"]).toBe("application/json");
    expect(loginCall.init.headers["Idempotency-Key"]).toBeTruthy();
    // Exact string from the API, trailing zero kept, never reformatted.
    expect(await screen.findByText(/151\.920 USD/)).toBeTruthy();
    expect(screen.getAllByText("unreviewed; not in any total").length).toBe(1);
    expect(screen.getByTestId("hold-rate").textContent).toContain("1 of 2 items held");
    // The held row shows no amount.
    const heldRow = screen.getByText("Synthetic Team Co").closest("tr");
    expect(heldRow.textContent).not.toMatch(/\d+\.\d{2} USD/);
  });
});

describe("item view", () => {
  it("labels replay extraction even when the server mode is live", async () => {
    const item = {
      ...META, llm_mode: "live", replay: false, id: "p1", vendor: "Synthetic Cloud Co", invoice_number: "SYN-1001",
      queue: "held", extraction_mode: "replay", facts_version: 1, lines: [], documents: [], facts: [],
      holds: [{ id: "h1", reason: "MISSING_REQUIRED_FACT", named_fact: "user_location", active: true }],
      questions: [], determinations: [], past_determinations: [], actions: [],
    };
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, status: 200, text: async () => JSON.stringify(item) })));
    render(<ItemView id="p1" tick={0} />);
    await waitFor(() => expect(screen.getAllByText("Recorded extraction (replay)").length).toBeGreaterThan(0));
    expect(screen.getByText("Held: no tax figure")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
  });
});

describe("UI copy and money rules (release gates scan these too)", () => {
  const dir = join(__dirname);
  const files = [];
  (function walk(d) {
    for (const name of readdirSync(d, { withFileTypes: true })) {
      const p = join(d, name.name);
      if (name.isDirectory()) walk(p);
      else if (/\.(jsx?|css)$/.test(name.name) && !name.name.includes(".test.")) files.push(p);
    }
  })(dir);
  const source = files.map((f) => readFileSync(f, "utf8")).join("\n");

  it("never parses or reformats money in the browser", () => {
    expect(source).not.toMatch(/parseFloat|parseInt|Number\(|toFixed|Intl\.NumberFormat/);
  });

  it("has no net, savings or recovered copy except the not-recovered-cash note", () => {
    const stripped = source.replace(/not recovered cash/gi, "");
    expect(stripped).not.toMatch(/\b(net|savings|recovered)\b/i);
  });

  it("never says filed or paid without simulated on the same line", () => {
    for (const line of source.split("\n")) {
      if (/\b(filed|paid)\b/i.test(line)) expect(line).toMatch(/simulated/i);
    }
  });
});
