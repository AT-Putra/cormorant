import { useEffect, useRef, useState } from "react";
import { api, openEventSocket, type DownloadJob, type ProbeResult } from "../api/client";
import ConfirmDialog from "../components/ConfirmDialog";

function fmtBytes(n: number | null | undefined): string {
  if (!n || n <= 0) return "0 MB";
  const mb = n / 1e6;
  return mb >= 1000 ? `${(mb / 1000).toFixed(2)} GB` : `${mb.toFixed(1)} MB`;
}

const STATUS_STYLES: Record<string, string> = {
  queued: "bg-zinc-800 text-zinc-300",
  probing: "bg-blue-950 text-blue-300",
  downloading: "bg-blue-950 text-blue-300",
  paused: "bg-yellow-950 text-yellow-300",
  paused_space_floor: "bg-orange-950 text-orange-300",
  done: "bg-emerald-950 text-emerald-300",
  failed: "bg-red-950 text-red-300",
  skipped: "bg-zinc-800 text-zinc-400",
};

export default function Queue() {
  const [jobs, setJobs] = useState<DownloadJob[]>([]);
  const [url, setUrl] = useState("");
  const [probe, setProbe] = useState<ProbeResult | null>(null);
  const [formatId, setFormatId] = useState<string>("");
  const [audioOnly, setAudioOnly] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState<DownloadJob | null>(null);
  // Per-job live tick from the event bus; not persisted server-side.
  const [live, setLive] = useState<Record<number, { speed: number | null; bytes: number | null }>>({});
  const wsRef = useRef<WebSocket | null>(null);

  async function refresh() {
    try {
      setJobs(await api.jobs());
    } catch {
      /* auth redirect handled by app shell */
    }
  }

  useEffect(() => {
    refresh();
    const ws = openEventSocket((e) => {
      const type = e["type"] as string;
      if (typeof type !== "string" || !type.startsWith("job.")) return;
      // Live streams report no total, so percent stays 0 — carry speed and
      // byte count off the event so the row still shows movement.
      if (type === "job.progress" && typeof e["job_id"] === "number") {
        setLive((m) => ({
          ...m,
          [e["job_id"] as number]: {
            speed: (e["speed"] as number) ?? null,
            bytes: (e["downloaded_bytes"] as number) ?? null,
          },
        }));
        return; // progress ticks are frequent; don't refetch the whole list
      }
      refresh();
    });
    wsRef.current = ws;
    return () => ws.close();
  }, []);

  async function doProbe() {
    setError(null);
    setProbe(null);
    if (!url.trim()) return;
    setBusy(true);
    try {
      const p = await api.probe(url.trim());
      setProbe(p);
      setFormatId("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Probe failed");
    } finally {
      setBusy(false);
    }
  }

  async function start(redownload = false) {
    setError(null);
    setBusy(true);
    try {
      await api.createJob({
        url: url.trim(),
        format_id: formatId || null,
        audio_only: redownload ? audioOnly : audioOnly,
        redownload,
      });
      setUrl("");
      setProbe(null);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to queue");
    } finally {
      setBusy(false);
    }
  }

  async function action(job: DownloadJob, act: "pause" | "resume" | "cancel" | "retry") {
    try {
      await api.jobAction(job.id, act);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : `Failed to ${act}`);
    }
  }

  return (
    <div className="space-y-6">
      <section className="rounded-xl border border-zinc-800 bg-zinc-900 p-5">
        <h2 className="mb-3 text-sm font-medium text-zinc-400">New download</h2>
        <div className="flex gap-2">
          <input
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && doProbe()}
            placeholder="Paste a bilibili / instagram / tiktok / douyin / xiaohongshu URL"
            className="flex-1 rounded-md border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm outline-none focus:border-zinc-500"
          />
          <button
            onClick={doProbe}
            disabled={busy || !url.trim()}
            className="rounded-md bg-zinc-700 px-4 py-2 text-sm font-medium hover:bg-zinc-600 disabled:opacity-50"
          >
            Probe
          </button>
        </div>

        {probe && (
          <div className="mt-4 space-y-3 rounded-lg border border-zinc-800 p-4">
            <div className="text-sm">
              <span className="font-medium">{probe.title}</span>
              <span className="ml-2 text-xs uppercase tracking-wide text-zinc-500">
                {probe.platform}
              </span>
            </div>
            {probe.formats.length > 0 && (
              <label className="block text-sm">
                <span className="mb-1 block text-xs text-zinc-500">Quality (default: best)</span>
                <select
                  value={formatId}
                  onChange={(e) => setFormatId(e.target.value)}
                  className="w-full rounded-md border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm text-zinc-100 placeholder:text-zinc-500"
                >
                  <option value="">Best available</option>
                  {probe.formats.map((f) => (
                    <option key={f.format_id} value={f.format_id}>
                      {f.resolution ?? f.format_id} {f.ext ?? ""}{" "}
                      {f.filesize_approx ? `(${(f.filesize_approx / 1e6).toFixed(0)} MB)` : ""}
                    </option>
                  ))}
                </select>
              </label>
            )}
            <label className="flex items-center gap-2 text-sm text-zinc-300">
              <input
                type="checkbox"
                checked={audioOnly}
                onChange={(e) => setAudioOnly(e.target.checked)}
              />
              Audio only (MP3/M4A)
            </label>
            <div className="flex gap-2">
              <button
                onClick={() => start(false)}
                disabled={busy}
                className="rounded-md bg-white px-4 py-2 text-sm font-medium text-zinc-950 hover:bg-zinc-200 disabled:opacity-50"
              >
                Download
              </button>
              <button
                onClick={() => start(true)}
                disabled={busy}
                className="rounded-md border border-zinc-600 px-4 py-2 text-sm text-zinc-300 hover:bg-zinc-800 disabled:opacity-50"
              >
                Re-download (ignore duplicates)
              </button>
            </div>
          </div>
        )}
        {error && <p className="mt-3 text-sm text-red-400">{error}</p>}
      </section>

      <section className="space-y-2">
        <h2 className="text-sm font-medium text-zinc-400">Queue</h2>
        {jobs.length === 0 && <p className="text-sm text-zinc-600">No downloads yet.</p>}
        {jobs.map((job) => (
          <div
            key={job.id}
            className="flex items-center gap-3 rounded-lg border border-zinc-800 bg-zinc-900 px-4 py-3"
          >
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2">
                <span className={`rounded px-1.5 py-0.5 text-[11px] uppercase ${STATUS_STYLES[job.status] ?? ""}`}>
                  {job.status.replace("_", " ")}
                </span>
                <span className="truncate text-sm">{job.title ?? job.url}</span>
              </div>
              {(job.status === "downloading" || job.status === "paused") &&
                (job.progress > 0 ? (
                  <div className="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-zinc-800">
                    <div
                      className="h-full rounded-full bg-blue-500 transition-all"
                      style={{ width: `${Math.min(100, job.progress)}%` }}
                    />
                  </div>
                ) : (
                  // No percent available (live stream): show captured size and
                  // rate instead of a bar that would sit at 0 forever.
                  <p className="mt-1 text-xs text-zinc-400">
                    {fmtBytes(live[job.id]?.bytes)} captured
                    {live[job.id]?.speed ? ` · ${fmtBytes(live[job.id]?.speed)}/s` : ""}
                  </p>
                ))}
              {job.error && <p className="mt-1 text-xs text-red-400">{job.error}</p>}
            </div>
            <div className="flex shrink-0 gap-1">
              {job.status === "downloading" && (
                <button onClick={() => action(job, "pause")} className={btn}>Pause</button>
              )}
              {(job.status === "paused" || job.status === "paused_space_floor") && (
                <button onClick={() => action(job, "resume")} className={btn}>Resume</button>
              )}
              {["queued", "probing", "downloading"].includes(job.status) && (
                <button onClick={() => action(job, "cancel")} className={btnDanger}>Cancel</button>
              )}
              {(job.status === "failed" || (job.status as string) === "interrupted") && (
                <button onClick={() => action(job, "retry")} className={btn}>Retry</button>
              )}
              <button onClick={() => setConfirming(job)} className={btnDanger}>Delete</button>
            </div>
          </div>
        ))}
      </section>

      {confirming && (
        <ConfirmDialog
          title="Remove this job?"
          message={
            <>
              “{confirming.title ?? confirming.url}” will be removed from the queue.
              {!["done", "failed", "skipped"].includes(confirming.status) &&
                " The download in progress is cancelled and partial files are discarded."}
            </>
          }
          confirmLabel="Remove"
          onCancel={() => setConfirming(null)}
          onConfirm={async () => {
            const id = confirming.id;
            setConfirming(null);
            await api.deleteJob(id);
            await refresh();
          }}
        />
      )}
    </div>
  );
}

const btn =
  "rounded-md border border-zinc-700 px-2.5 py-1 text-xs text-zinc-300 hover:bg-zinc-800";
const btnDanger =
  "rounded-md border border-red-900 px-2.5 py-1 text-xs text-red-300 hover:bg-red-950";
