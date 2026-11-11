import { render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import App from "./App.jsx";
import { mockApi } from "./test/mockApi.js";
import { baseRoutes, EMPTY_TOTALS, META, PURCHASES, TOTALS } from "./test/fixtures.js";

const BANNER = "Synthetic data • simulated filing";

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
});

function section(name) {
  return screen.getByRole("heading", { name, level: 2 }).closest("section");
}

test("the banner renders with the synthetic and replay labels", async () => {
  mockApi(baseRoutes());
  render(<App />);
  expect(await screen.findByText(BANNER)).toBeTruthy();
  expect(screen.getByText("Synthetic demo data")).toBeTruthy();
  expect(await screen.findByText("Recorded extraction: replay")).toBeTruthy();
});

test("the banner stays up when the API fails", async () => {
  mockApi({
    "GET /api/v1/purchases": () => {
      throw new Error("offline");
    },
    "GET /api/v1/totals": TOTALS,
  });
  render(<App />);
  expect(await screen.findByRole("alert")).toBeTruthy();
  expect(screen.getByText(BANNER)).toBeTruthy();
});

test("the three queues render as separate lists", async () => {
  mockApi(baseRoutes());
  render(<App />);
  await screen.findByText("SYN-1", { exact: false });
  expect(within(section("Ready for review")).getByText(/SYN-1\b/)).toBeTruthy();
  expect(within(section("Ready for review")).queryByText(/SYN-4|SYN-6/)).toBeNull();
  expect(within(section("Needs human review")).getByText(/SYN-4/)).toBeTruthy();
  expect(within(section("Needs human review")).queryByText(/SYN-1\b|SYN-6/)).toBeNull();
  expect(within(section("Held")).getByText(/SYN-6/)).toBeTruthy();
  expect(within(section("Held")).queryByText(/SYN-1\b|SYN-4/)).toBeNull();
});

test("the figures render side by side with no combined figure", async () => {
  mockApi(baseRoutes());
  render(<App />);
  const figures = await screen.findByRole("region", { name: "Figures" });
  await within(figures).findByText(/151\.92 USD/);
  const headings = within(figures)
    .getAllByRole("heading", { level: 3 })
    .map((node) => node.textContent);
  expect(headings).toEqual(["Approved liabilities", "not recovered cash", "Held"]);
  expect(within(figures).getByText(/151\.92 USD/)).toBeTruthy();
  expect(within(figures).getAllByText(/83\.00 USD/).length).toBeGreaterThan(0);
  expect(within(figures).getByText(/120\.00 USD/)).toBeTruthy();
  // No figure combines the three: 151.92, 83.00 and 120.00 appear only as copied.
  expect(figures.textContent).not.toMatch(/combined|234\.92|68\.92|354\.92/i);
});

test("no approved liabilities shows the API message, never a bare zero", async () => {
  mockApi(baseRoutes({ "GET /api/v1/totals": EMPTY_TOTALS }));
  render(<App />);
  const figures = await screen.findByRole("region", { name: "Figures" });
  expect(await within(figures).findByText("No approved liabilities")).toBeTruthy();
  expect(figures.textContent).not.toMatch(/(^|[^\d.])0\.00/);
});

test("a held row shows the purchase amount, not tax, and no tax figure", async () => {
  mockApi(baseRoutes());
  render(<App />);
  const held = section("Held");
  expect(await within(held).findByText(/120\.00 USD/)).toBeTruthy();
  expect(within(held).getAllByText(/purchase amount, not tax/).length).toBeGreaterThan(0);
  expect(within(held).getByText("Missing fact: user_location")).toBeTruthy();
  expect(await within(held).findByText(/Asked: spend owner/)).toBeTruthy();
  expect(within(held).getByText(/Reply status: OPEN/)).toBeTruthy();
  expect(held.textContent).not.toMatch(/(^|[^\d.])0\.00/);
  expect(held.textContent).not.toMatch(/engine|expected tax|tax amount:/i);
});

test("replay extraction is labeled replay and never live", async () => {
  mockApi(
    baseRoutes({
      "GET /api/v1/purchases": { ...PURCHASES, llm_mode: "replay", replay: true },
      "GET /api/v1/totals": { ...TOTALS, ...META },
    }),
  );
  render(<App />);
  expect(await screen.findByText("Recorded extraction: replay")).toBeTruthy();
  await waitFor(() => {
    expect(document.body.textContent).not.toMatch(/\blive\b/i);
  });
});
