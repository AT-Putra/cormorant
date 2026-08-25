import { useEffect, useState } from "react";
import { api, type LibraryItem } from "../api/client";
import ConfirmDialog from "../components/ConfirmDialog";

export default function Library() {
  const [items, setItems] = useState<LibraryItem[]>([]);
  const [platform, setPlatform] = useState("");
  const [playing, setPlaying] = useState<LibraryItem | null>(null);
  const [confirming, setConfirming] = useState<LibraryItem | null>(null);

  async function refresh() {
    setItems(await api.library({ platform: platform || undefined }));
  }

  useEffect(() => {
    refresh().catch(() => {});
  }, [platform]);

  return (
    <div className="space-y-4">
      {/* Platform filter chips */}
      <div className="flex flex-wrap items-center gap-2" role="group" aria-label="Filter by platform">
        {["", "bilibili", "instagram", "tiktok", "douyin", "xhs"].map((p) => (
          <button
            key={p || "all"}
            onClick={() => setPlatform(p)}
            aria-pressed={platform === p}
            className={`min-h-[32px] cursor-pointer rounded-full px-3.5 py-1.5 text-xs font-medium capitalize transition-all ${
              platform === p
                ? "bg-gradient-to-r from-accent to-accent-2 text-surface shadow-md shadow-accent/20"
                : "border border-line bg-surface-2 text-ink-faint hover:border-ink-faint/50 hover:text-ink-dim"
            }`}
          >
            {p || "All"}
          </button>
        ))}
      </div>

      {items.length === 0 && (
        <div className="card flex flex-col items-center gap-2 p-10 text-center">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.5} strokeLinecap="round" strokeLinejoin="round" aria-hidden className="h-8 w-8 text-ink-faint">
            <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20M4 19.5A2.5 2.5 0 0 0 6.5 22H20V2H6.5A2.5 2.5 0 0 0 4 4.5v15Z" />
          </svg>
          <p className="text-sm text-ink-faint">Library is empty.</p>
        </div>
      )}

      <div className="stagger grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
        {items.map((item) => (
          <div key={item.id} className="card card-hover group overflow-hidden">
            <button
              onClick={() => item.media_type !== "image_set" && setPlaying(item)}
              className={`relative block aspect-video w-full overflow-hidden bg-black ${
                item.media_type !== "image_set" ? "cursor-pointer" : "cursor-default"
              }`}
              aria-label={item.media_type !== "image_set" ? `Play ${item.title}` : undefined}
            >
              {item.has_thumbnail ? (
                <>
                  <img
                    src={`/api/library/${item.id}/thumbnail`}
                    alt=""
                    loading="lazy"
                    className="h-full w-full object-cover transition-transform duration-300 group-hover:scale-[1.04]"
                  />
                  {item.media_type !== "image_set" && (
                    // Play affordance fades in over the thumbnail on hover/focus
                    <span
                      aria-hidden
                      className="absolute inset-0 flex items-center justify-center bg-black/30 opacity-0 transition-opacity duration-200 group-hover:opacity-100 group-focus-visible:opacity-100"
                    >
                      <span className="flex h-11 w-11 items-center justify-center rounded-full bg-white/90 text-surface shadow-lg">
                        <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden className="ml-0.5 h-5 w-5">
                          <path d="M8 5.14v13.72a1 1 0 0 0 1.5.86l11-6.86a1 1 0 0 0 0-1.72l-11-6.86a1 1 0 0 0-1.5.86Z" />
                        </svg>
                      </span>
                    </span>
                  )}
                </>
              ) : (
                <span className="flex h-full items-center justify-center text-xs text-ink-faint">no thumbnail</span>
              )}
            </button>
            <div className="space-y-2 p-3">
              <p className="truncate text-xs font-medium text-ink" title={item.title}>
                {item.title}
              </p>
              <p className="text-[11px] uppercase tracking-wide text-ink-faint">
                {item.platform} · {item.creator}
                {item.size_bytes ? ` · ${(item.size_bytes / 1e6).toFixed(1)} MB` : ""}
              </p>
              <div className="flex gap-1.5">
                <a
                  href={`/api/library/${item.id}/stream?download=1`}
                  download
                  className="min-h-[28px] cursor-pointer rounded-lg border border-line px-2 py-1 text-[11px] font-medium leading-[18px] text-ink-dim transition-colors hover:border-ink-faint hover:text-ink"
                >
                  Download
                </a>
                <button
                  onClick={() => setConfirming(item)}
                  aria-label={`Delete ${item.title}`}
                  className="min-h-[28px] cursor-pointer rounded-lg border border-bad/30 px-2 py-1 text-[11px] font-medium leading-[18px] text-bad transition-colors hover:bg-bad/10"
                >
                  Delete
                </button>
              </div>
            </div>
          </div>
        ))}
      </div>

      {confirming && (
        <ConfirmDialog
          title="Delete this item?"
          message={
            <>
              “{confirming.title}” and its file will be removed from disk. This cannot be undone.
            </>
          }
          onCancel={() => setConfirming(null)}
          onConfirm={async () => {
            const id = confirming.id;
            setConfirming(null);
            await api.deleteLibraryItem(id);
            await refresh();
          }}
        />
      )}

      {playing && (
        <div
          role="dialog"
          aria-modal="true"
          aria-label={playing.title}
          className="rise-in fixed inset-0 z-50 flex items-center justify-center bg-black/85 p-4 backdrop-blur-sm sm:p-6"
          onClick={() => setPlaying(null)}
        >
          <video
            src={`/api/library/${playing.id}/stream`}
            controls
            autoPlay
            className="max-h-full max-w-full rounded-xl shadow-2xl"
            onClick={(e) => e.stopPropagation()}
          />
        </div>
      )}
    </div>
  );
}
