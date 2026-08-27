import { useEffect, useRef, useState } from "react";
import {
  api,
  openEventSocket,
  type DownloadJob,
  type ProbeResult,
  type QualityOption,
  type Recording,
} from "../api/client";
import ConfirmDialog from "../components/ConfirmDialog";

function fmtBytes(n: number | null | undefined): string {
  if (!n || n <= 0) return "0 MB";
  const mb = n / 1e6;
  return mb >= 1000 ? `${(mb / 1000).toFixed(2)} GB` : `${mb.toFixed(1)} MB`;
}

// Codec ids arrive as full RFC 6381 strings (av01.0.09M.08, hvc1.1.6.L150) —
// unreadable in a dropdown, so map the family prefix to the name people use.
const VCODEC_NAMES: [RegExp, string][] = [
  [/^av0?1/, "AV1"],
  [/^(hev1|hvc1|h\.?265|hevc)/, "H.265"],
  [/^(avc1|avc3|avc|h\.?264)/, "H.264"],
  [/^vp0?9/, "VP9"],
  [/^vp0?8/, "VP8"],
];
const ACODEC_NAMES: [RegExp, string][] = [
  [/^mp4a/, "AAC"],
  [/^opus/, "Opus"],
  [/^(ec-3|eac3)/, "E-AC3"],
  [/^ac-3/, "AC3"],
  [/^flac/, "FLAC"],
  [/^mp3/, "MP3"],
];
// Live rooms label tiers in Chinese only. Keep the original alongside the
// translation so the entry still matches what the bilibili player shows.
const NOTE_NAMES: Record<string, string> = {
  "原画": "Source (原画)",
  "蓝光": "Blu-ray (蓝光)",
  "超清": "Ultra HD (超清)",
  "高清": "HD (高清)",
  "流畅": "Smooth (流畅)",
};
const PROTOCOL_NAMES: Record<string, string> = {
  m3u8_native: "HLS",
  m3u8: "HLS",
  http_dash_segments: "DASH",
  https: "HTTPS",
  http: "HTTP",
  rtmp: "RTMP",
};

function codecName(raw: string | null | undefined, table: [RegExp, string][]): string | null {
  if (!raw || raw === "none") return null;
  const hit = table.find(([re]) => re.test(raw));
  return hit ? hit[1] : raw.split(".")[0];
}

function qualityLabel(f: QualityOption): string {
  const parts: string[] = [];
  const vcodec = codecName(f.vcodec, VCODEC_NAMES);
  const acodec = codecName(f.acodec, ACODEC_NAMES);

  if (f.resolution && f.resolution !== "audio only") parts.push(f.resolution);
  else if (!vcodec) parts.push("Audio only");
  if (f.fps) parts.push(`${Math.round(f.fps)}fps`);
  // Live has no resolution/fps/bitrate at all; the tier note is the only
  // thing that says how good the stream is, so it leads there.
  const note = f.format_note?.trim();
  if (note && !f.resolution) parts.push(NOTE_NAMES[note] ?? note);
  if (vcodec) parts.push(vcodec);
  if (acodec) parts.push(acodec);
  if (f.tbr) parts.push(`${Math.round(f.tbr)}k`);
  if (f.ext) parts.push(f.ext === "fmp4" ? "fMP4" : f.ext.toUpperCase());
  // Protocol only earns a slot when it distinguishes otherwise-identical
  // entries — i.e. the live case, where plain https means an FLV pull.
  if (!f.resolution && f.protocol) parts.push(PROTOCOL_NAMES[f.protocol] ?? f.protocol);
  if (f.filesize_approx) {
    // Video-only sizes understate the merged file; the +audio flag is why the
    // finished download is bigger than the number in this list.
    parts.push(vcodec && !acodec ? `${fmtBytes(f.filesize_approx)} +audio` : fmtBytes(f.filesize_approx));
  }
  return parts.join(" · ");
}

