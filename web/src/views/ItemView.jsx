import { useState } from "react";
import { get, money, post } from "../api.js";
import { usePoll } from "../usePoll.js";
import { statusLabel } from "./Queue.jsx";

const PATH_FOR = {
  LIABILITY: "LIABILITY",
  REFUND_CANDIDATE: "REFUND_PURSUIT",
  CHARGED_CORRECTLY: "CHARGED_CORRECTLY",
};
const EDITABLE = [
  "line_amount",
  "tax_charged",
  "service_period",
  "user_location",
  "seat_count",
  "delivery_evidence",
  "shipping",
  "invoice_date",
  "vendor_name",
];

function Value({ v }) {
  if (v === null || v === undefined) return <em>none</em>;
  return <code>{String(v)}</code>;
}

function ExtractionLabel({ mode }) {
  return mode === "replay" ? (
    <span className="tag replay">Recorded extraction (replay)</span>
  ) : (
    <span className="tag">Extraction: {mode || "unknown"}</span>
  );
}

function Determination({ det, actions, onDone }) {
  const [reason, setReason] = useState("");
  const [note, setNote] = useState("");
  const [message, setMessage] = useState("");
  const supported = det.label === "ENGINE_SUPPORTED";
  const request = det.engine_request || {};
  async function act(path, body) {
    setMessage("");
    try {
      await post(path, { ...body, expected_version: det.version });
      onDone();
    } catch (err) {
      setMessage(`${err.code || "Refused"}: ${err.message}`);
    }
  }
  const canDecide = det.unit_status === "READY_FOR_REVIEW" && supported;
  return (
    <article className={`det ${supported ? "supported" : "nhr"}`}>
      <h4>
        {det.jurisdiction} ·{" "}
        {supported ? "Reviewed rule / engine-supported" : "Needs human review"}
        {det.nhr_reason ? ` · ${det.nhr_reason}` : ""}
      </h4>
      <dl className="trace">
        <dt>Direction</dt>
        <dd>{det.direction || "—"}{det.derived_direction ? " (derived from engine expected tax vs tax charged)" : ""}</dd>
        <dt>Rule</dt>
        <dd>
          {det.rule_reference ? (
            det.rule_source_url ? (
              <a href={det.rule_source_url} target="_blank" rel="noreferrer">
                {det.rule_reference}
              </a>
            ) : (
              det.rule_reference
            )
          ) : (
            "—"
          )}
        </dd>
        <dt>Rate</dt>
        <dd><Value v={det.rate} /></dd>
        <dt>Location code</dt>
        <dd><Value v={det.location_code} /></dd>
        <dt>Math (engine)</dt>
        <dd>
          {supported && det.rate ? (
            <>
              base <Value v={request.line_amount} /> × rate <Value v={det.rate} /> → expected tax{" "}
              <Value v={det.expected_tax?.value} />; tax charged <Value v={request.tax_charged} />
            </>
          ) : (
            "No amount for this result."
          )}
        </dd>
        <dt>Engine amount</dt>
        <dd>{money(det.engine_amount) || "none"}</dd>
        <dt>Status</dt>
        <dd>
          {det.unit_status}
          {det.approver_name ? ` by ${det.approver_name} at ${det.approved_at}` : ""}
          {det.error_code ? ` (${det.error_code})` : ""}
        </dd>
      </dl>
      {canDecide ? (
        <div className="decide">
          <input
            aria-label="Decision reason"
            placeholder="Reason (required)"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
          />
          <button
            type="button"
            disabled={!reason}
            onClick={() =>
              act(`/determinations/${det.id}/review`, {
                action: "approve",
                approval_path: PATH_FOR[det.direction],
                reason,
              })
            }
          >
            Approve
          </button>
          <button
            type="button"
            className="secondary"
            disabled={!reason}
            onClick={() => act(`/determinations/${det.id}/review`, { action: "reject", reason })}
          >
            Reject
          </button>
        </div>
      ) : null}
      {!supported && actions.includes("refer") && det.unit_status !== "SENT_TO_ADVISOR" ? (
        <div className="decide">
          <input aria-label="Note" placeholder="Note" value={note} onChange={(e) => setNote(e.target.value)} />
          <button type="button" className="secondary" onClick={() => act(`/determinations/${det.id}/refer`, { action: "leave_open", note })}>
            Leave open
          </button>
          <button type="button" className="secondary" onClick={() => act(`/determinations/${det.id}/refer`, { action: "send_to_advisor", note })}>
            Send to advisor
          </button>
        </div>
      ) : null}
      {message ? <p className="error">{message}</p> : null}
    </article>
  );
}

