import { StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { checkReadiness, type Readiness } from "./health";
import "./styles.css";

const statusCopy = {
  ready: ["Database connected", "The API reached PostgreSQL successfully."],
  not_ready: ["Database unavailable", "The API is responding, but PostgreSQL is not ready or configured."],
  unreachable: ["API check failed", "The browser could not verify the API. Check that the local services are running."],
} as const;

function App() {
  const [status, setStatus] = useState<Readiness | null>(null);
  const [checking, setChecking] = useState(true);
  const [checkedAt, setCheckedAt] = useState<Date | null>(null);

  async function refresh() {
    setChecking(true);
    setStatus(await checkReadiness());
    setCheckedAt(new Date());
    setChecking(false);
  }

  useEffect(() => {
    let current = true;
    void checkReadiness().then((result) => {
      if (current) {
        setStatus(result);
        setCheckedAt(new Date());
        setChecking(false);
      }
    });
    return () => { current = false; };
  }, []);

  return (
    <main>
      <header><a className="brand" href="/">SignalDesk<span>SD</span></a><span className="stage">Foundation build</span></header>
      <section className="intro">
        <p className="eyebrow">From release notes to clear decisions</p>
        <h1>A quieter place<br />to keep up.</h1>
        <p className="lede">Follow the software your projects depend on. Review what changed, record a decision, and track what comes next.</p>
      </section>
      <section className="status-card" aria-labelledby="status-title">
        <div className="card-heading"><span className={`indicator ${status ?? "pending"}`} /><h2 id="status-title">Connection check</h2></div>
        <div role="status" aria-live="polite" aria-busy={checking}>
          <h3>{checking ? "Checking services…" : status ? statusCopy[status][0] : "Not checked"}</h3>
          <p>{checking ? "Contacting the API and its database." : status ? statusCopy[status][1] : "Run a check to see the current state."}</p>
        </div>
        <button onClick={() => void refresh()} disabled={checking}>{checking ? "Checking…" : "Check again"}</button>
        {checkedAt && <p className="timestamp">Last checked {checkedAt.toLocaleTimeString()}</p>}
      </section>
      <aside><strong>What is available today?</strong><p>This foundation verifies the browser → API → database connection. Sign-in, projects, collection, and review workflows are still being built. Database connectivity does not mean collection is running.</p></aside>
      <footer>SignalDesk · Portfolio MVP <span>Build 0.1.0</span></footer>
    </main>
  );
}

createRoot(document.getElementById("root")!).render(<StrictMode><App /></StrictMode>);
