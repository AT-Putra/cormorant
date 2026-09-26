import { createContext, useContext } from "react";
import type { StorageStatus } from "../api/client";

// Kept apart from DiskStatus.tsx so that file exports only components
// (Vite fast refresh), while the hook and formatters are shared with Settings.

export interface StorageCtx {
  status: StorageStatus | null;
  /** How old `status` is, as of the latest poll attempt. */
  ageMs: number | null;
  /** Polls have been failing long enough that `status` may no longer hold. */
  stale: boolean;
  refresh: () => void;
}

export const StorageContext = createContext<StorageCtx>({
  status: null,
  ageMs: null,
  stale: false,
  refresh: () => {},
});

export function useStorage(): StorageCtx {
  return useContext(StorageContext);
}

export function fmtSize(bytes: number): string {
  const gb = bytes / 1e9;
  return gb >= 100 ? `${gb.toFixed(0)} GB` : gb >= 1 ? `${gb.toFixed(1)} GB` : `${(bytes / 1e6).toFixed(0)} MB`;
}

export function fmtAge(ms: number): string {
  // Down, not to nearest: an age is "at least this old", never older than it is.
  const s = Math.max(0, Math.floor(ms / 1000));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  return m < 60 ? `${m}m` : `${Math.floor(m / 60)}h`;
}