function FactEdit({ item, onDone }) {
  const [field, setField] = useState("line_amount");
  const [lineId, setLineId] = useState(item.lines?.[0]?.id || "");
  const [value, setValue] = useState("");
  const [source, setSource] = useState("");
  const [reason, setReason] = useState("");
  const [message, setMessage] = useState("");
  async function submit(event) {
    event.preventDefault();
    setMessage("");
    try {
      await post(`/purchases/${item.id}/facts`, {
        field,
        value,
        source,
        reason,
        expected_version: item.facts_version,
        line_id: lineId || null,
      });
      setValue("");
      setSource("");
      setReason("");
      setMessage("Fact recorded. Earlier results are superseded and the item re-runs.");
      onDone();
    } catch (err) {
      setMessage(`${err.code || "Refused"}: ${err.message}`);
    }
  }
  return (
    <form className="factedit" onSubmit={submit} aria-label="Edit a fact">
      <h3>Supply or correct a fact</h3>
      <label>
        Field{" "}
        <select value={field} onChange={(e) => setField(e.target.value)}>
          {EDITABLE.map((f) => (
            <option key={f} value={f}>{f}</option>
          ))}
        </select>
      </label>
      {item.lines?.length ? (
        <label>
          Line{" "}
          <select value={lineId} onChange={(e) => setLineId(e.target.value)}>
            {item.lines.map((line, i) => (
              <option key={line.id} value={line.id}>
                {i + 1}. {line.description || line.id}
              </option>
            ))}
          </select>
        </label>
      ) : null}
      <input aria-label="Value" placeholder="Value as printed" value={value} onChange={(e) => setValue(e.target.value)} required />
      <input aria-label="Source" placeholder="Source (required), e.g. order form page 1" value={source} onChange={(e) => setSource(e.target.value)} required />
      <input aria-label="Fact reason" placeholder="Reason" value={reason} onChange={(e) => setReason(e.target.value)} required />
      <button type="submit" disabled={!value || !source || !reason}>Record fact</button>
      {message ? <p className="note">{message}</p> : null}
    </form>
  );
}

export default function ItemView({ id, tick }) {
  const { data: item, error, refresh } = usePoll(() => get(`/purchases/${id}`), [id, tick]);
  if (error) return <p className="error">{error.message}</p>;
  if (!item) return <p>Loading item…</p>;
  const current = item.determinations || [];
  const superseded = (item.past_determinations || []).filter(
    (d) => d.unit_status === "SUPERSEDED" && (d.approver_name || d.approved_at),
  );
  const extracted = Object.fromEntries(
    (item.facts || [])
      .filter((f) => f.origin === "AI_EXTRACTED")
      .map((f) => [f.line_id ? `${f.field} (line)` : f.field, f.value]),
  );
  const activeHolds = (item.holds || []).filter((h) => h.active !== false);
  return (
    <section aria-label="Item view">
      <p><a href="#/queue">← Back to queue</a></p>
      <h2>
        {item.vendor} · {item.invoice_number}
      </h2>
      <p>
        {statusLabel(item)} · invoice date {item.invoice_date || "missing"} · facts version{" "}
        {item.facts_version} <ExtractionLabel mode={item.extraction_mode} />
      </p>
      {item.stop_reason ? <p className="note">{item.stop_reason}</p> : null}
      {superseded.length && current.some((d) => d.unit_status === "READY_FOR_REVIEW") ? (
        <p className="warn" role="alert">
          An earlier approval was superseded because a fact changed. Review and approve the new version
          below; the old approval no longer counts in any total.
        </p>
      ) : null}

      {activeHolds.length ? (
        <div className="held" role="status">
          <h3>Held: no tax figure</h3>
          {activeHolds.map((h) => (
            <p key={h.id}>
              {h.reason === "MISSING_REQUIRED_FACT" ? "Missing" : "Unconfirmed"} required fact:{" "}
              <strong>{h.named_fact}</strong>
            </p>
          ))}
          {(item.questions || []).map((q) => (
            <p key={q.id} className="sub">
              Asked {q.recipient} at {q.asked_at}: “{q.prompt}” Reply: {q.answer ? q.answer : q.state}
            </p>
          ))}
          {item.purchase_amount ? (
            <p>Purchase amount, not tax: {money(item.purchase_amount)}</p>
          ) : null}
        </div>
      ) : null}

      <h3>Results</h3>
      {current.length === 0 ? <p>No current result.</p> : null}
      {current.map((det) => (
        <Determination key={det.id} det={det} actions={item.actions || []} onDone={refresh} />
      ))}

      <h3>Receipt and documents</h3>
      {(item.documents || []).length === 0 ? <p>No documents.</p> : null}
      <ul>
        {(item.documents || []).map((doc) => (
          <li key={doc.id}>
            {doc.mime_type || "document"} · {doc.pages ?? "?"} page(s) · <code>{doc.id}</code>
          </li>
        ))}
      </ul>

      <h3>
        Extracted JSON <ExtractionLabel mode={item.extraction_mode} />
      </h3>
      <pre className="json">{JSON.stringify(extracted, null, 2)}</pre>

      <h3>Admitted facts and sources</h3>
      <table>
        <thead>
          <tr><th>Field</th><th>Value</th><th>Origin</th><th>Confirmation</th><th>Rung</th><th>Source</th></tr>
        </thead>
        <tbody>
          {(item.facts || []).map((f) => (
            <tr key={f.id}>
              <td>{f.field}{f.line_id ? " (line)" : ""}</td>
              <td><Value v={f.value} /></td>
              <td>{f.origin}</td>
              <td>{f.confirmation}</td>
              <td>{f.evidence_rung ?? "—"}</td>
              <td>
                {f.source}
                {(f.evidence || []).map((ev) => (
                  <div key={ev.id} className="sub">“{ev.passage}”</div>
                ))}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <FactEdit item={item} onDone={refresh} />

      {(item.decisions || []).length ? (
        <>
          <h3>Trail</h3>
          <ul>
            {item.decisions.map((d, i) => (
              <li key={d.id || i}>
                {d.action} {d.approval_path || ""} by {d.approver_name || d.actor_name || "reviewer"}{" "}
                {d.reason ? `— ${d.reason}` : ""}
              </li>
            ))}
          </ul>
        </>
      ) : null}
    </section>
  );
}
