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
      <section className="rounded-xl border border-zinc-800 bg-zinc-900 p-5">
        <h2 className="mb-3 text-sm font-medium text-zinc-400">Watch a creator</h2>
        <div className="flex flex-wrap gap-2">
          <input
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="Profile or room URL"
            className="min-w-64 flex-1 rounded-md border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm outline-none focus:border-zinc-500"
          />
          <select
            value={scope}
            onChange={(e) => setScope(e.target.value as typeof scope)}
            className="rounded-md border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm text-zinc-100 placeholder:text-zinc-500"
          >
            <option value="both">Lives + posts</option>
            <option value="lives">Lives only</option>
            <option value="posts">Posts only</option>
          </select>
          <button
            onClick={add}
            disabled={busy || !url.trim()}
            className="rounded-md bg-white px-4 py-2 text-sm font-medium text-zinc-950 hover:bg-zinc-200 disabled:opacity-50"
          >
            Watch
          </button>
        </div>
        {error && <p className="mt-2 text-sm text-red-400">{error}</p>}
      </section>

      <section className="space-y-2">
        {watches.length === 0 && (
          <p className="text-sm text-zinc-600">
            Nobody watched yet. Add a creator above to auto-record lives / auto-download posts.
          </p>
        )}
        {watches.map((w) => (
          <div
            key={w.id}
            className="flex items-center gap-4 rounded-lg border border-zinc-800 bg-zinc-900 px-4 py-3"
          >
            <div className="min-w-0 flex-1">
              <span className="text-sm font-medium">{w.display_name}</span>
              <span className="ml-2 text-xs uppercase tracking-wide text-zinc-500">
                {w.platform}
              </span>
            </div>
            <select
              value={w.scope}
              onChange={async (e) => {
                await api.updateWatch(w.id, { scope: e.target.value });
                await refresh();
              }}
              className="rounded-md border border-zinc-700 bg-zinc-950 px-2 py-1 text-xs"
            >
              <option value="both">lives + posts</option>
              <option value="lives">lives only</option>
              <option value="posts">posts only</option>
            </select>
            <label className="flex items-center gap-1.5 text-xs text-zinc-300">
              <input
                type="checkbox"
                checked={w.enabled}
                onChange={async (e) => {
                  await api.updateWatch(w.id, { enabled: e.target.checked });
                  await refresh();
                }}
              />
              enabled
            </label>
            <button
              onClick={async () => {
                await api.removeWatch(w.id);
                await refresh();
              }}
              className="rounded-md border border-red-900 px-2.5 py-1 text-xs text-red-300 hover:bg-red-950"
            >
              Remove
            </button>
          </div>
        ))}
      </section>
    </div>
  );
}