// Bilibili live serves the same tier from several CDN endpoints, so labels
// collide; the raw format_id is appended only to the ones that would tie.
// `best` is the id "Best available" actually resolves to, cap included. When
// yt-dlp plans to mux it reports a merged id ("137+140"), and then no single
// row IS the pick — each half is only part of it, which the label has to say
// rather than promising two different rows are each the best one.
function qualityLabels(
  formats: QualityOption[],
  best?: string | null,
): { format_id: string; label: string }[] {
  const labels = formats.map(qualityLabel);
  const seen = new Map<string, number>();
  labels.forEach((l) => seen.set(l, (seen.get(l) ?? 0) + 1));
  const chosen = new Set((best ?? "").split("+").filter(Boolean));
  const suffix = chosen.size > 1 ? "part of best available" : "best available";
  return formats.map((f, i) => {
    const base = (seen.get(labels[i]) ?? 0) > 1 ? `${labels[i]} · ${f.format_id}` : labels[i];
    return {
      format_id: f.format_id,
      label: chosen.has(f.format_id) ? `${base} · ${suffix}` : base,
    };
  });
}

// Backend stores naive UTC; tag with Z so Date parses the real instant.
function toDate(iso: string): Date {
  return new Date(/Z|[+-]\d{2}:\d{2}$/.test(iso) ? iso : `${iso}Z`);
}

