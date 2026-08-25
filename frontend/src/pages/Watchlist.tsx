import { useEffect, useState } from "react";
import { api, type CreatorWatch } from "../api/client";
import ConfirmDialog from "../components/ConfirmDialog";

export default function Watchlist() {
  const [watches, setWatches] = useState<CreatorWatch[]>([]);
  const [url, setUrl] = useState("");
  const [liveUrl, setLiveUrl] = useState("");
  const [scope, setScope] = useState<"lives" | "posts" | "both">("both");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // A creator whose listing was walled off lands named after its bare profile
  // id, so every name is editable in place.
  const [editingId, setEditingId] = useState<number | null>(null);
  const [draftName, setDraftName] = useState("");
  const [confirming, setConfirming] = useState<CreatorWatch | null>(null);
  const [roomEditId, setRoomEditId] = useState<number | null>(null);
  const [draftRoom, setDraftRoom] = useState("");

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
      await api.addWatch(url.trim(), scope, liveUrl);
      setUrl("");
      setLiveUrl("");
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to add creator");
    } finally {
      setBusy(false);
    }
  }

  async function saveRoom(w: CreatorWatch) {
    const room = draftRoom.trim();
    setRoomEditId(null);
    if (room === (w.live_url ?? "")) return;
    await api.updateWatch(w.id, { live_url: room });
    await refresh();
  }

  function roomLabel(w: CreatorWatch) {
    if (!w.live_url) return "+ live room";
    try {
      return `room ${new URL(w.live_url).pathname.replace(/^\//, "") || "set"}`;
    } catch {
      return "room set";
    }
  }

  async function saveName(w: CreatorWatch) {
    const name = draftName.trim();
    setEditingId(null);
    if (!name || name === w.display_name) return;
    await api.updateWatch(w.id, { display_name: name });
    await refresh();
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
            className="input min-h-[44px] min-w-0 flex-1"
          />
          <input
            value={liveUrl}
            onChange={(e) => setLiveUrl(e.target.value)}
            placeholder="Live room URL (optional)"
            title="bilibili keeps live rooms on their own URL (live.bilibili.com/<room>), which a profile page never names"
            className="input min-h-[44px] min-w-0 flex-1"
          />
          <select
            value={scope}
            onChange={(e) => setScope(e.target.value as typeof scope)}
            className="input min-h-[44px] w-full shrink-0 sm:w-44"
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
                    {editingId === w.id ? (
                      <input
                        autoFocus
                        value={draftName}
                        onChange={(e) => setDraftName(e.target.value)}
                        onBlur={() => void saveName(w)}
                        onKeyDown={(e) => {
                          if (e.key === "Escape") setDraftName(w.display_name);
                          if (e.key === "Enter" || e.key === "Escape") e.currentTarget.blur();
                        }}
                        aria-label={`Rename ${w.display_name}`}
                        className="input input-sm min-h-[28px] w-full"
                      />
                    ) : (
                      <button
                        type="button"
                        title="Rename"
                        onClick={() => {
                          setDraftName(w.display_name);
                          setEditingId(w.id);
                        }}
                        className="block max-w-full cursor-pointer truncate text-left text-sm font-medium text-ink transition-colors hover:text-accent"
                      >
                        {w.display_name}
                      </button>
                    )}
                    <div className="mt-0.5 flex flex-wrap items-center gap-2">
                      <span className="pill bg-surface-3 text-ink-faint">{w.platform}</span>
                      {roomEditId === w.id ? (
                        <input
                          autoFocus
                          value={draftRoom}
                          onChange={(e) => setDraftRoom(e.target.value)}
                          onBlur={() => void saveRoom(w)}
                          onKeyDown={(e) => {
                            if (e.key === "Escape") setDraftRoom(w.live_url ?? "");
                            if (e.key === "Enter" || e.key === "Escape") e.currentTarget.blur();
                          }}
                          placeholder="https://live.bilibili.com/…"
                          aria-label={`Live room URL for ${w.display_name}`}
                          className="input input-sm min-h-[28px] w-full max-w-xs"
                        />
                      ) : (
                        <button
                          type="button"
                          title={w.live_url ?? "Where the live check points — blank means the profile page"}
                          onClick={() => {
                            setDraftRoom(w.live_url ?? "");
                            setRoomEditId(w.id);
                          }}
                          className="cursor-pointer text-xs text-ink-faint transition-colors hover:text-accent"
                        >
                          {roomLabel(w)}
                        </button>
                      )}
                    </div>
                  </div>
                  <select
                    value={w.scope}
                    onChange={async (e) => {
                      await api.updateWatch(w.id, { scope: e.target.value });
                      await refresh();
                    }}
                    aria-label={`Scope for ${w.display_name}`}
                    className="input input-sm min-h-[32px] w-full sm:w-auto"
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
                    onClick={() => setConfirming(w)}
                    aria-label={`Remove ${w.display_name}`}
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

      {confirming && (
        <ConfirmDialog
          title="Stop watching this creator?"
          message={
            <>
              “{confirming.display_name}” will no longer be polled, so new lives and posts stop
              being picked up. Already-downloaded files stay in your library.
            </>
          }
          confirmLabel="Remove"
          onCancel={() => setConfirming(null)}
          onConfirm={async () => {
            const id = confirming.id;
            setConfirming(null);
            await api.removeWatch(id);
            await refresh();
          }}
        />
      )}
    </div>
  );
}
