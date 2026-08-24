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
    <div className="flex min-h-screen items-center justify-center bg-zinc-950 px-4">
      <form
        onSubmit={submit}
        className="w-full max-w-sm space-y-4 rounded-xl border border-zinc-800 bg-zinc-900 p-6"
      >
        <h1 className="text-lg font-semibold text-zinc-100">
          {mode === "setup" ? "Create your access password" : "Sign in"}
        </h1>
        <p className="text-sm text-zinc-500">
          {mode === "setup"
            ? "First launch: this password gates the web UI. Store it somewhere safe."
            : "Enter your access password."}
        </p>
        <input
          type="password"
          autoFocus
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          placeholder="Password"
          className="w-full rounded-md border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm text-zinc-100 placeholder:text-zinc-500 outline-none focus:border-zinc-500"
        />
        {mode === "setup" && (
          <input
            type="password"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            placeholder="Confirm password"
            className="w-full rounded-md border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm text-zinc-100 placeholder:text-zinc-500 outline-none focus:border-zinc-500"
          />
        )}
        {error && <p className="text-sm text-red-400">{error}</p>}
        <button
          type="submit"
          disabled={busy}
          className="w-full rounded-md bg-white py-2 text-sm font-medium text-zinc-950 hover:bg-zinc-200 disabled:opacity-50"
        >
          {busy ? "…" : mode === "setup" ? "Set password" : "Sign in"}
        </button>
      </form>
    </div>
  );
}
