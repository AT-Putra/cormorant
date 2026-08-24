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
      <div className="flex items-center gap-2">
        {["", "bilibili", "instagram", "tiktok", "douyin", "xhs"].map((p) => (
          <button
            key={p || "all"}
            onClick={() => setPlatform(p)}
            className={`rounded-md px-3 py-1.5 text-xs ${
              platform === p ? "bg-zinc-700 text-white" : "border border-zinc-800 text-zinc-400 hover:text-white"
            }`}
          >
            {p || "All"}
          </button>
        ))}
      </div>

      {items.length === 0 && <p className="text-sm text-zinc-600">Library is empty.</p>}

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
        {items.map((item) => (
          <div key={item.id} className="overflow-hidden rounded-lg border border-zinc-800 bg-zinc-900">
            <button
              onClick={() => item.media_type !== "image_set" && setPlaying(item)}
              className="block aspect-video w-full bg-zinc-950"
            >
              {item.has_thumbnail ? (
                <img
                  src={`/api/library/${item.id}/thumbnail`}
                  alt={item.title}
                  className="h-full w-full object-cover"
                />
              ) : (
                <span className="flex h-full items-center justify-center text-xs text-zinc-600">
                  no thumbnail
                </span>
              )}
            </button>
            <div className="space-y-1.5 p-2.5">
              <p className="truncate text-xs font-medium" title={item.title}>
                {item.title}
              </p>
              <p className="text-[11px] uppercase tracking-wide text-zinc-500">
                {item.platform} · {item.creator}
                {item.size_bytes ? ` · ${(item.size_bytes / 1e6).toFixed(1)} MB` : ""}
              </p>
              <div className="flex gap-2">
                <a
                  href={`/api/library/${item.id}/stream?download=1`}
                  download
                  className="rounded border border-zinc-700 px-2 py-0.5 text-[11px] text-zinc-300 hover:bg-zinc-800"
                >
                  Download
                </a>
                <button
                  onClick={() => setConfirming(item)}
                  className="rounded border border-red-900 px-2 py-0.5 text-[11px] text-red-300 hover:bg-red-950"
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
              “{confirming.title}” and its file will be removed from disk. This cannot be
              undone.
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
          className="fixed inset-0 flex items-center justify-center bg-black/80 p-6"
          onClick={() => setPlaying(null)}
        >
          <video
            src={`/api/library/${playing.id}/stream`}
            controls
            autoPlay
            className="max-h-full max-w-full rounded-lg"
            onClick={(e) => e.stopPropagation()}
          />
        </div>
      )}
    </div>
  );
}
