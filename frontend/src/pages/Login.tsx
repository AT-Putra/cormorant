import { useState } from "react";
import { api } from "../api/client";

export default function Login({ mode, onDone }: { mode: "setup" | "login"; onDone: () => void }) {
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    if (mode === "setup" && password !== confirm) {
      setError("Passwords do not match");
      return;
    }
    if (password.length < 4) {
      setError("Password must be at least 4 characters");
      return;
    }
    setBusy(true);
    try {
      if (mode === "setup") await api.setup(password);
      else await api.login(password);
      onDone();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex min-h-dvh items-center justify-center px-4">
      <form
        onSubmit={submit}
        className="page card w-full max-w-sm space-y-5 p-7 shadow-2xl shadow-black/40"
        style={{ animationDuration: "0.4s" }}
      >
        {/* Brand mark */}
        <div className="flex flex-col items-center gap-3 pb-1 text-center">
          <span className="flex h-14 w-14 items-center justify-center rounded-2xl bg-gradient-to-br from-accent to-accent-2 text-surface shadow-lg shadow-accent/25">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2.25} strokeLinecap="round" strokeLinejoin="round" aria-hidden className="h-7 w-7">
              <path d="M12 3v12m0 0 4-4m-4 4-4-4M4 19h16" />
            </svg>
          </span>
          <div>
            <h1 className="font-display text-xl font-semibold tracking-tight text-ink">Cormorant</h1>
            <p className="mt-1.5 text-sm leading-snug text-ink-faint">
              {mode === "setup"
                ? "First launch: create the password that gates this UI. Store it somewhere safe."
                : "Enter your access password."}
            </p>
          </div>
        </div>

        <div className="space-y-3">
          <input
            type="password"
            autoFocus
            autoComplete={mode === "setup" ? "new-password" : "current-password"}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="Password"
            className="input"
            aria-label="Password"
          />
          {mode === "setup" && (
            <input
              type="password"
              autoComplete="new-password"
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
              placeholder="Confirm password"
              className="input"
              aria-label="Confirm password"
            />
          )}
        </div>

        {error && (
          <p role="alert" className="rise-in rounded-lg border border-bad/30 bg-bad/10 px-3 py-2 text-sm text-bad">
            {error}
          </p>
        )}

        <button
          type="submit"
          disabled={busy}
          className="btn-primary flex min-h-[44px] w-full cursor-pointer items-center justify-center rounded-xl py-2.5 text-sm disabled:cursor-not-allowed disabled:opacity-40"
        >
          {busy ? <span className="dot-live" /> : mode === "setup" ? "Set password" : "Sign in"}
        </button>
      </form>
    </div>
  );
}
