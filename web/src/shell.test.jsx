import { render, screen, waitFor } from "@testing-library/react";
import { expect, test } from "vitest";
import App from "./App.jsx";
import { mockApi } from "./test/mockApi.js";
import { baseRoutes, EMPTY_TOTALS } from "./test/fixtures.js";

const BANNER = "Synthetic data • simulated filing";
const BARE_ZERO = /(^|[^\d.])0\.00/;

function renderAt(hash, routes = baseRoutes()) {
  window.location.hash = hash;
  const api = mockApi(routes);
  render(<App />);
  return api;
}

test.each(["#/queue", "#/totals", "#/workpaper", "#/item/ready-1", "#/item/held-1"])(
  "the banner renders on %s",
  async (hash) => {
    renderAt(hash);
    expect(await screen.findByText(BANNER)).toBeTruthy();
    expect(screen.getByText("Synthetic demo data")).toBeTruthy();
  },
);

test("the figures render side by side with no combined figure", async () => {
  renderAt("#/totals");
  const liabilities = await screen.findByTestId("liabilities");
  const refunds = screen.getByTestId("refunds");
  const held = screen.getByTestId("held");
  expect(liabilities.parentElement).toBe(refunds.parentElement);
  expect(refunds.parentElement).toBe(held.parentElement);
  expect(liabilities.textContent).toContain("151.92 USD");
  expect(liabilities.textContent).not.toContain("83.00");
  expect(refunds.textContent).toContain("83.00 USD");
  expect(refunds.textContent).toContain("not recovered cash");
  expect(refunds.textContent).not.toContain("151.92");
  expect(held.textContent).toContain("120.00 USD purchase amount, not tax");
  // No figure combines them: no sum or difference of the copied strings.
  expect(document.body.textContent).not.toMatch(/234\.92|68\.92|354\.92|271\.92|combined/i);
});

test("no approved liabilities shows the API message, never a bare zero", async () => {
  renderAt("#/totals", baseRoutes({ "GET /api/v1/totals": EMPTY_TOTALS }));
  const liabilities = await screen.findByTestId("liabilities");
  expect(liabilities.textContent).toContain("No approved liabilities");
  expect(liabilities.textContent).not.toMatch(BARE_ZERO);
  expect(screen.getByTestId("refunds").textContent).not.toMatch(BARE_ZERO);
});

test("a held row shows no tax figure; the held item shows purchase amount, not tax", async () => {
  renderAt("#/queue");
  const row = (await screen.findByText("SYN-6")).closest("tr");
  expect(row.textContent).not.toMatch(/\d+\.\d{2}/);
  expect(row.textContent).toContain("No engine call and no tax amount.");

  window.location.hash = "#/item/held-1";
  window.dispatchEvent(new HashChangeEvent("hashchange"));
  const box = await screen.findByRole("status");
  expect(box.textContent).toContain("Held: no tax figure");
  expect(box.textContent).toMatch(/purchase amount, not tax: 120\.00 USD/i);
  expect(box.textContent).toContain("user_location");
  expect(box.textContent).not.toMatch(BARE_ZERO);
  expect(screen.queryByRole("button", { name: /^Approve/ })).toBeNull();
});

test("replay extraction is labeled replay and never live", async () => {
  renderAt("#/item/ready-1");
  expect((await screen.findAllByText("Recorded extraction (replay)")).length).toBeGreaterThan(0);
  await waitFor(() => expect(document.body.textContent).not.toMatch(/\blive\b/i));
});
