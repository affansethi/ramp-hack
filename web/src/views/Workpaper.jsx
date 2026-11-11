import { useState } from "react";
import { money, post } from "../api.js";

// WA Q4 2026 only. Ready status is simulated; nothing is transmitted.
export default function Workpaper() {
  const [paper, setPaper] = useState(null);
  const [message, setMessage] = useState("");

  async function create() {
    setMessage("");
    try {
      setPaper(await post("/workpapers", { jurisdiction: "WA", period: "2026Q4" }));
    } catch (err) {
      setMessage(`${err.code || "Refused"}: ${err.message}`);
    }
  }

  async function markReady() {
    setMessage("");
    try {
      setPaper(
        await post(`/workpapers/${paper.id}/mark-ready`, {
          expected_version: paper.version,
          fingerprint: paper.fingerprint || null,
          reason: "Reviewed for the simulated filing lifecycle",
        }),
      );
    } catch (err) {
      setMessage(`${err.code || "Refused"}: ${err.message}`);
    }
  }

  const ready = paper?.status === "READY_TO_FILE_SIMULATED";
  return (
    <section aria-label="Workpaper">
      <h2>WA Q4 2026 workpaper</h2>
      <p className="note">2026-10-01 to 2026-12-31, by invoice date. Simulated lifecycle only.</p>
      <button type="button" onClick={create}>
        Create or refresh draft
      </button>
      {message ? <p className="error">{message}</p> : null}
      {paper ? (
        <article className="workpaper">
          <p>
            Version {paper.version} ·{" "}
            {ready ? (
              <span className="tag frozen">Frozen · READY_TO_FILE_SIMULATED</span>
            ) : (
              <span className="tag">DRAFT</span>
            )}
            {paper.created_after_ready ? (
              <span className="tag warn">New draft after a frozen version</span>
            ) : null}
          </p>
          <h3>Approved liabilities</h3>
          {(paper.approved_liabilities || []).length === 0 ? (
            <p>{paper.approved_liabilities_message || "No approved liabilities"}</p>
          ) : (
            <ul>
              {paper.approved_liabilities.map((line, i) => (
                <li key={`${line.invoice}-${i}`}>
                  {line.invoice}: {money(line.amount)}
                </li>
              ))}
            </ul>
          )}
          <p>
            Held: {paper.held_count ?? 0} · Open: {paper.open_count ?? 0}
          </p>
          <h3>Excluded (no amounts)</h3>
          <ul>
            {(paper.excluded || []).map((row, i) => (
              <li key={`${row.invoice}-${i}`}>
                {row.invoice}: {row.reason}
              </li>
            ))}
          </ul>
          <h3>Refund candidates (separate list; not recovered cash)</h3>
          <ul>
            {(paper.refund_candidates?.lines || []).map((line, i) => (
              <li key={`${line.invoice}-${i}`}>
                {line.invoice}: {money(line.amount)}
              </li>
            ))}
          </ul>
          {!ready ? (
            <button type="button" onClick={markReady}>
              Mark ready (simulated)
            </button>
          ) : null}{" "}
          <a className="button secondary" href={`/api/v1/workpapers/${paper.id}/export?format=csv`}>
            Export CSV (DRAFT / SIMULATED)
          </a>
        </article>
      ) : null}
    </section>
  );
}
