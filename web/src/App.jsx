import { useEffect, useState } from "react";
import { get, login, post } from "./api.js";
import "./styles.css";
import ItemView from "./views/ItemView.jsx";
import Queue from "./views/Queue.jsx";
import Totals from "./views/Totals.jsx";
import Workpaper from "./views/Workpaper.jsx";

function useRoute() {
  const read = () => (window.location.hash || "#/queue").slice(1);
  const [route, setRoute] = useState(read);
  useEffect(() => {
    const onHash = () => setRoute(read());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  return route;
}

export function Banner({ session }) {
  return (
    <header className="banner" role="banner">
      <span className="banner-dot" aria-hidden="true" />
      <strong>Synthetic demo data</strong>
      <span>Synthetic data • simulated filing</span>
      {session?.llm_mode ? (
        <span>
          Extraction mode:{" "}
          {session.replay ? "Recorded extraction (replay)" : session.llm_mode}
        </span>
      ) : null}
    </header>
  );
}

export function Simulate({ onChange }) {
  const [scenarios, setScenarios] = useState([]);
  const [scenario, setScenario] = useState("case1");
  const [job, setJob] = useState(null);
  const [message, setMessage] = useState("");

  useEffect(() => {
    get("/demo/scenarios")
      .then((body) => setScenarios(body.scenarios || []))
      .catch((err) => setMessage(err.message));
  }, []);

  useEffect(() => {
    if (!job || job.status === "SUCCEEDED" || job.status === "FAILED") return;
    const timer = setInterval(async () => {
      try {
        const next = await get(`/jobs/${job.id}`);
        setJob(next);
        onChange?.();
      } catch (err) {
        setMessage(err.message);
      }
    }, 1000);
    return () => clearInterval(timer);
  }, [job, onChange]);

  async function swipe() {
    setMessage("");
    try {
      const body = await post("/demo/events", { scenario });
      setJob({ id: body.job_id, status: "QUEUED", stage: "fetch" });
      setMessage(`${body.source}: ${scenario}${body.duplicate ? " (replayed, no new work)" : ""}`);
    } catch (err) {
      setMessage(err.message);
    }
  }

  async function reset() {
    setMessage("");
    try {
      await post("/demo/reset", {});
      setJob(null);
      setMessage("Synthetic data reset.");
      onChange?.();
    } catch (err) {
      setMessage(err.message);
    }
  }

  return (
    <section className="simulate card" aria-label="Simulate">
      <div className="simulate-title">
        <strong>Simulate a Ramp event</strong>
        <span className="sub">Sends a synthetic card swipe through the pipeline</span>
      </div>
      <label>
        Scenario{" "}
        <select value={scenario} onChange={(e) => setScenario(e.target.value)}>
          {scenarios.map((name) => (
            <option key={name} value={name}>
              {name}
            </option>
          ))}
        </select>
      </label>
      <button type="button" onClick={swipe}>
        Simulate swipe
      </button>
      <button type="button" className="secondary" onClick={reset}>
        Reset demo
      </button>
      {job ? (
        <span className={`job job-${(job.status || "").toLowerCase()}`} aria-live="polite">
          Job {job.stage || ""}: {job.status}
          {job.error_code ? ` (${job.error_code})` : ""}
        </span>
      ) : null}
      {message ? <span className="note">{message}</span> : null}
    </section>
  );
}

export default function App() {
  const route = useRoute();
  const [session, setSession] = useState(null);
  const [error, setError] = useState(null);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    login()
      .then(setSession)
      .catch((err) => setError(err.message));
  }, []);

  const bump = () => setTick((n) => n + 1);
  const [view, id] = route.replace(/^\//, "").split("/");

  return (
    <div className="app">
      <Banner session={session} />
      <div className="brand">
        <span className="brand-mark" aria-hidden="true">TL</span>
        <div>
          <div className="brand-name">Tax Leak</div>
          <div className="sub">Purchase-side sales and use tax review</div>
        </div>
      </div>
      <nav className="tabs">
        <a href="#/queue" className={view === "queue" ? "active" : ""}>Review queue</a>
        <a href="#/totals" className={view === "totals" ? "active" : ""}>Totals and drafts</a>
        <a href="#/workpaper" className={view === "workpaper" ? "active" : ""}>Workpaper</a>
        {session?.person_name ? (
          <span className="who">Reviewer: {session.person_name}</span>
        ) : null}
      </nav>
      {error ? <p className="error">Demo login failed: {error}</p> : null}
      {!session ? (
        <p>Signing in to the demo…</p>
      ) : (
        <main>
          <Simulate onChange={bump} />
          {view === "item" && id ? <ItemView id={id} tick={tick} /> : null}
          {view === "totals" ? <Totals tick={tick} /> : null}
          {view === "workpaper" ? <Workpaper tick={tick} /> : null}
          {view === "queue" || !view ? <Queue tick={tick} /> : null}
        </main>
      )}
    </div>
  );
}
