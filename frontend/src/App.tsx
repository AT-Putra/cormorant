import { useEffect, useState } from "react";
import { BrowserRouter, NavLink, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { api } from "./api/client";
import Queue from "./pages/Queue";
import Watchlist from "./pages/Watchlist";
import SettingsPage from "./pages/SettingsPage";
import Library from "./pages/Library";
import ActivityLog from "./pages/ActivityLog";
import Login from "./pages/Login";

/* Inline stroke icons (lucide-style) — no icon dependency needed for five glyphs. */
const ICONS = {
  Queue: "M12 3v12m0 0 4-4m-4 4-4-4M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2",
  Watchlist: "M15 10l4.553-2.276A1 1 0 0 1 21 8.618v6.764a1 1 0 0 1-1.447.894L15 14M5 18h8a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2Z",
  Library: "M4 19.5A2.5 2.5 0 0 1 6.5 17H20M4 19.5A2.5 2.5 0 0 0 6.5 22H20V2H6.5A2.5 2.5 0 0 0 4 4.5v15Z",
  Activity: "M22 12h-4l-3 9L9 3l-3 9H2",
  Settings: "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6Z|M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1Z",
} as const;

function Icon({ name, className }: { name: keyof typeof ICONS; className?: string }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.75}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
      className={`h-[18px] w-[18px] ${className ?? ""}`}
    >
      {ICONS[name].split("|").map((d) => (
        <path key={d} d={d} />
      ))}
    </svg>
  );
}

const NAV = [
  ["Queue", "/queue"],
  ["Watchlist", "/watchlist"],
  ["Library", "/library"],
  ["Activity", "/activity"],
  ["Settings", "/settings"],
] as const;

type NavName = (typeof NAV)[number][0];

function Shell({ children, onLogout }: { children: React.ReactNode; onLogout: () => void }) {
  const location = useLocation();

  return (
    <div className="flex min-h-dvh flex-col">
      {/* Top bar */}
      <header className="sticky top-0 z-40 border-b border-line bg-surface/80 backdrop-blur-md">
        <div className="mx-auto flex h-14 w-full max-w-6xl items-center gap-4 px-4 sm:px-6">
          <NavLink to="/queue" className="group flex items-center gap-2.5">
            <span className="flex h-8 w-8 items-center justify-center rounded-xl bg-gradient-to-br from-accent to-accent-2 text-surface shadow-lg shadow-accent/20 transition-transform group-hover:scale-105">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2.25} strokeLinecap="round" strokeLinejoin="round" aria-hidden className="h-4 w-4">
                <path d="M12 3v12m0 0 4-4m-4 4-4-4M4 19h16" />
              </svg>
            </span>
            <span className="font-display text-lg font-semibold tracking-tight text-ink">
              Cormorant
            </span>
          </NavLink>

          <span className="hidden rounded-full border border-line bg-surface-3 px-2.5 py-0.5 text-[11px] font-medium uppercase tracking-wider text-ink-faint sm:inline-block">
            self-hosted
          </span>

          <button
            onClick={async () => {
              await api.logout();
              onLogout();
            }}
            className="ml-auto cursor-pointer rounded-lg px-3 py-1.5 text-sm text-ink-dim transition-colors hover:bg-surface-3 hover:text-ink"
          >
            Log out
          </button>
        </div>
      </header>

      {/* Desktop nav rail */}
      <div className="border-b border-line/60 bg-surface/50 max-sm:hidden">
        <nav className="mx-auto flex w-full max-w-6xl items-center gap-1 px-4 py-2 sm:px-6">
          {NAV.map(([label, to]) => (
            <NavLink
              key={to}
              to={to}
              className={({ isActive }) =>
                `relative flex cursor-pointer items-center gap-2 rounded-lg px-3.5 py-2 text-sm font-medium transition-colors ${
                  isActive ? "text-ink" : "text-ink-faint hover:bg-surface-3 hover:text-ink-dim"
                }`
              }
            >
              {({ isActive }) => (
                <>
                  {isActive && (
                    <span
                      aria-hidden
                      className="absolute inset-x-3 -bottom-2 h-0.5 rounded-full bg-gradient-to-r from-accent to-accent-2"
                    />
                  )}
                  <Icon name={label as NavName} />
                  {label}
                </>
              )}
            </NavLink>
          ))}
        </nav>
      </div>

      <main
        key={location.pathname}
        className="page mx-auto w-full max-w-6xl flex-1 px-4 pb-28 pt-6 sm:px-6 md:pb-10"
      >
        {children}
      </main>

      {/* Mobile bottom nav — top-level screens only */}
      <nav
        className="fixed inset-x-0 bottom-0 z-40 border-t border-line bg-surface/90 backdrop-blur-md md:hidden"
        style={{ paddingBottom: "env(safe-area-inset-bottom)" }}
      >
        <div className="grid grid-cols-5">
          {NAV.map(([label, to]) => (
            <NavLink
              key={to}
              to={to}
              className={({ isActive }) =>
                `flex min-h-[56px] cursor-pointer flex-col items-center justify-center gap-1 py-1.5 text-[11px] font-medium transition-colors ${
                  isActive ? "text-accent" : "text-ink-faint active:text-ink-dim"
                }`
              }
            >
              {({ isActive }) => (
                <>
                  <span className="relative">
                    <Icon name={label as NavName} />
                    {isActive && (
                      <span
                        aria-hidden
                        className="absolute -top-2 left-1/2 h-1 w-1 -translate-x-1/2 rounded-full bg-accent"
                      />
                    )}
                  </span>
                  {label}
                </>
              )}
            </NavLink>
          ))}
        </div>
      </nav>
    </div>
  );
}

export default function App() {
  const [state, setState] = useState<"loading" | "setup" | "login" | "ready">("loading");

  useEffect(() => {
    api
      .authStatus()
      .then((s) => setState(s.needs_setup ? "setup" : s.authenticated ? "ready" : "login"))
      .catch(() => setState("login"));
  }, []);

  if (state === "loading") {
    return (
      <div className="flex min-h-dvh items-center justify-center bg-surface text-ink-faint">
        <span className="dot-live mr-2 inline-block text-accent" />
        Loading…
      </div>
    );
  }
  if (state === "setup" || state === "login") {
    return (
      <Login mode={state === "setup" ? "setup" : "login"} onDone={() => setState("ready")} />
    );
  }

  return (
    <BrowserRouter>
      <Shell onLogout={() => setState("login")}>
        <Routes>
          <Route path="/" element={<Navigate to="/queue" replace />} />
          <Route path="/queue" element={<Queue />} />
          <Route path="/watchlist" element={<Watchlist />} />
          <Route path="/library" element={<Library />} />
          <Route path="/activity" element={<ActivityLog />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="*" element={<Navigate to="/queue" replace />} />
        </Routes>
      </Shell>
    </BrowserRouter>
  );
}
