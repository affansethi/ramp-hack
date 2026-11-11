import { useState } from "react";
import { allPurchases, get, money } from "../api.js";
import { usePoll } from "../usePoll.js";

// Status groups the reviewer sees. Held, NHR, error and failed/ambiguous items
// never show a tax amount; only unreviewed engine results show their copied
// amount, labeled as not in any total.
export const STATUS = {
  ready: "Ready for review",
  decided: "Decided",
  held: "Held",
  needs_human_review: "Needs human review",
  error: "Determination error",
  failed: "Failed fetch",
  ambiguous: "Possible duplicate",
  processing: "Processing",
};

export function statusLabel(row) {
  return STATUS[row.queue] || row.queue || "Unknown";
}

export default function Queue({ tick }) {
  const [label, setLabel] = useState("");
  const [direction, setDirection] = useState("");
  const params = {};
  if (label) params.label = label;
  if (direction) params.direction = direction;
  const { data, error } = usePoll(
    async () => {
      const [rows, totals, failed] = await Promise.all([
        allPurchases(params),
        get("/totals"),
        get("/jobs", { status: "FAILED" }),
      ]);
      return { rows, totals, failed: failed.jobs || [] };
    },
    [label, direction, tick],
  );
  const rows = data?.rows || [];
  const holdRate = data?.totals?.hold_rate;
  const failed = data?.failed || [];
  const duplicates = rows.filter((row) => (row.possible_duplicate_ids || []).length);

  return (
    <section aria-label="Review queue">
      <h2>Review queue</h2>
      <div className="toolbar">
        <label>
          Label{" "}
          <select value={label} onChange={(e) => setLabel(e.target.value)}>
            <option value="">All</option>
            <option value="ENGINE_SUPPORTED">Reviewed rule / engine-supported</option>
            <option value="NEEDS_HUMAN_REVIEW">Needs human review</option>
          </select>
        </label>
        <label>
          Direction{" "}
          <select value={direction} onChange={(e) => setDirection(e.target.value)}>
            <option value="">All</option>
            <option value="LIABILITY">Liability</option>
            <option value="REFUND_CANDIDATE">Refund candidate</option>
            <option value="CHARGED_CORRECTLY">Charged correctly</option>
          </select>
        </label>
        {holdRate ? (
          <span className="holdrate" data-testid="hold-rate">
            Hold rate: {holdRate.held_count} of {holdRate.item_count} items held
            {holdRate.percent != null ? ` (${holdRate.percent}%)` : ""}
          </span>
        ) : null}
      </div>
      {error ? <p className="error">{error.message}</p> : null}
      {failed.length || duplicates.length ? (
        <div className="warn" data-testid="visibility">
          <strong>Needs attention (no tax figures):</strong> {failed.length} failed fetch
          {failed.length === 1 ? "" : "es"}, {duplicates.length} possible duplicate
          {duplicates.length === 1 ? "" : "s"}
          <ul>
            {failed.map((job) => (
              <li key={job.id}>
                Failed fetch of {job.event_type} {job.ramp_object_id} after {job.attempts} attempts: {job.reason}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {rows.length === 0 ? (
        <p>No purchases yet. Use Simulate swipe to send a synthetic Ramp event.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Vendor</th>
              <th>Invoice date</th>
              <th>Amount</th>
              <th>State</th>
              <th>Label</th>
              <th>Direction</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => {
              const pending = row.pending_results || [];
              const first = pending[0];
              return (
                <tr key={row.id} className={`q-${row.queue}`}>
                  <td>
                    <a href={`#/item/${row.id}`}>{row.vendor || "(unknown vendor)"}</a>
                    <div className="sub">{row.invoice_number}</div>
                  </td>
                  <td>{row.invoice_date || "missing"}</td>
                  <td>
                    {first && money(first.engine_amount) ? (
                      <>
                        {money(first.engine_amount)}
                        <div className="sub">unreviewed; not in any total</div>
                      </>
                    ) : (
                      "—"
                    )}
                  </td>
                  <td>{pending.map((p) => p.jurisdiction).join(", ") || "—"}</td>
                  <td>{first ? "Reviewed rule / engine-supported" : row.queue === "needs_human_review" ? "Needs human review" : "—"}</td>
                  <td>{pending.map((p) => p.direction).filter(Boolean).join(", ") || "—"}</td>
                  <td>
                    {statusLabel(row)}
                    {(row.possible_duplicate_ids || []).length ? (
                      <span className="tag warn">Possible duplicate</span>
                    ) : null}
                    {row.stop_reason ? <div className="sub">{row.stop_reason}</div> : null}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </section>
  );
}
