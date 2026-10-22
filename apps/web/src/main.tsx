import React, { lazy, Suspense } from "react";
import ReactDOM from "react-dom/client";
const App = lazy(() => import("./App"));
import FoundationApp from "./app/FoundationApp";
import "./app/foundation.css";
import "./index.css";
import "./app.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <Suspense fallback={<p role="status">Loading workspace…</p>}>
      {new URLSearchParams(window.location.search).get("simulation") === "1" ? (
        <>
          <div className="simulation-entry">
            Fixture simulation · No backend or live operations.{" "}
            <a href="/">Open persisted application</a>
          </div>
          <App />
        </>
      ) : (
        <FoundationApp />
      )}
    </Suspense>
  </React.StrictMode>,
);
