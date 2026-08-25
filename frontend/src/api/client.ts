// Typed REST client + WebSocket hook for the VideoDownloader backend.

export type JobStatus =
  | "queued"
  | "probing"
  | "downloading"
  | "paused"
  | "paused_space_floor"
  | "done"
  | "failed"
  | "skipped";

export interface QualityOption {
  format_id: string;
  ext?: string;
  resolution?: string;
  fps?: number | null;
  vcodec?: string | null;
  acodec?: string | null;
  filesize_approx?: number | null;
  tbr?: number | null;
  format_note?: string | null;
  protocol?: string | null;
}

export interface ProbeResult {
  platform: string;
  title: string;
  duration: number | null;
  formats: QualityOption[];
}

export interface DownloadJob {
  id: number;
  url: string;
  platform: string;
  kind: string;
  title: string | null;
  creator: string | null;
  status: JobStatus;
  progress: number;
  error: string | null;
  is_auto: boolean;
  started_at: string | null;
  finished_at: string | null;
}

export interface CreatorWatch {
  id: number;
  platform: string;
  creator_id: string;
  display_name: string;
  scope: "lives" | "posts" | "both";
  live_url: string | null;
  enabled: boolean;
  last_seen_post_id: string | null;
}

export interface CredentialInfo {
  platform: string;
  validated_at: string | null;
  updated_at: string | null;
}

export interface AppSettings {
  folder_template: string;
  concurrency_cap: number;
  poll_interval_seconds: number;
  space_floor_pct: number;
  default_quality: string;
}

export interface LibraryItem {
  id: number;
  title: string;
  platform: string;
  creator: string;
  media_type: string;
  size_bytes: number | null;
  duration_seconds: number | null;
  has_thumbnail: boolean;
  created_at: string;
}

export interface ActivityEntry {
  id: number;
  ts: string;
  event_type: string;
  message: string;
  ref_type: string | null;
  ref_id: string | null;
}

class ApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    credentials: "same-origin",
    ...init,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail ?? JSON.stringify(body);
    } catch {
      /* not json */
    }
    throw new ApiError(res.status, detail);
  }
  return res.status === 204 ? (undefined as T) : res.json();
}

export const api = {
  // auth
  authStatus: () => request<{ needs_setup: boolean; authenticated: boolean }>("/api/auth/status"),
  setup: (password: string) =>
    request<{ ok: boolean }>("/api/auth/setup", { method: "POST", body: JSON.stringify({ password }) }),
  login: (password: string) =>
    request<{ ok: boolean }>("/api/auth/login", { method: "POST", body: JSON.stringify({ password }) }),
  logout: () => request<{ ok: boolean }>("/api/auth/logout", { method: "POST" }),
  changePassword: (currentPassword: string, newPassword: string) =>
    request<{ ok: boolean }>("/api/auth/change-password", {
      method: "POST",
      body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
    }),

  // downloads
  probe: (url: string) =>
    request<ProbeResult>("/api/downloads/probe", { method: "POST", body: JSON.stringify({ url }) }),
  createJob: (payload: {
    url: string;
    format_id?: string | null;
    kind?: string;
    audio_only?: boolean;
    redownload?: boolean;
  }) => request<DownloadJob>("/api/downloads", { method: "POST", body: JSON.stringify(payload) }),
  jobs: () => request<DownloadJob[]>("/api/downloads"),
  jobAction: (id: number, action: "pause" | "resume" | "cancel" | "retry") =>
    request<DownloadJob>(`/api/downloads/${id}/${action}`, { method: "POST" }),
  deleteJob: (id: number) => request<void>(`/api/downloads/${id}`, { method: "DELETE" }),
  recordLive: (url: string) =>
    request<object>("/api/downloads/record-live", { method: "POST", body: JSON.stringify({ url }) }),

  // watchlist
  watchlist: () => request<CreatorWatch[]>("/api/watchlist"),
  addWatch: (url: string, scope: string, liveUrl?: string) =>
    request<CreatorWatch>("/api/watchlist", {
      method: "POST",
      body: JSON.stringify({ url, scope, live_url: liveUrl?.trim() || null }),
    }),
  updateWatch: (
    id: number,
    patch: { scope?: string; enabled?: boolean; display_name?: string; live_url?: string },
  ) =>
    request<CreatorWatch>(`/api/watchlist/${id}`, { method: "PATCH", body: JSON.stringify(patch) }),
  removeWatch: (id: number) => request<void>(`/api/watchlist/${id}`, { method: "DELETE" }),

  // credentials
  credentials: () => request<CredentialInfo[]>("/api/credentials"),
  saveCredential: (platform: string, cookieText: string) =>
    request<{ validated: boolean }>(`/api/credentials/${platform}`, {
      method: "POST",
      body: JSON.stringify({ cookie_text: cookieText }),
    }),
  removeCredential: (platform: string) =>
    request<void>(`/api/credentials/${platform}`, { method: "DELETE" }),

  // settings
  settings: () => request<AppSettings>("/api/settings"),
  saveSettings: (patch: Partial<AppSettings>) =>
    request<{ saved: boolean; applied_immediately: boolean; settings: AppSettings }>("/api/settings", {
      method: "PUT",
      body: JSON.stringify(patch),
    }),
  ytdlpVersion: () => request<{ version: string }>("/api/settings/ytdlp/version"),
  ytdlpUpdate: () =>
    request<{ updated: boolean; restarting: boolean; version: string }>("/api/settings/ytdlp/update", { method: "POST" }),

  // notifications
  notifConfig: () =>
    request<{
      channel_type: string | null;
      target_masked: string | null;
      quiet_hours_start: string | null;
      quiet_hours_end: string | null;
      configured: boolean;
    }>("/api/notifications/config"),
  saveNotifConfig: (payload: {
    channel_type: string;
    target?: string;
    token?: string;
    quiet_hours_start?: string;
    quiet_hours_end?: string;
  }) => request<object>("/api/notifications/config", { method: "PUT", body: JSON.stringify(payload) }),
  testNotification: () => request<{ delivered: boolean }>("/api/notifications/test", { method: "POST" }),

  // library
  library: (params?: { platform?: string; creator?: string; media_type?: string }) => {
    const qs = new URLSearchParams(
      Object.entries(params ?? {}).filter(([, v]) => v) as [string, string][],
    ).toString();
    return request<LibraryItem[]>(`/api/library${qs ? `?${qs}` : ""}`);
  },
  deleteLibraryItem: (id: number) => request<void>(`/api/library/${id}`, { method: "DELETE" }),

  // activity
  activity: (params?: { limit?: number; offset?: number; event_type?: string }) => {
    const qs = new URLSearchParams(
      Object.entries(params ?? {}).filter(([, v]) => v !== undefined) as [string, string][],
    ).toString();
    return request<ActivityEntry[]>(`/api/activity${qs ? `?${qs}` : ""}`);
  },
};

export function openEventSocket(onEvent: (e: Record<string, unknown>) => void): WebSocket {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${proto}//${location.host}/api/ws`);
  ws.onmessage = (m) => {
    try {
      onEvent(JSON.parse(m.data));
    } catch {
      /* non-json */
    }
  };
  return ws;
}
