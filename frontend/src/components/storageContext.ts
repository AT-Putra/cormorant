import { createContext, useContext } from "react";
import type { StorageStatus } from "../api/client";

// Kept apart from DiskStatus.tsx so that file exports only components
// (Vite fast refresh), while the hook and formatter are shared with Settings.

export interface StorageCtx {
  status: StorageStatus | null;
  refresh: () => void;
}

export const StorageContext = createContext<StorageCtx>({ status: null, refresh: () => {} });

export function useStorage(): StorageCtx {
  return useContext(StorageContext);
}

export function fmtSize(bytes: number): string {
  const gb = bytes / 1e9;
  return gb >= 100 ? `${gb.toFixed(0)} GB` : gb >= 1 ? `${gb.toFixed(1)} GB` : `${(bytes / 1e6).toFixed(0)} MB`;
}
