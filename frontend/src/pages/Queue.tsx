import { useEffect, useRef, useState } from "react";
import { api, openEventSocket, type DownloadJob, type ProbeResult } from "../api/client";
import ConfirmDialog from "../components/ConfirmDialog";

function fmtBytes(n: number | null | undefined): string {
  if (!n || n <= 0) return "0 MB";
  const mb = n / 1e6;
  return mb >= 1000 ? `${(mb / 1000).toFixed(2)} GB` : `${mb.toFixed(1)} MB`;
}

const STATUS_STYLES: Record<string, string> = {
  queued: "bg-surface-3 text-ink-dim",
  probing: "bg-cyan-950/60 text-cyan-300",
  downloading: "bg-cyan-950/60 text-cyan-300",
  paused: "bg-yellow-950/60 text-yellow-300",
  paused_space_floor: "bg-orange-950/60 text-orange-300",
  done: "bg-emerald-950/60 text-emerald-300",
  failed: "bg-red-950/60 text-red-300",
  skipped: "bg-surface-3 text-ink-faint",
};

const ACTIVE_STATUSES = ["downloading", "probing"];

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
        audio_only: audioOnly,
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
      <section className="card card-hover p-5 sm:p-6">
        <h2 className="mb-4 flex items-center gap-2 text-sm font-medium tracking-wide text-ink-dim uppercase">
          <span aria-hidden className="h-4 w-1 rounded-full bg-gradient-to-b from-accent to-accent-2" />
          New download
        </h2>
        <div className="flex flex-col gap-2 sm:flex-row">
          <input
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && doProbe()}
            placeholder="Paste a bilibili / instagram / tiktok / douyin / xiaohongshu URL"
            className="input flex-1"
          />
          <button
            onClick={doProbe}
            disabled={busy || !url.trim()}
            className="btn-secondary min-h-[44px] cursor-pointer rounded-xl px-5 py-2.5 text-sm font-medium text-ink disabled:cursor-not-allowed disabled:opacity-40"
          >
            {busy && !probe ? "Probing…" : "Probe"}
          </button>
        </div>

        {probe && (
          <div className="rise-in mt-4 space-y-4 rounded-xl border border-line bg-surface/60 p-4">
            <div className="flex items-start gap-3">
              <span
                aria-hidden
                className="mt-0.5 h-10 w-10 shrink-0 rounded-lg bg-gradient-to-br from-accent/20 to-accent-2/20 p-2 text-accent"
              >
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.75} strokeLinecap="round" strokeLinejoin="round" className="h-full w-full">
                  <path d="M15 10l4.553-2.276A1 1 0 0 1 21 8.618v6.764a1 1 0 0 1-1.447.894L15 14M5 18h8a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2Z" />
                </svg>
              </span>
              <div className="min-w-0">
                <p className="text-sm font-medium leading-snug text-ink">{probe.title}</p>
                <span className="pill mt-1 bg-surface-3 text-ink-faint">{probe.platform}</span>
              </div>
            </div>
            {probe.formats.length > 0 && (
              <label className="block text-sm">
                <span className="mb-1.5 block text-xs text-ink-faint">Quality (default: best)</span>
                <select value={formatId} onChange={(e) => setFormatId(e.target.value)} className="input">
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
            <label className="flex cursor-pointer items-center gap-2.5 text-sm text-ink-dim">
              <input
                type="checkbox"
                checked={audioOnly}
                onChange={(e) => setAudioOnly(e.target.checked)}
                className="h-4 w-4 accent-cyan-400"
              />
              Audio only (MP3/M4A)
            </label>
            <div className="flex flex-col gap-2 sm:flex-row">
              <button onClick={() => start(false)} disabled={busy} className="btn-primary min-h-[44px] cursor-pointer rounded-xl px-5 py-2.5 text-sm disabled:cursor-not-allowed disabled:opacity-40">
                Download
              </button>
              <button onClick={() => start(true)} disabled={busy} className="btn-secondary min-h-[44px] cursor-pointer rounded-xl px-5 py-2.5 text-sm text-ink-dim disabled:cursor-not-allowed disabled:opacity-40">
                Re-download (ignore duplicates)
              </button>
            </div>
          </div>
        )}
        {error && (
          <p role="alert" className="rise-in mt-3 text-sm text-bad">
            {error}
          </p>
        )}
      </section>

      <section className="space-y-2">
        <h2 className="flex items-center gap-2 px-1 text-sm font-medium tracking-wide text-ink-dim uppercase">
          <span aria-hidden className="h-4 w-1 rounded-full bg-gradient-to-b from-accent to-accent-2" />
          Queue
          {jobs.some((j) => ACTIVE_STATUSES.includes(j.status)) && (
            <span className="dot-live ml-1 text-cyan-300" />
          )}
        </h2>
        {jobs.length === 0 && (
          <div className="card flex flex-col items-center gap-2 p-10 text-center">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.5} strokeLinecap="round" strokeLinejoin="round" aria-hidden className="h-8 w-8 text-ink-faint">
              <path d="M12 3v12m0 0 4-4m-4 4-4-4M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2" />
            </svg>
            <p className="text-sm text-ink-faint">No downloads yet.</p>
            <p className="text-xs text-ink-faint/70">Paste a URL above to get started.</p>
          </div>
        )}
        <ul className="stagger space-y-2">
          {jobs.map((job) => {
            const active = ACTIVE_STATUSES.includes(job.status);
            const liveInfo = live[job.id];
            return (
              <li key={job.id}>
                <div className={`card p-4 ${active ? "card-hover" : ""}`}>
                  <div className="flex items-center gap-3">
                    <span className={`pill shrink-0 ${STATUS_STYLES[job.status] ?? ""}`}>
                      {active && <span className="dot-live" />}
                      {job.status.replace("_", " ")}
                    </span>
                    <span className="min-w-0 flex-1 truncate text-sm font-medium text-ink" title={job.title ?? job.url}>
                      {job.title ?? job.url}
                    </span>
                    {/* Actions: icon row on desktop, wraps under content on mobile */}
                    <div className="hidden shrink-0 gap-1.5 sm:flex">
                      <JobButtons job={job} onAction={action} onConfirm={() => setConfirming(job)} />
                    </div>
                  </div>

                  {(active || job.status === "paused" || job.status === "paused_space_floor") &&
                    (job.progress > 0 ? (
                      <div className="progress-track mt-3 w-full">
                        <div
                          className={`progress-fill ${job.status !== "downloading" ? "opacity-50" : ""}`}
                          style={{ width: `${Math.min(100, job.progress)}%` }}
                        />
                      </div>
                    ) : active ? (
                      // No percent available (live stream): sweep + captured size and rate
                      <div className="mt-3 flex items-center gap-3">
                        <div className="progress-track w-full">
                          <div className="progress-fill progress-indeterminate" />
                        </div>
                        <p className="shrink-0 font-mono text-xs tabular-nums text-ink-dim">
                          {fmtBytes(liveInfo?.bytes)}
                          {liveInfo?.speed ? ` · ${fmtBytes(liveInfo.speed)}/s` : ""}
                        </p>
                      </div>
                    ) : (
                      <div className="progress-track mt-3 w-full opacity-40">
                        <div className="progress-fill" style={{ width: "0%" }} />
                      </div>
                    ))}
                  {liveInfo?.speed && active && job.progress > 0 && (
                    <p className="mt-1.5 font-mono text-xs tabular-nums text-ink-faint">
                      {fmtBytes(liveInfo.speed)}/s · {fmtBytes(liveInfo.bytes)}
                    </p>
                  )}
                  {job.error && (
                    <p role="alert" className="mt-2 text-xs text-bad">
                      {job.error}
                    </p>
                  )}

                  <div className="mt-3 flex gap-1.5 sm:hidden">
                    <JobButtons job={job} onAction={action} onConfirm={() => setConfirming(job)} />
                  </div>
                </div>
              </li>
            );
          })}
        </ul>
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