function fmtTime(iso: string): string {
  const d = toDate(iso);
  const opts: Intl.DateTimeFormatOptions =
    d.toDateString() === new Date().toDateString()
      ? { hour: "2-digit", minute: "2-digit" }
      : { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" };
  // NBSP keeps each timestamp atomic; lines wrap only at the "·" separator.
  return d.toLocaleString(undefined, opts).replace(/ /g, " ");
}

// Live recordings have no percentage to show — nothing reports a total for a
// stream that has not ended. Elapsed time next to a byte count that climbs
// between polls is what tells a running capture from a dead engine.
function fmtElapsed(iso: string | null): string {
  if (!iso) return "—";
  const secs = Math.max(0, Math.floor((Date.now() - toDate(iso).getTime()) / 1000));
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  return h > 0 ? `${h}h ${m}m` : `${m}m ${secs % 60}s`;
}

const RECORDING_ACTIVE = "recording";

async function copyText(text: string): Promise<void> {
  if (navigator.clipboard?.writeText) return navigator.clipboard.writeText(text);
  // Fallback for plain-http LAN access where the async clipboard is absent.
  const ta = document.createElement("textarea");
  ta.value = text;
  document.body.appendChild(ta);
  ta.select();
  document.execCommand("copy");
  ta.remove();
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
  const [recordings, setRecordings] = useState<Recording[]>([]);
  // Re-render on a timer so elapsed time advances between events; a capture
  // can run for hours without publishing anything at all.
  const [, setTick] = useState(0);

  async function refresh() {
    try {
      const [j, r] = await Promise.all([api.jobs(), api.recordings()]);
      setJobs(j);
      setRecordings(r);
    } catch {
      /* auth redirect handled by app shell */
    }
  }

  useEffect(() => {
    refresh();
    const ws = openEventSocket((e) => {
      const type = e["type"] as string;
      if (typeof type !== "string") return;
      // recording.* was dropped here, so a capture starting, ending or being
      // rescued changed nothing on screen until a manual reload.
      if (!type.startsWith("job.") && !type.startsWith("recording.")) return;
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
    // 5s: fast enough that a stalled capture is obvious, slow enough that an
    // idle page is not polling for nothing.
    const timer = window.setInterval(() => {
      setTick((n) => n + 1);
      void refresh();
    }, 5000);
    return () => {
      ws.close();
      window.clearInterval(timer);
    };
  }, []);

  async function stopRecording(id: number) {
    setBusy(true);
    try {
      await api.stopRecording(id);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

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
            {/* An empty list used to render nothing at all, so a probe that
                found no formats looked identical to one that had not run —
                three separate bugs reached that same silent dead end. Say so
                instead, and make clear the download still works. */}
            {probe.formats.length > 0 ? (
              <label className="block text-sm">
                <span className="mb-1.5 block text-xs text-ink-faint">Quality (default: best)</span>
                <select value={formatId} onChange={(e) => setFormatId(e.target.value)} className="input">
                  <option value="">Best available</option>
                  {qualityLabels(probe.formats, probe.best_format_id).map((o) => (
                    <option key={o.format_id} value={o.format_id}>
                      {o.label}
                    </option>
                  ))}
                </select>
              </label>
            ) : (
              <p className="rounded-lg border border-line bg-surface-3/60 px-3 py-2.5 text-xs leading-relaxed text-ink-faint">
                No selectable formats came back for this URL. Downloading still
                works and will take the best available stream — there is just
                nothing here to choose between.
              </p>
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

      {/* Recordings had no surface at all: /api/recordings, stop and retry all
          existed, but nothing rendered them, so a capture running for hours
          was invisible and there was no way to end it from the UI. */}
      {recordings.length > 0 && (
        <section className="space-y-2">
          <h2 className="flex items-center gap-2 px-1 text-sm font-medium tracking-wide text-ink-dim uppercase">
            <span aria-hidden className="h-4 w-1 rounded-full bg-gradient-to-b from-accent to-accent-2" />
            Recordings
            {recordings.some((r) => r.status === RECORDING_ACTIVE) && (
              <span className="dot-live ml-1 text-cyan-300" />
            )}
          </h2>
          {recordings.slice(0, 6).map((r) => {
            const isLive = r.status === RECORDING_ACTIVE;
            return (
              <div key={r.id} className="card flex items-center justify-between gap-3 p-4">
                <div className="min-w-0">
                  <p className="truncate text-sm font-medium text-ink">
                    {r.creator}
                    <span className="ml-2 text-xs font-normal text-ink-faint">{r.platform}</span>
                  </p>
                  <p className="mt-0.5 text-xs text-ink-faint">
                    {isLive ? (
                      <>
                        recording for {fmtElapsed(r.started_at)} · {fmtBytes(r.size_bytes)} on disk
                      </>
                    ) : (
                      <>
                        {r.status}
                        {r.ended_at ? ` · ended ${fmtTime(r.ended_at)}` : ""}
                        {r.size_bytes ? ` · ${fmtBytes(r.size_bytes)}` : ""}
                      </>
                    )}
                  </p>
                  {r.error && <p className="mt-1 text-xs text-rose-300">{r.error}</p>}
                </div>
                <div className="flex shrink-0 items-center gap-2">
                  <span className={`pill ${isLive ? "bg-cyan-950/40 text-cyan-300" : "bg-surface-3 text-ink-faint"}`}>
                    {isLive ? "live" : r.status}
                  </span>
                  {isLive && (
                    <button
                      onClick={() => stopRecording(r.id)}
                      disabled={busy}
                      className="btn-secondary min-h-[36px] cursor-pointer rounded-lg px-3 py-1.5 text-xs text-ink-dim disabled:cursor-not-allowed disabled:opacity-40"
                    >
                      Stop
                    </button>
                  )}
                </div>
              </div>
            );
          })}
        </section>
      )}

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

                  <JobMeta job={job} />

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

// Icon-only copy button (44px hit area, 16px glyph) — label lives on aria/title.
function CopyLinkButton({ url }: { url: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      onClick={async () => {
        try {
          await copyText(url);
          setCopied(true);
          setTimeout(() => setCopied(false), 1500);
        } catch {
          /* clipboard unavailable */
        }
      }}
      title={`Copy source link: ${url}`}
      aria-label={`Copy source link: ${url}`}
      className="group/copy flex h-11 w-11 cursor-pointer items-center justify-center rounded-lg text-ink-faint transition-colors hover:bg-surface-3 hover:text-ink"
    >
      {copied ? (
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" aria-hidden className="h-4 w-4 text-emerald-400">
          <path d="m5 13 4 4L19 7" />
        </svg>
      ) : (
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.75} strokeLinecap="round" strokeLinejoin="round" aria-hidden className="h-4 w-4">
          <path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71" />
          <path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71" />
        </svg>
      )}
    </button>
  );
}

// Meta strip: started/finished times + copyable source link.
function JobMeta({ job }: { job: DownloadJob }) {
  const bits: string[] = [];
  if (job.started_at) bits.push(`Started ${fmtTime(job.started_at)}`);
  if (job.finished_at) bits.push(`Finished ${fmtTime(job.finished_at)}`);
  if (!bits.length && !job.url) return null;
  return (
    <div className="mt-2 flex items-center gap-1.5 text-xs text-ink-faint">
      {bits.length > 0 && (
        <>
          <span className="min-w-0 font-mono tabular-nums">{bits.join(" · ")}</span>
          <span aria-hidden className="text-line">·</span>
        </>
      )}
      <CopyLinkButton url={job.url} />
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
