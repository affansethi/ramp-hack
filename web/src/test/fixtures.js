// Fixture payloads shaped like the API. Amount strings are copied from
// tests/api and are only compared, never computed.
export const META = {
  data_mode: "synthetic",
  engine_mode: "mock_reviewed_ruleset",
  llm_mode: "replay",
  replay: true,
};

export const PURCHASES = {
  ...META,
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
      pending_results: [],
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
      pending_results: [],
    },
    {
      id: "held-1",
      vendor: "Synthetic Seats",
      invoice_number: "SYN-6",
      invoice_date: "2026-11-15",
      item_status: "HELD",
      queue: "held",
      scenario: "case6",
      extraction_mode: "replay",
      facts_version: 1,
      stop_reason: "Held for user_location. No engine call and no tax amount.",
      pending_results: [],
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
      pending_results: [],
    },
  ],
  next_cursor: null,
};

export const TOTALS = {
  ...META,
  approved_liabilities: {
    by_jurisdiction: [
      {
        jurisdiction: "WA",
        period: "2026Q4",
        total: { value: "151.92", currency: "USD" },
        line_count: 1,
      },
    ],
    message: null,
  },
  refund_candidates: {
    label: "not recovered cash",
    count: 1,
    total: { value: "83.00", currency: "USD" },
    rows: [
      {
        determination_id: "det-3",
        purchase_id: "p-3",
        vendor: "Synthetic Overcharge",
        amount: { value: "83.00", currency: "USD" },
        refund_letter_id: "letter-3",
      },
    ],
  },
  held: {
    count: 1,
    label: "purchase amount, not tax",
    purchase_amount: { value: "120.00", currency: "USD" },
  },
  open_needs_human_review: { count: 1 },
  determination_errors: { count: 1 },
  unreviewed: { by_direction: [] },
  hold_rate: {
    held_count: 1,
    item_count: 4,
    percent: "25.0",
    label: "share of current items on hold",
    message: null,
  },
  unreviewed_by_direction: { LIABILITY: 0, REFUND_CANDIDATE: 0, CHARGED_CORRECTLY: 0 },
  pre_close: {
    jurisdictions: [],
    item_level: { needs_human_review_count: 0, held_count: 1 },
  },
};

export const EMPTY_TOTALS = {
  ...TOTALS,
  approved_liabilities: { by_jurisdiction: [], message: "No approved liabilities" },
  refund_candidates: { label: "not recovered cash", count: 0, total: null, rows: [] },
};

export const HELD_DETAIL = {
  ...META,
  ...PURCHASES.purchases[2],
  lines: [],
  documents: [],
  facts: [],
  purchase_amount: { value: "120.00", currency: "USD" },
  purchase_amount_note: "purchase amount, not tax",
  holds: [{ id: "hold-1", reason: "MISSING_REQUIRED_FACT", named_fact: "user_location", active: true }],
  questions: [
    {
      id: "q-1",
      fact_key: "user_location",
      prompt: "Send a seat export or a named answer for seat location.",
      recipient: "spend owner",
      answer: null,
      state: "OPEN",
      asked_at: "2026-11-10T00:00:00Z",
    },
  ],
  determinations: [],
  past_determinations: [],
  fact_history: [],
  decisions: [],
  approver_name: null,
  approved_at: null,
  actions: [],
  possible_duplicate_ids: [],
};

export function baseRoutes(overrides = {}) {
  return {
    "GET /api/v1/purchases": PURCHASES,
    "GET /api/v1/totals": TOTALS,
    "GET /api/v1/purchases/held-1": HELD_DETAIL,
    ...overrides,
  };
}