function JobButtons({
  job,
  onAction,
  onConfirm,
}: {
  job: DownloadJob;
  onAction: (job: DownloadJob, act: "pause" | "resume" | "cancel" | "retry") => void;
  onConfirm: () => void;
}) {
  const btn =
    "min-h-[32px] cursor-pointer rounded-lg border border-line px-2.5 py-1 text-xs font-medium text-ink-dim transition-colors hover:border-ink-faint hover:bg-surface-3 hover:text-ink";
  const btnDanger =
    "min-h-[32px] cursor-pointer rounded-lg border border-bad/30 px-2.5 py-1 text-xs font-medium text-bad transition-colors hover:bg-bad/10";
  return (
    <>
      {job.status === "downloading" && (
        <button onClick={() => onAction(job, "pause")} className={btn}>Pause</button>
      )}
      {(job.status === "paused" || job.status === "paused_space_floor") && (
        <button onClick={() => onAction(job, "resume")} className={btn}>Resume</button>
      )}
      {["queued", "probing", "downloading"].includes(job.status) && (
        <button onClick={() => onAction(job, "cancel")} className={btnDanger}>Cancel</button>
      )}
      {(job.status === "failed" || (job.status as string) === "interrupted") && (
        <button onClick={() => onAction(job, "retry")} className={btn}>Retry</button>
      )}
      <button onClick={onConfirm} className={btnDanger}>Delete</button>
    </>
  );
}

// ponytail: two render paths for action buttons (sm+ inline / mobile below-row)
// instead of a responsive container query — revisit if a third breakpoint appears.
