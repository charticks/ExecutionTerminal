import React from "react";
import ReactDOM from "react-dom/client";
import { App } from "@/app/App";
import { mark } from "@/lib/startup";
import "@/theme/globals.css";
import "@/app/app.css";

mark("renderer:script-evaluated");

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);

// After the first paint, not after render(): this is the moment the user can
// actually see and click the Home page, which is the number that matters.
requestAnimationFrame(() => requestAnimationFrame(() => mark("renderer:first-paint")));
