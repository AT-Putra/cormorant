import { useEffect, useState } from "react";
import { api, type ActivityEntry } from "../api/client";

const TYPE_COLORS: Record<string, string> = {
  "job.done": "text-emerald-400",
  "job.failed": "text-bad",
  "job.skipped": "text-ink-faint",
  "recording.started": "text-cyan-300",
  finished: "text-emerald-400",
  interrupted: "text-yellow-300",
  "notification.suppressed": "text-ink-faint/70",
};

export default function ActivityLog() {
  const [entries, setEntries] = useState<ActivityEntry[]>([]);
  const [typeFilter, setTypeFilter] = useState("");

  useEffect(() => {
    api
      .activity({ limit: 200, event_type: typeFilter || undefined })
      .then(setEntries)
      .catch(() => {});
  }, [typeFilter]);

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <input
          value={typeFilter}
          onChange={(e) => setTypeFilter(e.target.value)}
          placeholder="Filter by event type (e.g. job., recording.)"
          aria-label="Filter by event type"
          className="input w-full sm:w-80 sm:text-xs"
        />
        <span className="pill bg-surface-3 text-ink-faint">{entries.length} entries</span>
      </div>
      <div className="card overflow-hidden">
        {entries.length === 0 && (
          <p className="p-6 text-sm text-ink-faint">No activity recorded yet.</p>
        )}
        <ul className="stagger">
          {entries.map((e) => (
            <li
              key={e.id}
              className="flex flex-col gap-1 border-b border-line/60 px-4 py-2.5 transition-colors last:border-0 hover:bg-surface-3/60 sm:flex-row sm:items-baseline sm:gap-3"
            >
              <time
                dateTime={e.ts}
                className="shrink-0 font-mono text-[11px] tabular-nums text-ink-faint"
              >
                {new Date(e.ts.endsWith("Z") ? e.ts : e.ts + "Z").toLocaleString()}
              </time>
              <span
                className={`shrink-0 font-mono text-[11px] ${TYPE_COLORS[e.event_type] ?? "text-ink-dim"}`}
              >
                {e.event_type}
              </span>
              <span className="min-w-0 break-words text-xs text-ink-dim">{e.message}</span>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
