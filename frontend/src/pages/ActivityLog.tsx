import { useEffect, useState } from "react";
import { api, type ActivityEntry } from "../api/client";

const TYPE_COLORS: Record<string, string> = {
  "job.done": "text-emerald-400",
  "job.failed": "text-red-400",
  "job.skipped": "text-zinc-500",
  "recording.started": "text-blue-400",
  finished: "text-emerald-400",
  interrupted: "text-yellow-400",
  "notification.suppressed": "text-zinc-600",
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
      <div className="flex items-center gap-2">
        <input
          value={typeFilter}
          onChange={(e) => setTypeFilter(e.target.value)}
          placeholder="Filter by event type (e.g. job., recording.)"
          className="w-80 rounded-md border border-zinc-700 bg-zinc-950 px-3 py-1.5 text-xs outline-none focus:border-zinc-500"
        />
        <span className="text-xs text-zinc-600">{entries.length} entries</span>
      </div>
      <div className="overflow-hidden rounded-lg border border-zinc-800">
        {entries.length === 0 && (
          <p className="p-4 text-sm text-zinc-600">No activity recorded yet.</p>
        )}
        {entries.map((e) => (
          <div
            key={e.id}
            className="flex items-baseline gap-3 border-b border-zinc-900 px-4 py-2 last:border-0"
          >
            <span className="shrink-0 font-mono text-[11px] text-zinc-600">
              {new Date(e.ts.endsWith("Z") ? e.ts : e.ts + "Z").toLocaleString()}
            </span>
            <span
              className={`shrink-0 font-mono text-[11px] ${TYPE_COLORS[e.event_type] ?? "text-zinc-400"}`}
            >
              {e.event_type}
            </span>
            <span className="truncate text-xs text-zinc-300">{e.message}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
