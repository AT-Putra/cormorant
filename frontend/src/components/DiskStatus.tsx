import { useCallback, useEffect, useState } from "react";
import { NavLink } from "react-router-dom";
import { api, type StorageStatus } from "../api/client";
import { StorageContext, fmtSize, useStorage } from "./storageContext";

// Free space moves slowly (a capture writes ~1 GB an hour), so a minute-scale
// poll is plenty; the Settings page refreshes on its own after a floor change.
const POLL_MS = 30_000;
// "Low" starts this many percentage points above the floor: early enough to
// free space before recordings are stopped, late enough not to cry wolf.
const LOW_MARGIN_PCT = 5;

// ok -> low (approaching) -> paused (no new lives) -> below (captures stopped)
type Level = "ok" | "low" | "paused" | "below";

function levelOf(s: StorageStatus): Level {
  if (s.below_floor) return "below";
  if (!s.room_to_start) return "paused";
  return s.free_pct < s.start_pct + LOW_MARGIN_PCT ? "low" : "ok";
}

const LEVEL_STYLES: Record<Level, { fill: string; text: string; pill: string; label: string | null }> = {
  ok: { fill: "bg-gradient-to-r from-accent to-accent-2", text: "text-ink-dim", pill: "", label: null },
  low: { fill: "bg-warn", text: "text-warn", pill: "bg-warn/15", label: "Low" },
  // Amber while new lives are skipped, red once running captures are stopped
  // -- the same two colours the banner uses for the same two states.
  paused: { fill: "bg-warn", text: "text-warn", pill: "bg-warn/15", label: "At floor" },
  below: { fill: "bg-bad", text: "text-bad", pill: "bg-bad/15", label: "Below floor" },
};

// Lucide hard-drive / triangle-alert. The glyph changes with the level so the
// state reads without colour -- the text pill is hidden on phones for width.
const DRIVE_ICON =
  "M22 12H2M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11ZM6 16h.01M10 16h.01";
const ALERT_ICON =
  "m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3ZM12 9v4M12 17h.01";

export function StorageProvider({ children }: { children: React.ReactNode }) {
  const [status, setStatus] = useState<StorageStatus | null>(null);

  const refresh = useCallback(() => {
    api
      .storage()
      .then(setStatus)
      // Unknown is not empty: keep showing the last reading rather than
      // flashing the meter away on one failed poll.
      .catch(() => {});
  }, []);

  useEffect(() => {
    refresh();
    const timer = window.setInterval(refresh, POLL_MS);
    const onVisible = () => document.visibilityState === "visible" && refresh();
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [refresh]);

  return <StorageContext.Provider value={{ status, refresh }}>{children}</StorageContext.Provider>;
}

/** Header meter: free space at a glance, links to the floor setting. */
export function DiskMeter({ className }: { className?: string }) {
  const { status } = useStorage();
  if (!status) return null;

  const level = levelOf(status);
  const style = LEVEL_STYLES[level];
  const usedPct = Math.min(100, Math.max(0, 100 - status.free_pct));
  const detail =
    `${fmtSize(status.free_bytes)} free of ${fmtSize(status.total_bytes)} ` +
    `(${status.free_pct.toFixed(1)}%)` +
    (status.floor_pct > 0 ? ` · floor ${status.floor_pct}%` : " · no floor");

  return (
    <NavLink
      to="/settings"
      title={detail}
      aria-label={`Media disk: ${detail}${style.label ? ` — ${style.label}` : ""}. Open settings.`}
      className={`flex min-h-[36px] cursor-pointer items-center gap-2 rounded-full border border-line bg-surface-2 px-2.5 py-1 text-xs transition-colors hover:border-ink-faint/50 hover:bg-surface-3 sm:px-3 ${className ?? ""}`}
    >
      <svg
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth={1.75}
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden
        className={`h-4 w-4 shrink-0 ${style.text}`}
      >
        <path d={level === "ok" ? DRIVE_ICON : ALERT_ICON} />
      </svg>
      <span className={`font-medium tabular-nums ${style.text}`}>
        {fmtSize(status.free_bytes)}
        <span className="max-sm:hidden"> free</span>
      </span>
      <span aria-hidden className="h-1.5 w-14 overflow-hidden rounded-full bg-line max-sm:hidden">
        <span
          className={`block h-full rounded-full transition-[width] duration-500 ${style.fill}`}
          style={{ width: `${usedPct}%` }}
        />
      </span>
      {style.label && (
        <span className={`pill max-sm:hidden ${style.pill} ${style.text}`}>{style.label}</span>
      )}
    </NavLink>
  );
}

/** Shown on every page while new recordings can't start -- the only other
 *  trace of a skipped live is a line in the activity log. */
export function DiskBanner() {
  const { status } = useStorage();
  if (!status || (!status.below_floor && status.room_to_start)) return null;

  const free = `${fmtSize(status.free_bytes)} free (${status.free_pct.toFixed(1)}%)`;
  const below = status.below_floor;

  return (
    <div
      role="status"
      aria-atomic="true"
      className={`border-b ${below ? "border-bad/30 bg-bad/10" : "border-warn/30 bg-warn/10"}`}
    >
      <div className="mx-auto flex w-full max-w-6xl flex-wrap items-center gap-x-4 gap-y-1 px-4 py-2.5 text-sm sm:px-6">
        <p className={below ? "text-bad" : "text-warn"}>
          <span className="font-semibold">
            {below ? `Disk below the ${status.floor_pct}% floor` : `Disk near the ${status.floor_pct}% floor`}
          </span>
          {" — "}
          {free}.{" "}
          <span className="text-ink-dim">
            {below
              ? "Running recordings are stopped and saved; new lives and auto-downloads won't start."
              : `New lives won't be recorded until ${status.start_pct}% is free.`}
          </span>
        </p>
        <span className="flex gap-3 text-xs font-medium">
          <NavLink to="/library" className="text-ink underline decoration-ink-faint underline-offset-4 hover:decoration-ink">
            Free up space
          </NavLink>
          <NavLink to="/settings" className="text-ink underline decoration-ink-faint underline-offset-4 hover:decoration-ink">
            Change floor
          </NavLink>
        </span>
      </div>
    </div>
  );
}
