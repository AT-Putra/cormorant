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
      await api.ytdlpUpdate();
      setMessage("yt-dlp updated — app is restarting to load the new version.");
    } catch (err) {
      const m = err instanceof Error ? err.message : "Update failed";
      setError(m.includes("deferred_until_idle") ? "Deferred: downloads/recording active." : m);
    }
  }

  return (
    <div className="space-y-6">
      {message && (
        <p className="rounded-md border border-emerald-900 bg-emerald-950 px-3 py-2 text-sm text-emerald-300">
          {message}
        </p>
      )}
      {error && <p className="text-sm text-red-400">{error}</p>}

      {/* Downloads settings */}
      {settings && (
        <section className="space-y-4 rounded-xl border border-zinc-800 bg-zinc-900 p-5">
          <h2 className="text-sm font-medium text-zinc-400">Downloads</h2>
          <Field label="Folder template">
            <input
              defaultValue={settings.folder_template}
              onBlur={(e) =>
                e.target.value !== settings.folder_template && save({ folder_template: e.target.value })
              }
              className={input}
            />
          </Field>
          <div className="grid grid-cols-2 gap-4">
            <Field label={`Concurrency cap (${settings.concurrency_cap})`}>
              <input
                type="range"
                min={1}
                max={8}
                defaultValue={settings.concurrency_cap}
                onMouseUp={(e) => save({ concurrency_cap: +(e.target as HTMLInputElement).value })}
                onTouchEnd={(e) => save({ concurrency_cap: +(e.target as HTMLInputElement).value })}
                className="w-full"
              />
            </Field>
            <Field label="Space floor %">
              <input
                type="number"
                min={0}
                max={50}
                defaultValue={settings.space_floor_pct}
                onBlur={(e) => save({ space_floor_pct: +e.target.value })}
                className={input}
              />
            </Field>
            <Field label="Poll interval (seconds)">
              <input
                type="number"
                min={60}
                defaultValue={settings.poll_interval_seconds}
                onBlur={(e) => save({ poll_interval_seconds: +e.target.value })}
                className={input}
              />
            </Field>
          </div>
        </section>
      )}

      {/* Platform credentials */}
      <section className="rounded-xl border border-zinc-800 bg-zinc-900 p-5">
        <h2 className="mb-3 text-sm font-medium text-zinc-400">
          Platform cookies (unlock max quality)
        </h2>
        <div className="flex flex-wrap gap-2">
          {PLATFORMS.map((p) => {
            const cred = creds.find((c) => c.platform === p);
            return (
              <button
                key={p}
                onClick={() => setCookiePlatform(p)}
                className={`rounded-md border px-3 py-1.5 text-xs ${
                  cred
                    ? "border-emerald-800 text-emerald-300"
                    : "border-zinc-700 text-zinc-400 hover:text-white"
                }`}
              >
                {p} {cred ? "✓" : ""}
              </button>
            );
          })}
        </div>
        <p className="mt-2 text-xs text-zinc-600">
          Click a platform to paste exported cookies (or a cookies.txt file's contents). Encrypted at rest.
        </p>
      </section>

      {/* Notifications */}
      <section className="space-y-3 rounded-xl border border-zinc-800 bg-zinc-900 p-5">
        <h2 className="text-sm font-medium text-zinc-400">Notifications</h2>
        <div className="grid grid-cols-2 gap-3">
          <Field label="Channel">
            <select
              value={notif.channel_type}
              onChange={(e) => setNotif({ ...notif, channel_type: e.target.value })}
              className={input}
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
              className={input}
            />
          </Field>
          <Field label="Token (Telegram bot / webhook)">
            <input
              value={notif.token}
              onChange={(e) => setNotif({ ...notif, token: e.target.value })}
              type="password"
              placeholder={notif.configured ? "(stored)" : ""}
              className={input}
            />
          </Field>
          <Field label="Quiet hours (start–end HH:MM)">
            <div className="flex gap-2">
              <input
                value={notif.quiet_hours_start}
                onChange={(e) => setNotif({ ...notif, quiet_hours_start: e.target.value })}
                placeholder="23:00"
                className={input}
              />
              <input
                value={notif.quiet_hours_end}
                onChange={(e) => setNotif({ ...notif, quiet_hours_end: e.target.value })}
                placeholder="07:00"
                className={input}
              />
            </div>
          </Field>
        </div>
        <div className="flex gap-2">
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
            className={btnPrimary}
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
            className={btnSecondary}
          >
            Send test
          </button>
        </div>
      </section>

      {/* yt-dlp engine */}
      <section className="flex items-center justify-between rounded-xl border border-zinc-800 bg-zinc-900 p-5">
        <div>
          <h2 className="text-sm font-medium text-zinc-400">Engine</h2>
          <p className="mt-1 font-mono text-xs text-zinc-500">yt-dlp {ytdlp}</p>
        </div>
        <button onClick={updateYtdlp} className={btnPrimary}>
          Update now
        </button>
      </section>

      {/* Cookie modal */}
      {cookiePlatform && (
        <div className="fixed inset-0 flex items-center justify-center bg-black/70 p-6" onClick={() => setCookiePlatform(null)}>
          <div className="w-full max-w-lg space-y-3 rounded-xl border border-zinc-700 bg-zinc-900 p-5" onClick={(e) => e.stopPropagation()}>
            <h3 className="font-medium capitalize">{cookiePlatform} cookies</h3>
            <textarea
              value={cookieText}
              onChange={(e) => setCookieText(e.target.value)}
              rows={10}
              placeholder="Paste cookie text here (Netscape cookies.txt format or raw header)"
              className="w-full rounded-md border border-zinc-700 bg-zinc-950 px-3 py-2 font-mono text-xs outline-none focus:border-zinc-500"
            />
            <div className="flex justify-end gap-2">
              <button onClick={() => setCookiePlatform(null)} className={btnSecondary}>
                Cancel
              </button>
              <button onClick={saveCookies} disabled={!cookieText.trim()} className={btnPrimary}>
                Save & validate
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

const input =
  "w-full rounded-md border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm outline-none focus:border-zinc-500";
const btnPrimary =
  "rounded-md bg-white px-4 py-2 text-sm font-medium text-zinc-950 hover:bg-zinc-200 disabled:opacity-50";
const btnSecondary =
  "rounded-md border border-zinc-600 px-4 py-2 text-sm text-zinc-300 hover:bg-zinc-800 disabled:opacity-50";

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block text-xs text-zinc-500">
      <span className="mb-1 block">{label}</span>
      {children}
    </label>
  );
}
