import React, { lazy, Suspense } from "react";
import ReactDOM from "react-dom/client";
import { I18nextProvider } from "react-i18next";
import App from "./App";
import { ToastProvider } from "./components/PageToast";
import i18n from "./i18n";
import "./index.css";

const Prototype = import.meta.env.DEV && new URLSearchParams(location.search).get("prototype") === "overview"
  ? lazy(() => new URLSearchParams(location.search).get("legacy") === "1" ? import("./pages/OverviewPrototype") : import("./pages/DesktopPrototype"))
  : null;

document.documentElement.dataset.nativeWindow = String(Boolean(window.__TAURI_INTERNALS__));
document.documentElement.dataset.nativeLinux = String(Boolean(window.__TAURI_INTERNALS__) && /Linux/i.test(navigator.userAgent));

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <I18nextProvider i18n={i18n}>
      <ToastProvider>
        {Prototype ? <Suspense fallback={null}><Prototype /></Suspense> : <App />}
      </ToastProvider>
    </I18nextProvider>
  </React.StrictMode>,
);
