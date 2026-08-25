import { useEffect, useState } from "react";
import { api, type AppSettings, type CredentialInfo } from "../api/client";

const PLATFORMS = ["bilibili", "instagram", "tiktok", "douyin", "xhs"] as const;

export default function SettingsPage() {
  const [settings, setSettings] = useState<AppSettings | null>(null);
  const [creds, setCreds] = useState<CredentialInfo[]>([]);
  const [ytdlp, setYtdlp] = useState<string>("…");
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  // cookie paste dialog state
  const [cookiePlatform, setCookiePlatform] = useState<(typeof PLATFORMS)[number] | null>(null);
  const [cookieText, setCookieText] = useState("");

  // notification config
  const [notif, setNotif] = useState<{
    channel_type: string;
    target: string;
    token: string;
    quiet_hours_start: string;
    quiet_hours_end: string;
    configured: boolean;
  }>({
    channel_type: "ntfy",
    target: "",
    token: "",
    quiet_hours_start: "",
    quiet_hours_end: "",
    configured: false,
  });

  async function refresh() {
    setSettings(await api.settings());
    setCreds(await api.credentials());
    api.ytdlpVersion().then((r) => setYtdlp(r.version)).catch(() => setYtdlp("unknown"));
    try {
      const c = await api.notifConfig();
      setNotif((n) => ({
        ...n,
        channel_type: c.channel_type ?? n.channel_type,
        quiet_hours_start: c.quiet_hours_start ?? "",
        quiet_hours_end: c.quiet_hours_end ?? "",
        configured: c.configured,
        target: c.target_masked ?? n.target,
      }));
    } catch {
      /* not configured */
    }
  }

  useEffect(() => {
    refresh().catch(() => {});
  }, []);

  async function save(patch: Partial<AppSettings>) {
    setMessage(null);
    setError(null);
    try {
      const res = await api.saveSettings(patch);
      setSettings(res.settings);
      setMessage(
        res.applied_immediately
          ? "Saved."
          : "Saved. Concurrency cap applies after next restart.",
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "Save failed");
    }
  }

  async function saveCookies() {
    if (!cookiePlatform) return;
    setError(null);
    try {
      await api.saveCredential(cookiePlatform, cookieText);
      setCookiePlatform(null);
      setCookieText("");
      await refresh();
      setMessage(`${cookiePlatform} cookies saved and validated.`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Cookie validation failed");
    }
  }

  async function updateYtdlp() {
    setError(null);
    setMessage(null);
    try {
      const r = await api.ytdlpUpdate();
      setMessage(
        r.updated
          ? `yt-dlp updated to ${r.version} — app is restarting to load it.`
          : `yt-dlp is already up to date (${r.version}).`,
      );
      if (r.updated) setYtdlp(r.version);
    } catch (err) {
      const m = err instanceof Error ? err.message : "Update failed";
      setError(m.includes("deferred_until_idle") ? "Deferred: downloads/recording active." : m);
    }
  }

  return (
    <div className="stagger space-y-6">
      {message && (
        <p
          role="status"
          className="rise-in rounded-xl border border-emerald-500/30 bg-emerald-950/40 px-4 py-2.5 text-sm text-emerald-300"
        >
          {message}
        </p>
      )}
      {error && (
        <p role="alert" className="rise-in rounded-xl border border-bad/30 bg-bad/10 px-4 py-2.5 text-sm text-bad">
          {error}
        </p>
      )}

      {/* Downloads settings */}
      {settings && (
        <section className="card p-5 sm:p-6">
          <h2 className="mb-4 flex items-center gap-2 text-sm font-medium tracking-wide text-ink-dim uppercase">
            <span aria-hidden className="h-4 w-1 rounded-full bg-gradient-to-b from-accent to-accent-2" />
            Downloads
          </h2>
          <div className="space-y-4">
            <Field label="Folder template">
              <input
                defaultValue={settings.folder_template}
                onBlur={(e) =>
                  e.target.value !== settings.folder_template && save({ folder_template: e.target.value })
                }
                className="input"
              />
            </Field>
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
              <Field label={`Concurrency cap (${settings.concurrency_cap})`}>
                <input
                  type="range"
                  min={1}
                  max={8}
                  defaultValue={settings.concurrency_cap}
                  onMouseUp={(e) => save({ concurrency_cap: +(e.target as HTMLInputElement).value })}
                  onTouchEnd={(e) => save({ concurrency_cap: +(e.target as HTMLInputElement).value })}
                  className="w-full accent-cyan-400"
                />
              </Field>
              <Field label="Space floor %">
                <input
                  type="number"
                  min={0}
                  max={50}
                  defaultValue={settings.space_floor_pct}
                  onBlur={(e) => save({ space_floor_pct: +e.target.value })}
                  className="input"
                />
              </Field>
              <Field label="Poll interval (seconds)">
                <input
                  type="number"
                  min={60}
                  defaultValue={settings.poll_interval_seconds}
                  onBlur={(e) => save({ poll_interval_seconds: +e.target.value })}
                  className="input"
                />
              </Field>
            </div>
          </div>
        </section>
      )}

      {/* Platform credentials */}
      <section className="card p-5 sm:p-6">
        <h2 className="mb-1 flex items-center gap-2 text-sm font-medium tracking-wide text-ink-dim uppercase">
          <span aria-hidden className="h-4 w-1 rounded-full bg-gradient-to-b from-accent to-accent-2" />
          Platform cookies (unlock max quality)
        </h2>
        <div className="mt-4 flex flex-wrap gap-2">
          {PLATFORMS.map((p) => {
            const cred = creds.find((c) => c.platform === p);
            return (
              <button
                key={p}
                onClick={() => setCookiePlatform(p)}
                aria-label={`${p} cookies${cred ? " — configured" : " — not configured"}`}
                className={`flex min-h-[36px] cursor-pointer items-center gap-1.5 rounded-full px-3.5 py-1.5 text-xs font-medium capitalize transition-all ${
                  cred
                    ? "border border-emerald-500/40 bg-emerald-950/40 text-emerald-300"
                    : "border border-line bg-surface-2 text-ink-faint hover:border-ink-faint/50 hover:text-ink-dim"
                }`}
              >
                {cred && (
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2.5} strokeLinecap="round" strokeLinejoin="round" aria-hidden className="h-3 w-3">
                    <path d="M20 6 9 17l-5-5" />
                  </svg>
                )}
                {p}
              </button>
            );
          })}
        </div>
        <p className="mt-3 text-xs text-ink-faint">
          Click a platform to paste exported cookies (or a cookies.txt file's contents). Encrypted at rest.
        </p>
      </section>

      {/* Notifications */}
      <section className="card p-5 sm:p-6">
        <h2 className="mb-4 flex items-center gap-2 text-sm font-medium tracking-wide text-ink-dim uppercase">
          <span aria-hidden className="h-4 w-1 rounded-full bg-gradient-to-b from-accent to-accent-2" />
          Notifications
        </h2>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <Field label="Channel">
            <select
              value={notif.channel_type}
              onChange={(e) => setNotif({ ...notif, channel_type: e.target.value })}
              className="input"
            >
              <option value="ntfy">ntfy</option>
              <option value="telegram">Telegram</option>
              <option value="discord">Discord</option>
            </select>
          </Field>
          <Field label={notif.channel_type === "ntfy" ? "Topic URL / name" : "Target (chat id / webhook)"}>
            <input
              value={notif.target}
              onChange={(e) => setNotif({ ...notif, target: e.target.value })}
              placeholder={notif.configured ? "(configured — leave empty to keep)" : ""}
              className="input"
            />
          </Field>
          <Field label="Token (Telegram bot / webhook)">
            <input
              value={notif.token}
              onChange={(e) => setNotif({ ...notif, token: e.target.value })}
              type="password"
              autoComplete="off"
              placeholder={notif.configured ? "(stored)" : ""}
              className="input"
            />
          </Field>
          <Field label="Quiet hours (start–end HH:MM)">
            <div className="flex gap-2">
              <input
                value={notif.quiet_hours_start}
                onChange={(e) => setNotif({ ...notif, quiet_hours_start: e.target.value })}
                placeholder="23:00"
                className="input"
              />
              <input
                value={notif.quiet_hours_end}
                onChange={(e) => setNotif({ ...notif, quiet_hours_end: e.target.value })}
                placeholder="07:00"
                className="input"
              />
            </div>
          </Field>
        </div>
        <div className="mt-4 flex flex-col gap-2 sm:flex-row">
          <button
            onClick={async () => {
              try {
                await api.saveNotifConfig({
                  channel_type: notif.channel_type,
                  ...(notif.target && !notif.configured ? { target: notif.target } : {}),
                  ...(notif.token ? { token: notif.token } : {}),
                  quiet_hours_start: notif.quiet_hours_start || undefined,
                  quiet_hours_end: notif.quiet_hours_end || undefined,
                });
                setMessage("Notification config saved.");
                await refresh();
              } catch (err) {
                setError(err instanceof Error ? err.message : "Save failed");
              }
            }}
            className="btn-primary min-h-[44px] cursor-pointer rounded-xl px-5 py-2.5 text-sm disabled:opacity-40"
          >
            Save notifications
          </button>
          <button
            onClick={async () => {
              try {
                const r = await api.testNotification();
                setMessage(r.delivered ? "Test sent ✓" : "Test suppressed/failed.");
              } catch (err) {
                setError(err instanceof Error ? err.message : "Test failed");
              }
            }}
            className="btn-secondary min-h-[44px] cursor-pointer rounded-xl px-5 py-2.5 text-sm text-ink-dim disabled:opacity-40"
          >
            Send test
          </button>
        </div>
      </section>

      {/* yt-dlp engine */}
      <section className="card flex flex-wrap items-center justify-between gap-3 p-5 sm:p-6">
        <div>
          <h2 className="flex items-center gap-2 text-sm font-medium tracking-wide text-ink-dim uppercase">
            <span aria-hidden className="h-4 w-1 rounded-full bg-gradient-to-b from-accent to-accent-2" />
            Engine
          </h2>
          <p className="mt-1.5 pl-3 font-mono text-xs tabular-nums text-ink-faint">yt-dlp {ytdlp}</p>
        </div>
        <button onClick={updateYtdlp} className="btn-secondary min-h-[44px] cursor-pointer rounded-xl px-5 py-2.5 text-sm text-ink disabled:opacity-40">
          Update now
        </button>
      </section>

      {/* Cookie modal */}
      {cookiePlatform && (
        <div
          role="dialog"
          aria-modal="true"
          aria-label={`${cookiePlatform} cookies`}
          className="rise-in fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4 backdrop-blur-sm sm:p-6"
          onClick={() => setCookiePlatform(null)}
        >
          <div
            className="card rise-in w-full max-w-lg space-y-4 p-5 shadow-2xl shadow-black/50"
            style={{ animationDuration: "0.35s" }}
            onClick={(e) => e.stopPropagation()}
          >
            <h3 className="text-base font-semibold capitalize text-ink">{cookiePlatform} cookies</h3>
            <textarea
              value={cookieText}
              onChange={(e) => setCookieText(e.target.value)}
              rows={10}
              aria-label="Cookie text"
              placeholder="Paste cookie text here (Netscape cookies.txt format or raw header)"
              className="input resize-y font-mono text-xs"
            />
            <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
              <button onClick={() => setCookiePlatform(null)} className="btn-secondary min-h-[44px] cursor-pointer rounded-xl px-5 py-2.5 text-sm text-ink-dim">
                Cancel
              </button>
              <button onClick={saveCookies} disabled={!cookieText.trim()} className="btn-primary min-h-[44px] cursor-pointer rounded-xl px-5 py-2.5 text-sm disabled:cursor-not-allowed disabled:opacity-40">
                Save &amp; validate
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block text-xs text-ink-faint">
      <span className="mb-1.5 block">{label}</span>
      {children}
    </label>
  );
}
