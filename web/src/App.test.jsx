import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import App from "./App.jsx";

const PURCHASES = {
  llm_mode: "replay",
  replay: true,
  purchases: [
    {
      id: "ready-1",
      vendor: "Synthetic SaaS",
      invoice_number: "SYN-1",
      invoice_date: "2026-11-15",
      item_status: "DETERMINED",
      queue: "ready",
      scenario: "case1",
      extraction_mode: "replay",
      facts_version: 1,
      stop_reason: "Engine-supported result waiting for a named reviewer.",
    },
    {
      id: "nhr-1",
      vendor: "Synthetic Hardware",
      invoice_number: "SYN-4",
      invoice_date: "2026-12-15",
      item_status: "DETERMINED",
      queue: "needs_human_review",
      scenario: "case4",
      extraction_mode: "replay",
      facts_version: 1,
      stop_reason: "Needs human review (RULE_TRANSITION_UNRESOLVED). No tax amount.",
    },
    {
      id: "held-1",
      vendor: "Synthetic Seats",
      invoice_number: "SYN-6",
      invoice_date: "2026-11-15",
      item_status: "DETERMINED",
      queue: "held",
      scenario: "case6",
      extraction_mode: "replay",
      facts_version: 1,
      stop_reason: "Held for user_location. No engine call and no tax amount.",
    },
    {
      id: "error-1",
      vendor: "Synthetic Broken",
      invoice_number: "SYN-9",
      invoice_date: "2026-11-15",
      item_status: "DETERMINED",
      queue: "error",
      scenario: "case9",
      extraction_mode: "replay",
      facts_version: 1,
      stop_reason: "Determination error (MALFORMED). No tax amount.",
    },
  ],
  next_cursor: null,
};

const TOTALS = {
  llm_mode: "replay",
  replay: true,
  approved_liabilities: {
    by_jurisdiction: [],
    message: "No approved liabilities",
  },
  refund_candidates: {
    label: "not recovered cash",
    count: 0,
    total: null,
    rows: [],
  },
  held: {
    count: 1,
    label: "purchase amount, not tax",
    purchase_amount: { value: "120.00", currency: "USD" },
  },
  open_needs_human_review: { count: 1 },
  determination_errors: { count: 1 },
  unreviewed: { by_direction: [] },
};

const HELD_DETAIL = {
  llm_mode: "replay",
  replay: true,
  id: "held-1",
  vendor: "Synthetic Seats",
  queue: "held",
  purchase_amount: { value: "120.00", currency: "USD" },
  purchase_amount_note: "purchase amount, not tax",
  holds: [{ id: "hold-1", reason: "MISSING", named_fact: "user_location", active: true }],
  questions: [
    {
      id: "q-1",
      fact_key: "user_location",
      recipient: "spend owner",
      state: "OPEN",
      asked_at: "2026-11-10T00:00:00Z",
    },
  ],
  determinations: [],
};

function mockFetch() {
  return vi.fn(async (url, options = {}) => {
    const ok = (body) => ({ ok: true, status: 200, json: async () => body });
    if (url === "/api/v1/demo/login" && options.method === "POST") {
      return ok({ csrf_token: "token" });
    }
    if (url === "/api/v1/purchases") return ok(PURCHASES);
    if (url === "/api/v1/totals") return ok(TOTALS);
    if (url === "/api/v1/purchases/held-1") return ok(HELD_DETAIL);
    throw new Error(`unexpected fetch: ${url}`);
  });
}

beforeEach(() => {
  vi.stubGlobal("fetch", mockFetch());
  vi.useFakeTimers({ shouldAdvanceTime: true });
});

test("banner, three queue headings, and held row", async () => {
  render(<App />);

  expect(await screen.findByText("Synthetic data • simulated filing")).toBeTruthy();
  expect(await screen.findByText("Synthetic demo data")).toBeTruthy();
  expect(await screen.findByText("Recorded extraction: replay")).toBeTruthy();

  expect(await screen.findByText("Approved liabilities")).toBeTruthy();
  expect(await screen.findByText("No approved liabilities")).toBeTruthy();
  expect(await screen.findByText("not recovered cash")).toBeTruthy();

  expect(screen.getByRole("heading", { name: "Ready for review" })).toBeTruthy();
  expect(screen.getByRole("heading", { name: "Needs human review" })).toBeTruthy();
  expect(
    screen.getByRole("heading", { name: "Held", level: 2 }),
  ).toBeTruthy();
  expect(screen.getByRole("heading", { name: "Errors" })).toBeTruthy();

  expect(await screen.findByText(/Asked: spend owner/)).toBeTruthy();
  expect(await screen.findByText(/2026-11-10T00:00:00Z/)).toBeTruthy();
  expect(await screen.findByText(/Reply status: OPEN/)).toBeTruthy();
  expect((await screen.findAllByText(/120\.00 USD/)).length).toBeGreaterThan(0);
  expect(
    (await screen.findAllByText(/purchase amount, not tax/)).length,
  ).toBeGreaterThan(0);
  expect(
    await screen.findByText("Missing fact: user_location"),
  ).toBeTruthy();

  await waitFor(() => {
    expect(screen.queryByText(/live/i)).toBeNull();
  });
});
