import { useEffect, useState } from "react";
import "./styles.css";

const POLL_MS = 2000;
const REQUESTED_WITH = { "X-Requested-With": "taxleak" };

async function getJson(path) {
  const res = await fetch(path, { headers: REQUESTED_WITH });
  if (!res.ok) throw new Error(`${path} failed with ${res.status}`);
  return res.json();
}

function formatMoney(amount) {
  if (!amount || typeof amount.value !== "string") return null;
  return `${amount.value} ${amount.currency || "USD"}`;
}

function modeLabel(meta, purchases) {
  if (meta && typeof meta.llm_mode === "string" && meta.llm_mode) {
    return meta.llm_mode;
  }
  const modes = new Set(
    (purchases || [])
      .map((row) => row.extraction_mode)
      .filter((mode) => typeof mode === "string" && mode),
  );
  if (modes.size === 1) return [...modes][0];
  return null;
}

function Banner({ meta, purchases }) {
  const mode = modeLabel(meta, purchases);
  const isReplay =
    (meta && meta.replay === true) ||
    (typeof meta?.llm_mode === "string" && meta.llm_mode.includes("replay")) ||
    (typeof mode === "string" && mode.includes("replay"));
  return (
    <header className="banner" role="banner">
      <strong>Synthetic data • simulated filing</strong>
      <span>Synthetic demo data</span>
      {mode ? (
        <span>Recorded extraction: {isReplay ? "replay" : mode}</span>
      ) : null}
    </header>
  );
}

function Totals({ totals }) {
  if (!totals) return <p>Totals are not available yet.</p>;
  const liabilities = totals.approved_liabilities || {};
  const groups = Array.isArray(liabilities.by_jurisdiction)
    ? liabilities.by_jurisdiction
    : [];
  const refund = totals.refund_candidates || {};
  const held = totals.held || {};
  return (
    <section className="figures" aria-label="Figures">
      <div>
        <h3>Approved liabilities</h3>
        {groups.length === 0 ? (
          <p>{liabilities.message || "No approved liabilities"}</p>
        ) : (
          <ul>
            {groups.map((group) => (
              <li key={`${group.jurisdiction}-${group.period}`}>
                {group.jurisdiction} · {group.period} —{" "}
                {formatMoney(group.total)} ({group.line_count} line
                {group.line_count === 1 ? "" : "s"})
              </li>
            ))}
          </ul>
        )}
      </div>
      <div>
        <h3>{refund.label || "Refund candidates"}</h3>
        <p>
          {refund.count ?? 0} approved for pursuit, kept apart from liabilities.
        </p>
        {refund.total ? <p>{formatMoney(refund.total)}</p> : null}
        {Array.isArray(refund.rows) && refund.rows.length > 0 ? (
          <ul>
            {refund.rows.map((row) => (
              <li key={row.determination_id || row.purchase_id}>
                {row.vendor} — {formatMoney(row.amount)}
              </li>
            ))}
          </ul>
        ) : null}
      </div>
      <div>
        <h3>Held</h3>
        <p>
          {held.count ?? 0} held
          {held.purchase_amount
            ? ` — ${formatMoney(held.purchase_amount)}`
            : ""}
          {held.label ? ` (${held.label})` : ""}
        </p>
      </div>
    </section>
  );
}

function HeldRow({ purchase, detail, heldLabel }) {
  const amount = detail?.purchase_amount;
  const amountNote =
    detail?.purchase_amount_note || heldLabel || "purchase amount, not tax";
  const hold = Array.isArray(detail?.holds) ? detail.holds[0] : null;
  const missing = hold?.named_fact || purchase.stop_reason;
  const questions = Array.isArray(detail?.questions) ? detail.questions : [];
  return (
    <li>
      <strong>
        {purchase.vendor} · {purchase.invoice_number}
      </strong>
      {amount ? (
        <p>
          {formatMoney(amount)} ({amountNote})
        </p>
      ) : (
        <p>{amountNote}</p>
      )}
      {missing ? <p>Missing fact: {missing}</p> : null}
      {questions.length === 0 ? (
        <p>Question details are not available yet.</p>
      ) : (
        <ul>
          {questions.map((question) => (
            <li key={question.id}>
              Asked: {question.recipient || "unknown"} · Asked at:{" "}
              {question.asked_at || "unknown"} · Reply status:{" "}
              {question.state || "unknown"}
            </li>
          ))}
        </ul>
      )}
    </li>
  );
}

function QueueSection({ title, purchases, renderRow, emptyText }) {
  return (
    <section>
      <h2>{title}</h2>
      {purchases.length === 0 ? (
        <p>{emptyText}</p>
      ) : (
        <ul>{purchases.map((purchase) => renderRow(purchase))}</ul>
      )}
    </section>
  );
}

function simpleRow(purchase) {
  return (
    <li key={purchase.id}>
      <strong>
        {purchase.vendor} · {purchase.invoice_number}
      </strong>
      {purchase.stop_reason ? <p>{purchase.stop_reason}</p> : null}
    </li>
  );
}

