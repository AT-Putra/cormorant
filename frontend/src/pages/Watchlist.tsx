import { useEffect, useState } from "react";
import { api, type CreatorWatch } from "../api/client";

export default function Watchlist() {
  const [watches, setWatches] = useState<CreatorWatch[]>([]);
  const [url, setUrl] = useState("");
  const [scope, setScope] = useState<"lives" | "posts" | "both">("both");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function refresh() {
    setWatches(await api.watchlist());
  }

  useEffect(() => {
    refresh().catch(() => {});
  }, []);

  async function add() {
    if (!url.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await api.addWatch(url.trim(), scope);
      setUrl("");
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to add creator");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-6">
      <section className="card card-hover p-5 sm:p-6">
        <h2 className="mb-4 flex items-center gap-2 text-sm font-medium tracking-wide text-ink-dim uppercase">
          <span aria-hidden className="h-4 w-1 rounded-full bg-gradient-to-b from-accent to-accent-2" />
          Watch a creator
        </h2>
        <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
          <input
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="Profile or room URL"
            className="input flex-1"
          />
          <select
            value={scope}
            onChange={(e) => setScope(e.target.value as typeof scope)}
            className="input sm:w-auto"
            aria-label="What to record"
          >
            <option value="both">Lives + posts</option>
            <option value="lives">Lives only</option>
            <option value="posts">Posts only</option>
          </select>
          <button
            onClick={add}
            disabled={busy || !url.trim()}
            className="btn-primary min-h-[44px] cursor-pointer rounded-xl px-5 py-2.5 text-sm disabled:cursor-not-allowed disabled:opacity-40"
          >
            {busy ? <span className="dot-live" /> : "Watch"}
          </button>
        </div>
        {error && (
          <p role="alert" className="rise-in mt-3 text-sm text-bad">
            {error}
          </p>
        )}
      </section>

      <section className="space-y-2">
        {watches.length === 0 ? (
          <div className="card flex flex-col items-center gap-2 p-10 text-center">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.5} strokeLinecap="round" strokeLinejoin="round" aria-hidden className="h-8 w-8 text-ink-faint">
              <path d="M15 10l4.553-2.276A1 1 0 0 1 21 8.618v6.764a1 1 0 0 1-1.447.894L15 14M5 18h8a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2Z" />
            </svg>
            <p className="text-sm text-ink-faint">Nobody watched yet.</p>
            <p className="text-xs text-ink-faint/70">
              Add a creator above to auto-record lives / auto-download posts.
            </p>
          </div>
        ) : (
          <ul className="stagger space-y-2">
            {watches.map((w) => (
              <li key={w.id}>
                <div className="card card-hover flex flex-wrap items-center gap-x-4 gap-y-3 p-4">
                  {/* Avatar chip from the creator's initial */}
                  <span
                    aria-hidden
                    className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-gradient-to-br from-accent/25 to-accent-2/25 text-sm font-semibold text-accent"
                  >
                    {(w.display_name || "?").trim().charAt(0).toUpperCase()}
                  </span>
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-sm font-medium text-ink">{w.display_name}</p>
                    <span className="pill mt-0.5 bg-surface-3 text-ink-faint">{w.platform}</span>
                  </div>
                  <select
                    value={w.scope}
                    onChange={async (e) => {
                      await api.updateWatch(w.id, { scope: e.target.value });
                      await refresh();
                    }}
                    aria-label={`Scope for ${w.display_name}`}
                    className="cursor-pointer rounded-lg border border-line bg-surface px-2.5 py-1.5 text-xs text-ink-dim outline-none transition-colors focus:border-accent/50"
                  >
                    <option value="both">lives + posts</option>
                    <option value="lives">lives only</option>
                    <option value="posts">posts only</option>
                  </select>
                  <label className="flex min-h-[32px] cursor-pointer items-center gap-1.5 text-xs text-ink-dim">
                    <input
                      type="checkbox"
                      checked={w.enabled}
                      onChange={async (e) => {
                        await api.updateWatch(w.id, { enabled: e.target.checked });
                        await refresh();
                      }}
                      className="h-3.5 w-3.5 accent-cyan-400"
                    />
                    enabled
                  </label>
                  <button
                    onClick={async () => {
                      await api.removeWatch(w.id);
                      await refresh();
                    }}
                    className="min-h-[32px] cursor-pointer rounded-lg border border-bad/30 px-2.5 py-1 text-xs font-medium text-bad transition-colors hover:bg-bad/10"
                  >
                    Remove
                  </button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
