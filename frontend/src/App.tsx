import { useEffect, useState } from "react";
import { BrowserRouter, NavLink, Navigate, Route, Routes } from "react-router-dom";
import { api } from "./api/client";
import Queue from "./pages/Queue";
import Watchlist from "./pages/Watchlist";
import SettingsPage from "./pages/SettingsPage";
import Library from "./pages/Library";
import ActivityLog from "./pages/ActivityLog";
import Login from "./pages/Login";

function Shell({ children, onLogout }: { children: React.ReactNode; onLogout: () => void }) {
  const nav = [
    ["Queue", "/queue"],
    ["Watchlist", "/watchlist"],
    ["Library", "/library"],
    ["Activity", "/activity"],
    ["Settings", "/settings"],
  ] as const;
  return (
    <div className="min-h-screen bg-zinc-950 text-zinc-100">
      <header className="flex items-center gap-6 border-b border-zinc-800 px-6 py-3">
        <span className="font-semibold tracking-tight">VideoDownloader</span>
        <nav className="flex gap-1">
          {nav.map(([label, to]) => (
            <NavLink
              key={to}
              to={to}
              className={({ isActive }) =>
                `rounded-md px-3 py-1.5 text-sm ${
                  isActive ? "bg-zinc-800 text-white" : "text-zinc-400 hover:text-white"
                }`
              }
            >
              {label}
            </NavLink>
          ))}
        </nav>
        <button
          className="ml-auto rounded-md border border-zinc-700 px-3 py-1.5 text-sm text-zinc-300 hover:bg-zinc-800"
          onClick={async () => {
            await api.logout();
            onLogout();
          }}
        >
          Log out
        </button>
      </header>
      <main className="mx-auto max-w-5xl p-6">{children}</main>
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
      <div className="flex min-h-screen items-center justify-center bg-zinc-950 text-zinc-500">
        Loading…
      </div>
    );
  }
  if (state === "setup" || state === "login") {
    return (
      <Login
        mode={state === "setup" ? "setup" : "login"}
        onDone={() => setState("ready")}
      />
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