export default function App() {
  const [purchases, setPurchases] = useState(null);
  const [totals, setTotals] = useState(null);
  const [meta, setMeta] = useState(null);
  const [details, setDetails] = useState({});
  const [error, setError] = useState(null);
  const [csrf, setCsrf] = useState(null);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState(null);

  useEffect(() => {
    let stopped = false;
    const detailsRef = { current: {} };
    async function login() {
      const key =
        typeof crypto !== "undefined" && crypto.randomUUID
          ? crypto.randomUUID()
          : String(Date.now());
      const res = await fetch("/api/v1/demo/login", {
        method: "POST",
        headers: {
          ...REQUESTED_WITH,
          "Idempotency-Key": key,
          "Content-Type": "application/json",
        },
        body: "{}",
      });
      if (!res.ok) throw new Error(`login failed with ${res.status}`);
      const body = await res.json();
      if (!stopped && typeof body.csrf_token === "string") {
        setCsrf(body.csrf_token);
      }
    }
    async function poll() {
      try {
        const [listing, figures] = await Promise.all([
          getJson("/api/v1/purchases"),
          getJson("/api/v1/totals"),
        ]);
        if (stopped) return;
        setPurchases(listing.purchases || []);
        setTotals(figures);
        setMeta({
          llm_mode: figures.llm_mode ?? listing.llm_mode,
          replay: figures.replay ?? listing.replay,
        });
        setError(null);
        const heldIds = (listing.purchases || [])
          .filter((row) => row.queue === "held")
          .map((row) => row.id);
        const missing = heldIds.filter((id) => !(id in detailsRef.current));
        for (const id of missing) {
          try {
            const detail = await getJson(`/api/v1/purchases/${id}`);
            if (stopped) return;
            detailsRef.current = { ...detailsRef.current, [id]: detail };
            setDetails(detailsRef.current);
          } catch {
            // Keep polling; the row renders without detail until it loads.
          }
        }
      } catch (err) {
        if (!stopped) setError(err instanceof Error ? err.message : String(err));
      }
    }
    login()
      .then(poll)
      .catch((err) => {
        if (!stopped) setError(err instanceof Error ? err.message : String(err));
      });
    const timer = setInterval(poll, POLL_MS);
    return () => {
      stopped = true;
      clearInterval(timer);
    };
  }, []);

  async function postDemo(path, payload) {
    if (!csrf) return;
    const key =
      typeof crypto !== "undefined" && crypto.randomUUID
        ? crypto.randomUUID()
        : String(Date.now());
    setBusy(true);
    setActionError(null);
    try {
      const res = await fetch(path, {
        method: "POST",
        headers: {
          ...REQUESTED_WITH,
          "Content-Type": "application/json",
          "Idempotency-Key": key,
          "X-CSRF-Token": csrf,
        },
        body: JSON.stringify(payload),
      });
      if (!res.ok) throw new Error(`${path} failed with ${res.status}`);
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  const rows = purchases || [];
  const ready = rows.filter((row) => row.queue === "ready");
  const nhr = rows.filter((row) => row.queue === "needs_human_review");
  const held = rows.filter((row) => row.queue === "held");
  const errors = rows.filter((row) => row.queue === "error");
  const other = rows.filter(
    (row) =>
      row.queue !== "ready" &&
      row.queue !== "needs_human_review" &&
      row.queue !== "held" &&
      row.queue !== "error",
  );

  return (
    <div>
      <Banner meta={meta} purchases={rows} />
      <main>
        <h1>Tax Leak</h1>
        <div className="controls">
          {["case1", "case2", "case3", "case4", "case5", "case6", "case7"].map(
            (id) => (
              <button
                key={id}
                type="button"
                disabled={!csrf || busy}
                onClick={() =>
                  postDemo("/api/v1/demo/events", { scenario: id })
                }
              >
                Load {id}
              </button>
            ),
          )}
          <button
            type="button"
            disabled={!csrf || busy}
            onClick={() => postDemo("/api/v1/demo/reset", {})}
          >
            Reset demo
          </button>
        </div>
        {actionError ? <p role="alert">{actionError}</p> : null}
        {error ? <p role="alert">Could not load data: {error}</p> : null}
        {purchases === null && !error ? <p>Loading purchases…</p> : null}
        <Totals totals={totals} />
        <QueueSection
          title="Ready for review"
          purchases={ready}
          renderRow={simpleRow}
          emptyText="No purchases are ready for review."
        />
        <QueueSection
          title="Needs human review"
          purchases={nhr}
          renderRow={simpleRow}
          emptyText="No purchases need human review."
        />
        <QueueSection
          title="Held"
          purchases={held}
          renderRow={(purchase) => (
            <HeldRow
              key={purchase.id}
              purchase={purchase}
              detail={details[purchase.id]}
              heldLabel={totals?.held?.label}
            />
          )}
          emptyText="No held purchases."
        />
        <QueueSection
          title="Errors"
          purchases={errors}
          renderRow={simpleRow}
          emptyText="No determination errors."
        />
        {other.length > 0 ? (
          <QueueSection
            title="Decided and processing"
            purchases={other}
            renderRow={simpleRow}
            emptyText="No other purchases."
          />
        ) : null}
      </main>
    </div>
  );
}
