import { expect, test } from "@playwright/test";

// Runs the real app (replay extraction, temp-signed rule copies). Amounts are
// illustrative engine outputs, not signed goldens.

async function reset(page) {
  await page.goto("/#/queue");
  await expect(page.getByText("Synthetic demo data").first()).toBeVisible();
  await expect(page.getByRole("button", { name: "Simulate swipe" })).toBeVisible();
  await page.getByRole("button", { name: "Reset demo" }).click();
  await expect(page.getByText("Synthetic data reset.")).toBeVisible();
}

async function swipe(page, scenario) {
  await page.goto("/#/queue");
  await page.getByLabel("Scenario").selectOption(scenario);
  await page.getByRole("button", { name: "Simulate swipe" }).click();
  await expect(page.getByText(/Job .*SUCCEEDED/)).toBeVisible({ timeout: 20_000 });
}

test("each view renders with the synthetic banner", async ({ page }) => {
  for (const view of ["#/queue", "#/totals", "#/workpaper"]) {
    await page.goto(`/${view}`);
    await expect(page.getByText("Synthetic demo data").first()).toBeVisible();
  }
  await page.goto("/#/totals");
  await expect(page.getByText("Refund candidates are not recovered cash.")).toBeVisible();
  await page.goto("/#/workpaper");
  await expect(page.getByRole("heading", { name: "WA Q4 2026 workpaper" })).toBeVisible();
});

test("case 1: swipe, review the trace, approve, liability total updates", async ({ page }) => {
  await reset(page);
  await swipe(page, "case1");
  const row = page.getByRole("row", { name: /Synthetic Cloud Co/ });
  await expect(row).toContainText("Ready for review", { timeout: 20_000 });
  await page.goto("/#/totals");
  await expect(page.getByTestId("liabilities")).toContainText("No approved liabilities");

  await page.goto("/#/queue");
  await row.getByRole("link").click();
  await expect(page.getByText("Recorded extraction (replay)").first()).toBeVisible();
  await expect(page.getByRole("link", { name: "WA-RAS-001" })).toBeVisible();
  await expect(page.locator(".trace")).toContainText("0.1055");
  await expect(page.locator(".trace")).toContainText("1726");
  await expect(page.locator(".trace")).toContainText("151.92 USD");
  await page.getByLabel("Decision reason").fill("Reviewed the engine result");
  await page.getByRole("button", { name: "Approve" }).click();
  await expect(page.locator(".trace")).toContainText("APPROVED by Avery Chen");

  await page.goto("/#/totals");
  await expect(page.getByTestId("liabilities")).toContainText("WA 2026Q4: 151.92 USD");
  await expect(page.getByText(/DRAFT · debit Use Tax Expense/)).toBeVisible();

  await page.goto("/#/workpaper");
  await page.getByRole("button", { name: "Create or refresh draft" }).click();
  await expect(page.locator(".workpaper")).toContainText("151.92 USD");
  await page.getByRole("button", { name: "Mark ready (simulated)" }).click();
  await expect(page.getByText("Frozen · READY_TO_FILE_SIMULATED")).toBeVisible();
  const href = await page.getByRole("link", { name: /Export CSV/ }).getAttribute("href");
  const exported = await page.request.get(href);
  expect(exported.status()).toBe(200);
  const csv = await exported.text();
  expect(csv).toContain("DRAFT / SIMULATED");
  expect(csv).toContain("151.92");
});

test("case 6 holds with no tax figure and the hold rate shows", async ({ page }) => {
  await reset(page);
  await swipe(page, "case6");
  const row = page.getByRole("row", { name: /Held/ });
  await expect(row).toBeVisible({ timeout: 20_000 });
  await expect(page.getByTestId("hold-rate")).toContainText("1 of 1 items held");
  await row.getByRole("link").click();
  await expect(page.getByText("Held: no tax figure")).toBeVisible();
  await expect(page.locator(".held")).toContainText("user_location");
  // "I don't know" keeps the hold; a sourced answer releases it.
  await page.getByRole("button", { name: "I don't know" }).click();
  await expect(page.locator(".held")).toContainText("dont_know");
  await page.getByLabel("Answer", { exact: true }).fill("Seattle, WA");
  await page.getByLabel("Evidence reference").fill("Seat export from the SSO admin, November 2026");
  await page.getByRole("button", { name: "Record sourced answer" }).click();
  await expect(page.getByText("Ready for review").first()).toBeVisible({ timeout: 20_000 });
  await expect(page.locator(".held")).toHaveCount(0);
});

test("case 3 refund stays separate with a DRAFT letter; case 4 is NHR with no amount", async ({ page }) => {
  await reset(page);
  await swipe(page, "case3");
  await page.getByRole("row", { name: /Ready for review/ }).getByRole("link").click();
  await expect(page.locator(".trace")).toContainText("REFUND_CANDIDATE");
  await page.getByLabel("Decision reason").fill("Pursue the vendor amount");
  await page.getByRole("button", { name: "Approve" }).click();
  await expect(page.locator(".trace")).toContainText("APPROVED_FOR_PURSUIT");
  await page.goto("/#/totals");
  await expect(page.getByTestId("liabilities")).toContainText("No approved liabilities");
  await expect(page.getByTestId("refunds")).toContainText("1 candidate");
  await page.getByRole("button", { name: "DRAFT letter" }).click();
  await expect(page.getByText("Vendor refund letter (DRAFT, not sent)")).toBeVisible();

  await swipe(page, "case4");
  const nhr = page.getByRole("row", { name: /Needs human review/ });
  await expect(nhr).toBeVisible({ timeout: 20_000 });
  await nhr.getByRole("link").click();
  await expect(page.getByText("RULE_TRANSITION_UNRESOLVED").first()).toBeVisible();
  await expect(page.locator(".trace")).toContainText("No amount for this result.");
  await expect(page.getByRole("button", { name: "Approve" })).toHaveCount(0);
});
