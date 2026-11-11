import { useState } from "react";
import { get, money } from "../api.js";
import { usePoll } from "../usePoll.js";

// Figures side by side, never combined. Only exact API strings are shown.
export default function Totals({ tick }) {
  const { data, error } = usePoll(async () => {
    const [totals, journals] = await Promise.all([
      get("/totals"),
      get("/drafts/journal-entries"),
    ]);
    return { totals, journals: journals.drafts || [] };
  }, [tick]);
  const [letter, setLetter] = useState(null);
  const [letterError, setLetterError] = useState("");

  if (error) return <p className="error">{error.message}</p>;
  if (!data) return <p>Loading totals…</p>;
  const { totals, journals } = data;
  const liabilities = totals.approved_liabilities || {};
  const groups = liabilities.by_jurisdiction || [];
  const refunds = totals.refund_candidates || {};
  const held = totals.held || {};
  const byDirection = totals.unreviewed?.by_direction || [];

  async function openLetter(id) {
    setLetterError("");
    try {
      setLetter(await get(`/drafts/refund-letters/${id}`));
    } catch (err) {
      setLetterError(err.message);
    }
  }

  return (
    <section aria-label="Totals and drafts">
      <h2>Totals</h2>
      <div className="figures">
        <div className="panel" data-testid="liabilities">
          <h3>Approved liabilities</h3>
          {groups.length === 0 ? (
            <p>{liabilities.message || "No approved liabilities"}</p>
          ) : (
            <ul>
              {groups.map((g) => (
                <li key={`${g.jurisdiction}-${g.period}`}>
                  {g.jurisdiction} {g.period}: {money(g.total)} ({g.line_count} line
                  {g.line_count === 1 ? "" : "s"})
                </li>
              ))}
            </ul>
          )}
        </div>
        <div className="panel" data-testid="refunds">
          <h3>Refund candidates approved for pursuit</h3>
          <p className="note">Refund candidates are not recovered cash.</p>
          <p>
            {refunds.count || 0} candidate{refunds.count === 1 ? "" : "s"}
            {refunds.total ? `: ${money(refunds.total)}` : ""}
          </p>
          <ul>
            {(refunds.rows || []).map((row) => (
              <li key={row.determination_id}>
                {row.vendor} {row.jurisdiction}: {money(row.amount)}{" "}
                {row.refund_letter_id ? (
                  <button type="button" className="link" onClick={() => openLetter(row.refund_letter_id)}>
                    DRAFT letter
                  </button>
                ) : null}
              </li>
            ))}
          </ul>
        </div>
        <div className="panel" data-testid="held">
          <h3>Held</h3>
          <p>
            {held.count || 0} item{held.count === 1 ? "" : "s"}
            {held.purchase_amount ? ` · ${money(held.purchase_amount)} ${held.label || "purchase amount, not tax"}` : ""}
          </p>
        </div>
        <div className="panel">
          <h3>Open items (no amount)</h3>
          <p>Needs human review: {totals.open_needs_human_review?.count ?? 0}</p>
          <p>Determination errors: {totals.determination_errors?.count ?? 0}</p>
          <p>
            Unreviewed:{" "}
            {byDirection.length
              ? byDirection.map((d) => `${d.direction} ${d.count}`).join(", ")
              : "0"}
          </p>
        </div>
      </div>

      <h2>Drafts</h2>
      <h3>Journal entries (DRAFT)</h3>
      {journals.length === 0 ? <p>No draft journal entries.</p> : null}
      <ul>
        {journals.map((j) => (
          <li key={j.id}>
            DRAFT · debit {j.debit_account} / credit {j.credit_account} · {money(j.amount)}
          </li>
        ))}
      </ul>
      {letterError ? <p className="error">{letterError}</p> : null}
      {letter ? (
        <article className="letter" aria-label="Refund letter draft">
          <h3>Vendor refund letter (DRAFT, not sent)</h3>
          <p>
            {letter.vendor} · invoice {letter.invoice_number} · amount in question {money(letter.amount)}
          </p>
          <p>{letter.basis_text || <em>No basis text.</em>}</p>
          <p className="note">{letter.basis_note}</p>
        </article>
      ) : null}
    </section>
  );
}
