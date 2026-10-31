// News Digest Autopilot - Cloudflare Worker control plane.
// Serves the dashboard, starts a GitHub Actions collection run before each delivery time, stores
// the finished digests and emails them (Composio Gmail) at the times the user picked.
import DASHBOARD_HTML from "./dashboard.html";
import LOGIN_HTML from "./login.html";
import * as render from "./render.js";

const DEFAULTS = {
  enabled: false,
  timezone: "Asia/Karachi",
  slots: ["08:00", "20:00"],
  lead_minutes: 30,
  lookback_mode: "since_last",
  lookback_hours: 24,
  language: "English",
  item_style: "paragraph",
  words_per_story: 70,
  show_time: true,
  show_sources: true,
  show_emoji: true,
  story_order: "sources",
  other_topic_name: "Other News",
  other_news_mode: "list",
  other_news_limit: 20,
  show_empty_topics: true,
  digest_title: "News Digest",
  writing_prompt:
    "Write like a wire-service editor: neutral, factual and plain. Start with the most important fact. Use active voice. " +
    "Write numbers as digits. Write Pakistani currency as PKR (for example PKR 285,000). Attribute claims to the outlet " +
    "or person who made them (for example \"according to Dawn\"). Do not use emojis, hashtags or exclamation marks.",
  sort_prompt: "",
  max_posts_per_source: 60,
  skip_retweets: true,
  include_replies: false,
  fetch_full_articles: false,
  transcribe_videos: true,
  whisper_model: "small",
  max_video_minutes_each: 15,
  max_video_minutes_per_run: 90,
  stories_per_call: 4,
  research_edition: "PK:en",
  email_enabled: true,
  email_recipients: [],
  email_subject: "{title} | {date}, {time}",
  email_when_empty: false,
  // Fastest good models first; slow or exhausted ones are skipped for the rest of a run.
  text_models: [
    "gemini-3.6-flash", "gemini-3.5-flash", "gemini-3-flash-preview", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite",
    "gemini-flash-lite-latest", "gemini-3.7-flash", "gemini-3.8-flash", "gemini-flash-latest",
    "gemma-4-31b-it", "gemma-4-26b-a4b-it",
  ],
  // Internal (not editable through /api/settings)
  public_url: "",
  last_cleanup_date: null,
  x_status: null,
  panel_password_hash: null,
  x_auth_token: null,
  x_ct0: null,
  fb_app_secret: null,
  fb_token: null,
  fb_user_token: null,
  fb_user_expires: null,
  fb_ig_id: null,
  fb_ig_username: null,
  fb_page_name: null,
};
const INTERNAL_KEYS = new Set(["public_url", "last_cleanup_date", "x_status", "panel_password_hash", "x_auth_token", "x_ct0",
  "fb_app_secret", "fb_token", "fb_user_token", "fb_user_expires", "fb_ig_id", "fb_ig_username", "fb_page_name"]);
// Never sent to the browser or the pipeline's general settings.
const SECRET_KEYS = ["panel_password_hash", "x_auth_token", "x_ct0", "fb_app_secret", "fb_token", "fb_user_token"];
const publicSettings = (s) => Object.fromEntries(Object.entries(s).filter(([k]) => !SECRET_KEYS.includes(k)));
const TEXT_MODELS = { large: "@cf/meta/llama-3.3-70b-instruct-fp8-fast", small: "@cf/meta/llama-3.1-8b-instruct-fast" };
const SESSION_COOKIE = "nd_session";
const SESSION_DAYS = 30;
const COMPOSIO_BASE = "https://backend.composio.dev";

export default {
  async fetch(request, env, ctx) {
    try {
      return await route(request, env, ctx);
    } catch (err) {
      return json({ error: err.message || String(err) }, err.status || 500);
    }
  },
  async scheduled(_event, env, ctx) {
    ctx.waitUntil(tick(env));
  },
};

// ---------- helpers ----------

class HttpError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

function json(data, status = 200, headers = {}) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store", ...headers },
  });
}

function html(body, status = 200) {
  return new Response(body, {
    status,
    headers: {
      "content-type": "text/html; charset=utf-8",
      "cache-control": "no-store",
      "x-frame-options": "DENY",
      "referrer-policy": "same-origin",
    },
  });
}

const nowIso = () => new Date().toISOString();
const isoAt = (ms) => new Date(ms).toISOString();
const randomId = (bytes = 6) =>
  [...crypto.getRandomValues(new Uint8Array(bytes))].map((b) => b.toString(16).padStart(2, "0")).join("");

async function body(request) {
  try {
    return await request.json();
  } catch {
    throw new HttpError(400, "invalid JSON body");
  }
}

function safeEqual(a, b) {
  const enc = new TextEncoder();
  const x = enc.encode(String(a));
  const y = enc.encode(String(b));
  if (x.length !== y.length) return false;
  return crypto.subtle.timingSafeEqual(x, y);
}

async function hmac(secret, message) {
  const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const sig = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(message));
  return [...new Uint8Array(sig)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

// The login code can be changed in the dashboard; it is stored as a salted PBKDF2 hash. Until then
// the DASHBOARD_PASSWORD secret is used. Changing it signs out every other browser.
const PBKDF2_ROUNDS = 5000;
const hex = (buf) => [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");

async function pbkdf2(password, saltHex, rounds) {
  const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(password), "PBKDF2", false, ["deriveBits"]);
  const salt = Uint8Array.from(saltHex.match(/../g).map((h) => parseInt(h, 16)));
  return hex(await crypto.subtle.deriveBits({ name: "PBKDF2", hash: "SHA-256", salt, iterations: rounds }, key, 256));
}

async function hashPassword(password) {
  const salt = hex(crypto.getRandomValues(new Uint8Array(16)));
  return `pbkdf2$${PBKDF2_ROUNDS}$${salt}$${await pbkdf2(password, salt, PBKDF2_ROUNDS)}`;
}

async function checkPassword(env, s, password) {
  if (s.panel_password_hash) {
    const [, rounds, salt, want] = s.panel_password_hash.split("$");
    return safeEqual(await pbkdf2(String(password || ""), salt, Number(rounds)), want);
  }
  return Boolean(env.DASHBOARD_PASSWORD) && safeEqual(password || "", env.DASHBOARD_PASSWORD);
}

const sessionSecret = (env, s) => `${env.PIPELINE_SECRET}|${s.panel_password_hash || env.DASHBOARD_PASSWORD}`;

async function makeSession(env, s) {
  const exp = Date.now() + SESSION_DAYS * 86400000;
  return `${exp}.${await hmac(sessionSecret(env, s), `session:${exp}`)}`;
}

const sessionCookie = (value) => `${SESSION_COOKIE}=${value}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=${SESSION_DAYS * 86400}`;

async function hasSession(request, env) {
  const cookie = request.headers.get("cookie") || "";
  const match = cookie.match(new RegExp(`(?:^|; )${SESSION_COOKIE}=([^;]+)`));
  if (!match) return false;
  const [exp, sig] = match[1].split(".");
  if (!exp || !sig || Number(exp) < Date.now()) return false;
  return safeEqual(sig, await hmac(sessionSecret(env, await getSettings(env)), `session:${exp}`));
}

function requirePipeline(request, env) {
  const auth = request.headers.get("authorization") || "";
  if (!env.PIPELINE_SECRET || !safeEqual(auth, `Bearer ${env.PIPELINE_SECRET}`)) throw new HttpError(401, "unauthorized");
}

// Digest pages opened from emails carry a signed token instead of a login.
const digestToken = (env, id) => hmac(env.PIPELINE_SECRET, `digest:${id}`);

// ---------- settings ----------

async function getSettings(env) {
  const { results } = await env.DB.prepare("SELECT key, value FROM settings").all();
  const s = { ...DEFAULTS };
  for (const row of results) {
    try {
      s[row.key] = JSON.parse(row.value);
    } catch {
      /* keep default */
    }
  }
  return s;
}

async function saveSettings(env, patch) {
  const stmts = Object.entries(patch).map(([k, v]) =>
    env.DB.prepare("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value").bind(k, JSON.stringify(v)),
  );
  if (stmts.length) await env.DB.batch(stmts);
}

const TIME_RE = /^([01]\d|2[0-3]):[0-5]\d$/;

function validTimezone(tz) {
  try {
    new Intl.DateTimeFormat("en", { timeZone: tz });
    return true;
  } catch {
    return false;
  }
}

function cleanSettingsPatch(patch) {
  const out = {};
  for (const [k, v] of Object.entries(patch || {})) {
    if (!(k in DEFAULTS) || INTERNAL_KEYS.has(k)) continue;
    const def = DEFAULTS[k];
    if (typeof def === "number") {
      const n = Number(v);
      if (!Number.isFinite(n)) throw new HttpError(400, `${k} must be a number`);
      out[k] = n;
    } else if (typeof def === "boolean") out[k] = Boolean(v);
    else if (Array.isArray(def)) {
      out[k] = (Array.isArray(v) ? v.map(String) : String(v).split(/[\s,;]+/)).map((x) => x.trim()).filter(Boolean);
    } else out[k] = String(v ?? "");
  }
  if ("slots" in out) {
    const bad = out.slots.find((t) => !TIME_RE.test(t));
    if (bad) throw new HttpError(400, `Delivery time "${bad}" must look like 08:30 (24-hour clock)`);
    out.slots = [...new Set(out.slots)].sort();
    if (out.slots.length > 24) throw new HttpError(400, "At most 24 deliveries per day");
  }
  if ("timezone" in out && !validTimezone(out.timezone)) throw new HttpError(400, "Unknown time zone (use a name like Asia/Karachi)");
  if ("lead_minutes" in out) out.lead_minutes = Math.max(10, Math.min(120, Math.round(out.lead_minutes)));
  if ("lookback_hours" in out) out.lookback_hours = Math.max(1, Math.min(168, out.lookback_hours));
  if ("lookback_mode" in out && !["since_last", "fixed"].includes(out.lookback_mode)) throw new HttpError(400, "bad lookback_mode");
  if ("item_style" in out && !["paragraph", "bullets", "oneline"].includes(out.item_style)) throw new HttpError(400, "bad item_style");
  if ("story_order" in out && !["sources", "newest"].includes(out.story_order)) throw new HttpError(400, "bad story_order");
  if ("whisper_model" in out && !["tiny", "base", "small", "medium"].includes(out.whisper_model)) throw new HttpError(400, "bad whisper_model");
  if ("words_per_story" in out) out.words_per_story = Math.max(20, Math.min(300, Math.round(out.words_per_story)));
  if ("max_posts_per_source" in out) out.max_posts_per_source = Math.max(5, Math.min(200, Math.round(out.max_posts_per_source)));
  if ("max_video_minutes_each" in out) out.max_video_minutes_each = Math.max(1, Math.min(60, out.max_video_minutes_each));
  if ("max_video_minutes_per_run" in out) out.max_video_minutes_per_run = Math.max(0, Math.min(300, out.max_video_minutes_per_run));
  if ("research_edition" in out && !/^[A-Z]{2}:[a-z]{2}$/.test(out.research_edition)) throw new HttpError(400, "Search edition must look like PK:en");
  if ("stories_per_call" in out) out.stories_per_call = Math.max(1, Math.min(6, Math.round(out.stories_per_call)));
  if ("other_news_mode" in out && !["list", "full", "drop"].includes(out.other_news_mode)) throw new HttpError(400, "bad other_news_mode");
  if ("other_news_limit" in out) out.other_news_limit = Math.max(1, Math.min(100, Math.round(out.other_news_limit)));
  if ("email_recipients" in out) {
    const bad = out.email_recipients.find((e) => !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(e));
    if (bad) throw new HttpError(400, `"${bad}" is not an email address`);
  }
  if ("writing_prompt" in out && out.writing_prompt.length > 6000) throw new HttpError(400, "Writing prompt is too long (6000 characters max)");
  if ("sort_prompt" in out && out.sort_prompt.length > 4000) throw new HttpError(400, "Sorting prompt is too long (4000 characters max)");
  return out;
}

// ---------- time helpers (IANA time zone) ----------

function zonedParts(tz, ms) {
  const f = new Intl.DateTimeFormat("en-CA", {
    timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
  });
  const p = Object.fromEntries(f.formatToParts(new Date(ms)).map((x) => [x.type, x.value]));
  return { date: `${p.year}-${p.month}-${p.day}`, time: `${p.hour}:${p.minute}`, y: +p.year, m: +p.month, d: +p.day, h: +p.hour, mi: +p.minute };
}

function tzOffsetMs(tz, ms) {
  const p = zonedParts(tz, ms);
  return Date.UTC(p.y, p.m - 1, p.d, p.h, p.mi) - (ms - (ms % 60000));
}

// UTC milliseconds of a local date ("2026-10-25") and time ("08:00") in the time zone.
function zonedToUtc(tz, date, hhmm) {
  const [y, m, d] = date.split("-").map(Number);
  const [h, mi] = hhmm.split(":").map(Number);
  const guess = Date.UTC(y, m - 1, d, h, mi);
  let t = guess - tzOffsetMs(tz, guess);
  const second = guess - tzOffsetMs(tz, t);
  if (second !== t) t = second;
  return t;
}

function upcomingSlots(s, count = 3) {
  if (!s.enabled || !s.slots?.length) return [];
  const out = [];
  const now = Date.now();
  for (let day = 0; day < 3 && out.length < count; day++) {
    const date = zonedParts(s.timezone, now + day * 86400000).date;
    for (const slot of s.slots) {
      const at = zonedToUtc(s.timezone, date, slot);
      if (at > now) out.push(isoAt(at));
    }
  }
  return out.sort().slice(0, count);
}

// ---------- GitHub dispatch ----------

async function dispatch(env, { mode, digestId, trigger }) {
  if (!env.GH_TOKEN || !env.GH_REPO) throw new HttpError(500, "GitHub is not configured on the Worker");
  const runId = randomId();
  await env.DB.prepare("INSERT INTO runs (id, digest_id, started_at, mode, trigger, status) VALUES (?, ?, ?, ?, ?, 'dispatched')")
    .bind(runId, digestId || null, nowIso(), mode, trigger)
    .run();
  const res = await fetch(`https://api.github.com/repos/${env.GH_REPO}/actions/workflows/${env.GH_WORKFLOW || "digest.yml"}/dispatches`, {
    method: "POST",
    headers: {
      authorization: `Bearer ${env.GH_TOKEN}`,
      accept: "application/vnd.github+json",
      "x-github-api-version": "2022-11-28",
      "user-agent": "news-digest-autopilot",
      "content-type": "application/json",
    },
    body: JSON.stringify({ ref: env.GH_REF || "main", inputs: { mode, run_id: runId, digest_id: digestId || "", trigger } }),
  });
  if (res.status !== 204) {
    const text = (await res.text()).slice(0, 300);
    await env.DB.prepare("UPDATE runs SET status = 'error', finished_at = ?, summary = ? WHERE id = ?")
      .bind(nowIso(), `GitHub dispatch failed (${res.status}): ${text}`, runId)
      .run();
    throw new HttpError(502, `GitHub dispatch failed (${res.status}): ${text}`);
  }
  return runId;
}

// ---------- digests ----------

// Window of posts a new digest covers: from the end of the previous real digest (no gaps, no repeats)
// or the last X hours.
async function windowFor(env, s, kind, now) {
  const fixed = isoAt(now - s.lookback_hours * 3600000);
  if (s.lookback_mode !== "since_last") return fixed;
  const prev = await env.DB.prepare(
    "SELECT window_end FROM digests WHERE kind != 'preview' AND status IN ('collecting','ready','sent','email_failed','skipped') ORDER BY window_end DESC LIMIT 1",
  ).first();
  if (!prev || now - Date.parse(prev.window_end) > 72 * 3600000) return fixed;
  // A preview looks at the same window the next real digest would.
  return prev.window_end < isoAt(now) ? prev.window_end : fixed;
}

async function startDigest(env, s, { id, kind, trigger, slotAt, slotDate = null, slotTime = null }) {
  const now = Date.now();
  const windowStart = await windowFor(env, s, kind, now);
  const sendEmail = kind !== "preview" && s.email_enabled ? 1 : 0;
  const ins = await env.DB.prepare(
    `INSERT OR IGNORE INTO digests (id, kind, trigger, status, slot_date, slot_time, slot_at, window_start, window_end, send_email, created_at)
     VALUES (?, ?, ?, 'collecting', ?, ?, ?, ?, ?, ?, ?)`,
  ).bind(id, kind, trigger, slotDate, slotTime, isoAt(slotAt), windowStart, isoAt(now), sendEmail, isoAt(now)).run();
  if (!ins.meta?.changes) return null;
  try {
    const runId = await dispatch(env, { mode: kind === "preview" ? "preview" : "live", digestId: id, trigger });
    await env.DB.prepare("UPDATE digests SET run_id = ? WHERE id = ?").bind(runId, id).run();
    return { id, run_id: runId };
  } catch (err) {
    await env.DB.prepare("UPDATE digests SET status = 'failed', error = ? WHERE id = ?").bind(err.message, id).run();
    throw err;
  }
}

async function scheduleSlots(env, s) {
  const now = Date.now();
  const dates = [...new Set([zonedParts(s.timezone, now).date, zonedParts(s.timezone, now + 86400000).date])];
  for (const date of dates) {
    for (const slot of s.slots || []) {
      const at = zonedToUtc(s.timezone, date, slot);
      // Start collecting lead_minutes early; a slot missed by more than 3 hours is skipped.
      if (now < at - s.lead_minutes * 60000 || now > at + 3 * 3600000) continue;
      const id = `${date}_${slot.replace(":", "")}`;
      try {
        await startDigest(env, s, { id, kind: "scheduled", trigger: "schedule", slotAt: at, slotDate: date, slotTime: slot });
      } catch (err) {
        console.error("scheduled dispatch failed", err.message);
      }
    }
  }
}

function parseData(d) {
  try {
    return d.data ? JSON.parse(d.data) : null;
  } catch {
    return null;
  }
}

async function deliver(env, s, d) {
  const data = parseData(d);
  const stories = d.story_count || 0;
  if (!stories && !s.email_when_empty) {
    await env.DB.prepare("UPDATE digests SET status = 'skipped', sent_at = ?, error = 'No new posts in this window, so no email was sent' WHERE id = ?")
      .bind(nowIso(), d.id).run();
    return { ok: true, skipped: true };
  }
  try {
    const link = `${s.public_url}/d/${d.id}?t=${await digestToken(env, d.id)}`;
    const parts = render.emailParts(d, data, s, link);
    for (const part of parts) await sendEmail(env, s, part.subject, part.html);
    await addUsage(env, "emails", parts.length);
    await env.DB.prepare("UPDATE digests SET status = 'sent', sent_at = ?, error = NULL WHERE id = ?").bind(nowIso(), d.id).run();
    return { ok: true, parts: parts.length };
  } catch (err) {
    await env.DB.prepare("UPDATE digests SET status = 'email_failed', error = ? WHERE id = ?").bind(String(err.message).slice(0, 500), d.id).run();
    return { ok: false, error: err.message };
  }
}

async function deliverDue(env, s) {
  const { results } = await env.DB.prepare(
    "SELECT * FROM digests WHERE status = 'ready' AND send_email = 1 AND slot_at <= ? ORDER BY slot_at LIMIT 3",
  ).bind(nowIso()).all();
  for (const d of results) await deliver(env, s, d);
}

// ---------- email (Composio Gmail) ----------

async function sendEmail(env, s, subject, htmlBody) {
  const to = s.email_recipients || [];
  if (!to.length) throw new Error("No email recipient set (Delivery tab)");
  if (!env.COMPOSIO_API_KEY || !env.COMPOSIO_ACCOUNT_ID) throw new Error("Composio is not configured on the Worker");
  const res = await fetch(`${COMPOSIO_BASE}/api/v3.1/tools/execute/GMAIL_SEND_EMAIL`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "x-user-api-key": env.COMPOSIO_API_KEY,
      ...(env.COMPOSIO_ORG_ID ? { "x-org-id": env.COMPOSIO_ORG_ID } : {}),
      ...(env.COMPOSIO_PROJECT_ID ? { "x-project-id": env.COMPOSIO_PROJECT_ID } : {}),
    },
    body: JSON.stringify({
      connected_account_id: env.COMPOSIO_ACCOUNT_ID,
      user_id: env.COMPOSIO_USER_ID,
      arguments: { recipient_email: to[0], extra_recipients: to.slice(1), subject, body: htmlBody, is_html: true },
    }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok || data.successful === false) {
    const why = data.error?.message || data.error || data.message || `HTTP ${res.status}`;
    throw new Error(`Gmail via Composio: ${String(typeof why === "string" ? why : JSON.stringify(why)).slice(0, 300)}`);
  }
  return data;
}

// ---------- usage counters ----------

const utcDay = (ms = Date.now()) => new Date(ms).toISOString().slice(0, 10);

async function addUsage(env, key, n = 1) {
  await env.DB.prepare("INSERT INTO usage (day, key, n) VALUES (?, ?, ?) ON CONFLICT(day, key) DO UPDATE SET n = n + excluded.n")
    .bind(utcDay(), key, n).run();
}

// ---------- scheduler ----------

async function tick(env) {
  const s = await getSettings(env);
  const now = Date.now();
  await env.DB.batch([
    env.DB.prepare("UPDATE runs SET status = 'timeout', finished_at = ? WHERE status IN ('dispatched','running') AND started_at < ?")
      .bind(nowIso(), isoAt(now - 75 * 60000)),
    env.DB.prepare("UPDATE digests SET status = 'failed', error = COALESCE(error, 'The collection run did not finish in time') WHERE status = 'collecting' AND created_at < ?")
      .bind(isoAt(now - 80 * 60000)),
  ]);
  await dailyCleanup(env, s);
  if (s.enabled) await scheduleSlots(env, s);
  await deliverDue(env, s);
}

async function dailyCleanup(env, s) {
  const today = zonedParts(s.timezone, Date.now()).date;
  if (s.last_cleanup_date === today) return;
  const days = (n) => isoAt(Date.now() - n * 86400000);
  await env.DB.batch([
    env.DB.prepare("DELETE FROM seen WHERE seen_at < ?").bind(days(10)),
    env.DB.prepare("DELETE FROM runs WHERE started_at < ?").bind(days(30)),
    env.DB.prepare("DELETE FROM digests WHERE created_at < ?").bind(days(60)),
    env.DB.prepare("DELETE FROM usage WHERE day < ?").bind(days(40).slice(0, 10)),
  ]);
  await saveSettings(env, { last_cleanup_date: today });
}

// ---------- sources ----------

const SOURCE_UA = "Mozilla/5.0 (compatible; NewsDigestAutopilot/1.0)";
const firstPathPart = (u) => u.pathname.split("/").filter(Boolean)[0] || "";

// Turn a pasted link into a source: X and Instagram profiles by handle, Facebook pages need a FetchRSS
// feed, websites via their advertised RSS feed, RSS/Atom links as they are.
async function resolveSource(raw, kindHint) {
  let input = String(raw || "").trim();
  if (!input) throw new HttpError(400, "Paste a profile link, feed link or website");
  if (kindHint === "x" && /^@?\w{1,15}$/.test(input)) input = `https://x.com/${input.replace("@", "")}`;
  if (kindHint === "instagram" && /^@?[\w.]{1,30}$/.test(input)) input = `https://www.instagram.com/${input.replace("@", "")}/`;
  if (!/^https?:\/\//i.test(input)) input = `https://${input}`;
  let u;
  try {
    u = new URL(input);
  } catch {
    throw new HttpError(400, "That link doesn't look valid");
  }
  const host = u.hostname.replace(/^(www|m|mobile|mbasic)\./, "");
  if (host === "x.com" || host === "twitter.com") {
    const handle = firstPathPart(u).replace("@", "");
    if (!/^\w{1,15}$/.test(handle) || ["home", "i", "search", "explore", "hashtag"].includes(handle)) {
      throw new HttpError(400, "Use an X profile link, like https://x.com/dawn_com");
    }
    return { kind: "x", url: `https://x.com/${handle}`, handle, name: `@${handle}` };
  }
  if (host === "instagram.com") {
    const handle = firstPathPart(u);
    if (!/^[\w.]{1,30}$/.test(handle) || ["p", "reel", "reels", "stories", "explore", "tv"].includes(handle)) {
      throw new HttpError(400, "Use an Instagram profile link, like https://www.instagram.com/dawn.today/");
    }
    return { kind: "instagram", url: `https://www.instagram.com/${handle}/`, handle, name: `@${handle}` };
  }
  if (host === "facebook.com" || host === "fb.com" || host === "fb.me") {
    throw new HttpError(400, "Facebook pages can't be read directly for free. Open fetchrss.com, paste this page link there, " +
      "copy the RSS link it gives you and add that here as a Facebook (FetchRSS) source.");
  }
  let res;
  try {
    res = await fetch(u.toString(), { headers: { "user-agent": SOURCE_UA, accept: "application/rss+xml, application/atom+xml, text/html;q=0.9, */*;q=0.5" }, redirect: "follow" });
  } catch {
    throw new HttpError(400, "Couldn't open that link. Check the address and try again.");
  }
  const kind = kindHint === "facebook" || host.endsWith("fetchrss.com") ? "facebook" : "rss";
  const type = res.headers.get("content-type") || "";
  const text = await res.text();
  if (/<(rss|feed|rdf:RDF)\b/i.test(text.slice(0, 4000)) || /xml|rss|atom/i.test(type)) {
    const title = (text.slice(0, 6000).match(/<title[^>]*>(?:<!\[CDATA\[)?([^<\]]+)/i) || [])[1];
    return { kind, url: res.url, handle: null, name: (title || host).trim().slice(0, 80) };
  }
  if (/html/i.test(type)) {
    const link = text.match(/<link[^>]+rel=["']alternate["'][^>]*>/gi)?.find((l) => /rss|atom/i.test(l));
    const href = link?.match(/href=["']([^"']+)["']/i)?.[1];
    const title = (text.match(/<title[^>]*>([^<]{1,200})/i) || [])[1];
    if (href) return { kind, url: new URL(href.replace(/&amp;/g, "&"), res.url).toString(), handle: null, name: (title || host).trim().slice(0, 80) };
  }
  throw new HttpError(400, "No news feed found on that link. Paste the site's RSS link (often /feed or /rss).");
}

async function testSource(env, s, src) {
  if (src.kind === "x") {
    return { ok: Boolean(s.x_auth_token && s.x_ct0), message: s.x_auth_token ? "X profiles are checked during each collection run (needs the X cookies in Connections)." : "Add the burner X account cookies in Connections first." };
  }
  if (src.kind === "instagram") {
    const media = await instagramMedia(s, src.handle, { limit: 5 });
    return { ok: true, message: `Found ${media.media.length} recent posts on @${src.handle}` };
  }
  const res = await fetch(src.url, { headers: { "user-agent": SOURCE_UA } });
  const text = await res.text();
  const n = (text.match(/<(item|entry)[\s>]/gi) || []).length;
  if (!res.ok || !n) return { ok: false, message: `Feed answered HTTP ${res.status} with ${n} entries` };
  return { ok: true, message: `Feed works: ${n} entries` };
}

// ---------- Instagram reading (Meta Business Discovery) ----------

async function instagramMedia(s, username, { limit = 25, after = "" } = {}) {
  if (!s.fb_token || !s.fb_ig_id) throw new HttpError(400, "Instagram reading is not connected (Connections tab)");
  const name = String(username || "").replace(/[^\w.]/g, "");
  const media = `media${after ? `.after(${after.replace(/[^\w=-]/g, "")})` : ""}.limit(${Math.max(1, Math.min(50, limit))})`;
  const fields = `business_discovery.username(${name}){username,name,${media}{id,caption,permalink,timestamp,media_type,media_product_type,media_url,thumbnail_url,children{media_type,media_url}}}`;
  let data = {};
  // Page token first (never expires), then the user token.
  for (const token of [s.fb_token, s.fb_user_token].filter(Boolean)) {
    const res = await fetch(`https://graph.facebook.com/${s.fb_ig_id}?fields=${encodeURIComponent(fields)}&access_token=${encodeURIComponent(token)}`);
    data = await res.json();
    if (data.business_discovery) break;
  }
  if (!data.business_discovery) {
    throw new HttpError(400, `Instagram @${name}: ${data.error?.message || "account not found"} (only public Business/Creator accounts can be read)`);
  }
  const m = data.business_discovery.media || {};
  return { media: m.data || [], after: m.paging?.cursors?.after || null, name: data.business_discovery.name || "" };
}

// ---------- Workers AI (last-resort text fallback) ----------

async function aiText(env, { system, user, size = "large" }) {
  const model = TEXT_MODELS[size] || TEXT_MODELS.large;
  const input = {
    messages: [
      { role: "system", content: system },
      { role: "user", content: user },
    ],
    max_tokens: size === "small" ? 600 : 4000,
    temperature: 0.2,
  };
  let res;
  try {
    res = await env.AI.run(model, { ...input, response_format: { type: "json_object" } });
  } catch {
    res = await env.AI.run(model, input);
  }
  const text = typeof res.response === "string" ? res.response : JSON.stringify(res.response);
  await addUsage(env, "workers_ai_calls", 1);
  return { text, model };
}

// ---------- panel state ----------

function digestSummary(d) {
  const { data, ...rest } = d;
  return rest;
}

async function panelState(env) {
  const s = await getSettings(env);
  const [sources, research, topics, digests, runs, usage] = await Promise.all([
    env.DB.prepare("SELECT * FROM sources ORDER BY kind, id").all(),
    env.DB.prepare("SELECT * FROM research ORDER BY id").all(),
    env.DB.prepare("SELECT * FROM topics ORDER BY position, id").all(),
    env.DB.prepare("SELECT id, kind, trigger, status, slot_date, slot_time, slot_at, window_start, window_end, send_email, created_at, ready_at, sent_at, run_id, item_count, story_count, error FROM digests ORDER BY created_at DESC LIMIT 60").all(),
    env.DB.prepare("SELECT id, digest_id, started_at, finished_at, mode, trigger, status, summary, gh_run_url FROM runs ORDER BY started_at DESC LIMIT 30").all(),
    env.DB.prepare("SELECT key, n FROM usage WHERE day = ?").bind(utcDay()).all(),
  ]);
  return {
    settings: publicSettings(s),
    sources: sources.results,
    research: research.results,
    topics: topics.results,
    digests: digests.results.map(digestSummary),
    runs: runs.results,
    usage: Object.fromEntries(usage.results.map((r) => [r.key, r.n])),
    next_slots: upcomingSlots(s),
    now: nowIso(),
    config: {
      github: Boolean(env.GH_TOKEN && env.GH_REPO),
      repo: env.GH_REPO || "",
      composio: Boolean(env.COMPOSIO_API_KEY && env.COMPOSIO_ACCOUNT_ID),
      x_cookies: Boolean(s.x_auth_token && s.x_ct0),
      x_status: s.x_status,
      instagram: s.fb_ig_username ? { username: s.fb_ig_username, page: s.fb_page_name } : null,
      facebook_app: Boolean(env.FB_APP_ID && s.fb_app_secret),
    },
  };
}

// ---------- router ----------

async function route(request, env, ctx) {
  const url = new URL(request.url);
  const path = url.pathname.replace(/\/+$/, "") || "/";
  const method = request.method;
  const origin = url.origin;

  if (path === "/health") return json({ ok: true });
  if (path === "/") {
    if (!(await hasSession(request, env))) return html(LOGIN_HTML);
    const s = await getSettings(env);
    if (s.public_url !== origin) await saveSettings(env, { public_url: origin });
    return html(DASHBOARD_HTML);
  }
  let m = path.match(/^\/d\/([\w-]{4,40})$/);
  if (m && method === "GET") return digestPage(request, env, url, m[1]);
  if (path === "/api/login" && method === "POST") {
    const { password } = await body(request);
    const s = await getSettings(env);
    if (!(await checkPassword(env, s, password))) {
      await new Promise((r) => setTimeout(r, 1000));
      throw new HttpError(401, "Wrong login code");
    }
    return json({ ok: true }, 200, { "set-cookie": sessionCookie(await makeSession(env, s)) });
  }
  if (path === "/api/logout" && method === "POST") {
    return json({ ok: true }, 200, { "set-cookie": `${SESSION_COOKIE}=; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=0` });
  }
  m = path.match(/^\/oauth\/facebook\/(start|callback)$/);
  if (m && method === "GET") return facebookOauth(request, env, url, m[1]);

  if (path.startsWith("/pipeline/")) {
    requirePipeline(request, env);
    return pipelineRoute(request, env, ctx, path, method, url);
  }
  if (path.startsWith("/api/")) {
    if (!(await hasSession(request, env))) throw new HttpError(401, "Please log in again");
    return panelRoute(request, env, ctx, path, method);
  }
  return new Response("not found", { status: 404 });
}

// Read-only digest page: logged-in dashboard users, or anyone holding the signed link from the email.
async function digestPage(request, env, url, id) {
  const token = url.searchParams.get("t") || "";
  const allowed = (token && safeEqual(token, await digestToken(env, id))) || (await hasSession(request, env));
  if (!allowed) return html(render.messagePage("Link not valid", "Open the digest from your email or log in to the dashboard.", false), 403);
  const d = await env.DB.prepare("SELECT * FROM digests WHERE id = ?").bind(id).first();
  if (!d) return html(render.messagePage("Digest not found", "It may have been deleted (digests are kept 60 days).", false), 404);
  const s = await getSettings(env);
  return html(render.digestPage(d, parseData(d), s));
}

async function facebookOauth(request, env, url, step) {
  const origin = url.origin;
  const st = await getSettings(env);
  if (!env.FB_APP_ID || !st.fb_app_secret) return html(render.messagePage("Not set up yet", "The Facebook app secret is missing (Connections tab).", false));
  const redirect = `${origin}/oauth/facebook/callback`;
  const stateSig = (exp) => hmac(env.PIPELINE_SECRET, `oauth:facebook:${exp}`);
  if (step === "start") {
    if (!(await hasSession(request, env))) return Response.redirect(`${origin}/`, 302);
    const exp = Date.now() + 15 * 60000;
    const q = new URLSearchParams({
      client_id: env.FB_APP_ID, redirect_uri: redirect, state: `${exp}.${await stateSig(exp)}`, response_type: "code",
      scope: "instagram_basic,instagram_manage_insights,pages_show_list,pages_read_engagement,business_management",
    });
    return Response.redirect(`https://www.facebook.com/dialog/oauth?${q}`, 302);
  }
  const code = url.searchParams.get("code");
  const [exp, sig] = String(url.searchParams.get("state") || "").split(".");
  if (!code || !exp || Number(exp) < Date.now() || !safeEqual(sig || "", await stateSig(exp))) {
    return html(render.messagePage("Not connected", url.searchParams.get("error_description") || "The login link expired. Start again from the dashboard.", false));
  }
  const g = (p, params) => fetch(`https://graph.facebook.com/${p}?${new URLSearchParams(params)}`).then((r) => r.json());
  const short = await g("oauth/access_token", { client_id: env.FB_APP_ID, client_secret: st.fb_app_secret, redirect_uri: redirect, code });
  if (!short.access_token) return html(render.messagePage("Not connected", JSON.stringify(short.error || short).slice(0, 200), false));
  const long = await g("oauth/access_token", { grant_type: "fb_exchange_token", client_id: env.FB_APP_ID, client_secret: st.fb_app_secret, fb_exchange_token: short.access_token });
  const user = long.access_token || short.access_token;
  const pages = await g("me/accounts", { fields: "name,access_token,instagram_business_account{id,username}", access_token: user });
  const page = (pages.data || []).find((p) => p.instagram_business_account);
  if (!page) return html(render.messagePage("No linked Instagram found", "None of your Facebook Pages has an Instagram professional account linked.", false));
  await saveSettings(env, {
    fb_token: page.access_token, fb_ig_id: page.instagram_business_account.id,
    fb_user_token: user, fb_user_expires: isoAt(Date.now() + (long.expires_in || 5184000) * 1000),
    fb_ig_username: page.instagram_business_account.username, fb_page_name: page.name,
  });
  return html(render.messagePage("Instagram reading connected", `Public Business/Creator Instagram accounts can now be read (via @${page.instagram_business_account.username}).`, true));
}

async function pipelineRoute(request, env, ctx, path, method, url) {
  let m;
  if (path === "/pipeline/config" && method === "GET") {
    const s = await getSettings(env);
    const id = url.searchParams.get("digest_id") || "";
    const [digest, sources, research, topics, seen] = await Promise.all([
      env.DB.prepare("SELECT id, kind, status, window_start, window_end, slot_at FROM digests WHERE id = ?").bind(id).first(),
      env.DB.prepare("SELECT id, kind, url, handle, name FROM sources WHERE enabled = 1 ORDER BY kind, id").all(),
      env.DB.prepare("SELECT id, prompt, topic, max_items FROM research WHERE enabled = 1 ORDER BY id").all(),
      env.DB.prepare("SELECT name, description, emoji, keywords FROM topics WHERE enabled = 1 ORDER BY position, id").all(),
      env.DB.prepare("SELECT id FROM seen WHERE seen_at > ?").bind(isoAt(Date.now() - 8 * 86400000)).all(),
    ]);
    if (!digest) throw new HttpError(404, `digest ${id} not found`);
    return json({
      settings: publicSettings(s),
      digest,
      sources: sources.results,
      research: research.results,
      topics: topics.results,
      seen: seen.results.map((r) => r.id),
      x_cookies: s.x_auth_token && s.x_ct0 ? { auth_token: s.x_auth_token, ct0: s.x_ct0 } : null,
    });
  }
  // Manual runs started from the GitHub page (no digest yet) get a preview digest.
  if (path === "/pipeline/digests" && method === "POST") {
    const s = await getSettings(env);
    const id = `preview-${randomId(4)}`;
    const now = Date.now();
    await env.DB.prepare(
      `INSERT INTO digests (id, kind, trigger, status, slot_at, window_start, window_end, send_email, created_at)
       VALUES (?, 'preview', 'github', 'collecting', ?, ?, ?, 0, ?)`,
    ).bind(id, isoAt(now), await windowFor(env, s, "preview", now), isoAt(now), isoAt(now)).run();
    return json({ id });
  }
  if ((m = path.match(/^\/pipeline\/digests\/([\w-]+)$/)) && method === "POST") {
    const b = await body(request);
    const d = await env.DB.prepare("SELECT * FROM digests WHERE id = ?").bind(m[1]).first();
    if (!d) throw new HttpError(404, "digest not found");
    const stmts = [];
    if (b.status === "ready") {
      stmts.push(env.DB.prepare("UPDATE digests SET status = 'ready', ready_at = ?, data = ?, item_count = ?, story_count = ?, error = ? WHERE id = ?")
        .bind(nowIso(), JSON.stringify(b.data || {}), b.item_count || 0, b.story_count || 0, b.warning || null, d.id));
      if (d.kind !== "preview" && Array.isArray(b.seen) && b.seen.length) {
        stmts.push(env.DB.prepare("INSERT OR IGNORE INTO seen (id, seen_at) SELECT value, ? FROM json_each(?)").bind(nowIso(), JSON.stringify(b.seen.map(String))));
      }
    } else {
      stmts.push(env.DB.prepare("UPDATE digests SET status = 'failed', error = ? WHERE id = ?").bind(String(b.error || "collection failed").slice(0, 1000), d.id));
    }
    for (const r of (b.source_status || []).slice(0, 200)) {
      stmts.push(env.DB.prepare("UPDATE sources SET last_fetched_at = ?, last_error = ?, last_count = ? WHERE id = ?").bind(nowIso(), r.error || null, r.count || 0, r.id));
    }
    for (const r of (b.research_status || []).slice(0, 100)) {
      stmts.push(env.DB.prepare("UPDATE research SET last_run_at = ?, last_error = ?, last_count = ?, last_queries = ? WHERE id = ?")
        .bind(nowIso(), r.error || null, r.count || 0, JSON.stringify(r.queries || []), r.id));
    }
    await env.DB.batch(stmts);
    if (b.x_status) await saveSettings(env, { x_status: { ...b.x_status, at: nowIso() } });
    if (b.ai_calls) await addUsage(env, "ai_calls", Number(b.ai_calls) || 0);
    if (b.status === "ready" && d.send_email && Date.parse(d.slot_at) <= Date.now()) {
      const s = await getSettings(env);
      const fresh = await env.DB.prepare("SELECT * FROM digests WHERE id = ?").bind(d.id).first();
      ctx.waitUntil(deliver(env, s, fresh));
    }
    return json({ ok: true });
  }
  if (path === "/pipeline/x-probe" && method === "GET") {
    const s = await getSettings(env);
    const cookie = `auth_token=${s.x_auth_token}; ct0=${s.x_ct0}`;
    const ua = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36";
    const out = {};
    for (const u of ["https://x.com/", "https://x.com/home", "https://x.com/MarioNawfal", "https://abs.twimg.com/responsive-web/client-web/main.js"]) {
      try {
        const r = await fetch(u, { headers: { "user-agent": ua, cookie }, redirect: "manual" });
        out[u] = { status: r.status, type: r.headers.get("content-type"), cf: r.headers.get("cf-ray") ? "cf" : "", head: (await r.text()).slice(0, 120) };
      } catch (e) { out[u] = { error: String(e).slice(0, 100) }; }
    }
    return json(out);
  }
  if (path === "/pipeline/x-config" && method === "GET") {
    const s = await getSettings(env);
    const sources = await env.DB.prepare("SELECT id, kind, url, handle, name FROM sources WHERE kind = 'x' ORDER BY id").all();
    return json({
      settings: publicSettings(s),
      sources: sources.results,
      x_cookies: s.x_auth_token && s.x_ct0 ? { auth_token: s.x_auth_token, ct0: s.x_ct0 } : null,
    });
  }
  if (path === "/pipeline/x-report" && method === "POST") {
    const b = await body(request);
    const stmts = [];
    for (const r of (b.results || []).slice(0, 200)) {
      stmts.push(env.DB.prepare("UPDATE sources SET last_fetched_at = ?, last_error = ?, last_count = ? WHERE id = ?")
        .bind(nowIso(), r.error || null, r.count || 0, r.id));
    }
    if (stmts.length) await env.DB.batch(stmts);
    if (b.x_status) await saveSettings(env, { x_status: { ...b.x_status, at: nowIso() } });
    return json({ ok: true });
  }
  if (path === "/pipeline/runs" && method === "POST") {
    const b = await body(request);
    const id = String(b.id || randomId()).slice(0, 32);
    await env.DB.prepare(
      `INSERT INTO runs (id, digest_id, started_at, mode, trigger, status, gh_run_url) VALUES (?, ?, ?, ?, ?, 'running', ?)
       ON CONFLICT(id) DO UPDATE SET status = 'running', gh_run_url = excluded.gh_run_url, digest_id = COALESCE(excluded.digest_id, runs.digest_id)`,
    ).bind(id, b.digest_id || null, nowIso(), b.mode || "preview", b.trigger || "manual", b.gh_run_url || null).run();
    return json({ id });
  }
  if ((m = path.match(/^\/pipeline\/runs\/([\w-]+)$/)) && method === "PATCH") {
    const b = await body(request);
    await env.DB.prepare("UPDATE runs SET status = ?, summary = ?, log = ?, finished_at = ? WHERE id = ?")
      .bind(b.status || "done", b.summary || "", b.log || "", nowIso(), m[1]).run();
    return json({ ok: true });
  }
  if (path === "/pipeline/ai/text" && method === "POST") return json(await aiText(env, await body(request)));
  if (path === "/pipeline/ig-media" && method === "GET") {
    const s = await getSettings(env);
    const p = url.searchParams;
    return json(await instagramMedia(s, p.get("username"), { limit: Number(p.get("limit")) || 25, after: p.get("after") || "" }));
  }
  throw new HttpError(404, "unknown pipeline route");
}

const SOURCE_KINDS = ["rss", "facebook", "x", "instagram"];

// Accepts a Cookie-Editor / browser-extension JSON export, a "Cookie:" header line, name<TAB>value rows
// copied from the browser's cookie table, or the two bare values on separate lines.
function parseXCookies(text) {
  let auth = "";
  let ct0 = "";
  const clean = (v) => String(v || "").trim().replace(/^["']|["']$/g, "");
  try {
    const parsed = JSON.parse(text);
    const list = Array.isArray(parsed) ? parsed : Array.isArray(parsed?.cookies) ? parsed.cookies : [];
    for (const c of list) {
      if (c?.name === "auth_token") auth = clean(c.value);
      if (c?.name === "ct0") ct0 = clean(c.value);
    }
  } catch { /* not JSON */ }
  auth = auth || clean((text.match(/auth_token["']?\s*(?:=|:|\t)\s*["']?([A-Za-z0-9]{30,64})/) || [])[1]);
  ct0 = ct0 || clean((text.match(/\bct0["']?\s*(?:=|:|\t)\s*["']?([A-Za-z0-9]{32,200})/) || [])[1]);
  if (!auth || !ct0) {
    const bare = text.split(/[\s,;]+/).map(clean).filter((v) => /^[A-Za-z0-9]{30,200}$/.test(v));
    if (bare.length === 2 && !/auth_token|ct0/.test(text)) {
      const [a, b] = bare[0].length <= bare[1].length ? bare : [bare[1], bare[0]];
      auth = auth || a;
      ct0 = ct0 || b;
    }
  }
  return { auth, ct0 };
}

async function panelRoute(request, env, ctx, path, method) {
  let m;
  if (path === "/api/state" && method === "GET") return json(await panelState(env));
  if (path === "/api/settings" && method === "POST") {
    const patch = cleanSettingsPatch(await body(request));
    await saveSettings(env, patch);
    return json({ ok: true, saved: Object.keys(patch) });
  }
  if (path === "/api/toggle" && method === "POST") {
    const { enabled } = await body(request);
    await saveSettings(env, { enabled: Boolean(enabled) });
    return json({ ok: true, enabled: Boolean(enabled) });
  }
  if (path === "/api/run" && method === "POST") {
    const b = await body(request);
    const s = await getSettings(env);
    const busy = await env.DB.prepare("SELECT COUNT(*) AS n FROM digests WHERE status = 'collecting'").first();
    if (busy?.n) throw new HttpError(409, "A collection run is already in progress. Wait for it to finish.");
    const preview = Boolean(b.preview);
    const id = `${preview ? "preview" : "manual"}-${zonedParts(s.timezone, Date.now()).date}-${randomId(3)}`;
    return json({ ok: true, ...(await startDigest(env, s, { id, kind: preview ? "preview" : "manual", trigger: "dashboard", slotAt: Date.now() })) });
  }

  // Digests
  if ((m = path.match(/^\/api\/digests\/([\w-]+)$/))) {
    const d = await env.DB.prepare("SELECT * FROM digests WHERE id = ?").bind(m[1]).first();
    if (!d) throw new HttpError(404, "Digest not found");
    if (method === "GET") {
      const s = await getSettings(env);
      return json({ digest: digestSummary(d), html: render.digestFragment(d, parseData(d), s), link: `/d/${d.id}?t=${await digestToken(env, d.id)}` });
    }
    if (method === "DELETE") {
      await env.DB.prepare("DELETE FROM digests WHERE id = ?").bind(d.id).run();
      return json({ ok: true });
    }
  }
  if ((m = path.match(/^\/api\/digests\/([\w-]+)\/send$/)) && method === "POST") {
    const d = await env.DB.prepare("SELECT * FROM digests WHERE id = ?").bind(m[1]).first();
    if (!d || !d.data) throw new HttpError(404, "This digest has no content to send");
    const s = await getSettings(env);
    const out = await deliver(env, { ...s, email_when_empty: true }, d);
    if (!out.ok) throw new HttpError(502, out.error);
    return json(out);
  }

  // Sources
  if (path === "/api/sources" && method === "POST") {
    const b = await body(request);
    const kindHint = SOURCE_KINDS.includes(b.kind) ? b.kind : "";
    const src = await resolveSource(b.input, kindHint);
    const name = String(b.name || "").trim().slice(0, 80) || src.name;
    const res = await env.DB.prepare("INSERT OR IGNORE INTO sources (kind, url, handle, name, created_at) VALUES (?, ?, ?, ?, ?)")
      .bind(src.kind, src.url, src.handle, name, nowIso()).run();
    if (!res.meta?.changes) throw new HttpError(409, "That source is already in the list");
    return json({ ok: true, source: { ...src, name } });
  }
  if ((m = path.match(/^\/api\/sources\/(\d+)$/))) {
    const id = Number(m[1]);
    if (method === "DELETE") {
      await env.DB.prepare("DELETE FROM sources WHERE id = ?").bind(id).run();
      return json({ ok: true });
    }
    if (method === "PATCH") {
      const b = await body(request);
      const sets = [];
      const vals = [];
      if ("name" in b) {
        sets.push("name = ?");
        vals.push(String(b.name || "").trim().slice(0, 80));
      }
      if ("enabled" in b) {
        sets.push("enabled = ?");
        vals.push(b.enabled ? 1 : 0);
      }
      if ("input" in b) {
        const cur = await env.DB.prepare("SELECT kind FROM sources WHERE id = ?").bind(id).first();
        const src = await resolveSource(b.input, cur?.kind);
        sets.push("kind = ?", "url = ?", "handle = ?", "last_error = NULL");
        vals.push(src.kind, src.url, src.handle);
      }
      if (!sets.length) throw new HttpError(400, "nothing to change");
      await env.DB.prepare(`UPDATE sources SET ${sets.join(", ")} WHERE id = ?`).bind(...vals, id).run();
      return json({ ok: true });
    }
  }
  if ((m = path.match(/^\/api\/sources\/(\d+)\/test$/)) && method === "POST") {
    const src = await env.DB.prepare("SELECT * FROM sources WHERE id = ?").bind(Number(m[1])).first();
    if (!src) throw new HttpError(404, "Source not found");
    try {
      return json(await testSource(env, await getSettings(env), src));
    } catch (err) {
      return json({ ok: false, message: err.message });
    }
  }

  // Research prompts
  if (path === "/api/research" && method === "POST") {
    const b = await body(request);
    const prompt = String(b.prompt || "").trim();
    if (prompt.length < 5) throw new HttpError(400, "Write what news to look for");
    if (prompt.length > 1000) throw new HttpError(400, "Research prompt is too long (1000 characters max)");
    const max = Math.max(1, Math.min(25, Math.round(Number(b.max_items) || 8)));
    await env.DB.prepare("INSERT INTO research (prompt, topic, max_items, created_at) VALUES (?, ?, ?, ?)")
      .bind(prompt, String(b.topic || "").slice(0, 60), max, nowIso()).run();
    return json({ ok: true });
  }
  if ((m = path.match(/^\/api\/research\/(\d+)$/))) {
    const id = Number(m[1]);
    if (method === "DELETE") {
      await env.DB.prepare("DELETE FROM research WHERE id = ?").bind(id).run();
      return json({ ok: true });
    }
    if (method === "PATCH") {
      const b = await body(request);
      const sets = [];
      const vals = [];
      if ("prompt" in b) {
        const prompt = String(b.prompt || "").trim();
        if (prompt.length < 5 || prompt.length > 1000) throw new HttpError(400, "Research prompt must be 5 to 1000 characters");
        sets.push("prompt = ?");
        vals.push(prompt);
      }
      if ("topic" in b) {
        sets.push("topic = ?");
        vals.push(String(b.topic || "").slice(0, 60));
      }
      if ("max_items" in b) {
        sets.push("max_items = ?");
        vals.push(Math.max(1, Math.min(25, Math.round(Number(b.max_items) || 8))));
      }
      if ("enabled" in b) {
        sets.push("enabled = ?");
        vals.push(b.enabled ? 1 : 0);
      }
      if (!sets.length) throw new HttpError(400, "nothing to change");
      await env.DB.prepare(`UPDATE research SET ${sets.join(", ")} WHERE id = ?`).bind(...vals, id).run();
      return json({ ok: true });
    }
  }

  // Topics
  if (path === "/api/topics" && method === "POST") {
    const b = await body(request);
    const name = String(b.name || "").trim().slice(0, 60);
    if (!name) throw new HttpError(400, "Topic heading is empty");
    const pos = await env.DB.prepare("SELECT COALESCE(MAX(position), 0) + 1 AS p FROM topics").first();
    const res = await env.DB.prepare("INSERT OR IGNORE INTO topics (name, description, emoji, keywords, position) VALUES (?, ?, ?, ?, ?)")
      .bind(name, String(b.description || "").slice(0, 600), String(b.emoji || "").slice(0, 8), String(b.keywords || "").slice(0, 1000), pos.p).run();
    if (!res.meta?.changes) throw new HttpError(409, "That topic already exists");
    return json({ ok: true });
  }
  if (path === "/api/topics/order" && method === "POST") {
    const { ids = [] } = await body(request);
    await env.DB.batch(ids.slice(0, 100).map((id, i) => env.DB.prepare("UPDATE topics SET position = ? WHERE id = ?").bind(i + 1, Number(id))));
    return json({ ok: true });
  }
  if ((m = path.match(/^\/api\/topics\/(\d+)$/))) {
    const id = Number(m[1]);
    if (method === "DELETE") {
      await env.DB.prepare("DELETE FROM topics WHERE id = ?").bind(id).run();
      return json({ ok: true });
    }
    if (method === "PATCH") {
      const b = await body(request);
      const name = String(b.name || "").trim().slice(0, 60);
      if (!name) throw new HttpError(400, "Topic heading is empty");
      await env.DB.prepare("UPDATE topics SET name = ?, description = ?, emoji = ?, keywords = ?, enabled = ? WHERE id = ?")
        .bind(name, String(b.description || "").slice(0, 600), String(b.emoji || "").slice(0, 8), String(b.keywords || "").slice(0, 1000),
          b.enabled === false ? 0 : 1, id).run();
      return json({ ok: true });
    }
  }

  // Account and connections
  if (path === "/api/password" && method === "POST") {
    const b = await body(request);
    const s = await getSettings(env);
    if (!(await checkPassword(env, s, b.current))) throw new HttpError(400, "Current login code is wrong");
    const next = String(b.new_password || "");
    if (next.length < 6) throw new HttpError(400, "New login code must be at least 6 characters");
    const panel_password_hash = await hashPassword(next);
    await saveSettings(env, { panel_password_hash });
    return json({ ok: true }, 200, { "set-cookie": sessionCookie(await makeSession(env, { ...s, panel_password_hash })) });
  }
  if (path === "/api/x-cookies" && method === "POST") {
    const b = await body(request);
    let { auth, ct0 } = parseXCookies(String(b.cookie || ""));
    auth = auth || String(b.auth_token || "").trim();
    ct0 = ct0 || String(b.ct0 || "").trim();
    if (!auth || !ct0) {
      throw new HttpError(400, `Could not find ${!auth && !ct0 ? "auth_token and ct0" : !auth ? "auth_token" : "ct0"} in what you pasted. ` +
        "Paste the Cookie-Editor export (Export > JSON), or the line auth_token=...; ct0=...");
    }
    await saveSettings(env, { x_auth_token: auth, x_ct0: ct0, x_status: { ok: null, message: "Saved. Testing from GitHub...", at: nowIso() } });
    return json({ ok: true });
  }
  if (path === "/api/x-test" && method === "POST") {
    const s = await getSettings(env);
    if (!s.x_auth_token || !s.x_ct0) throw new HttpError(400, "Save the X cookies first.");
    const busy = await env.DB.prepare("SELECT COUNT(*) AS n FROM runs WHERE mode = 'xtest' AND status IN ('dispatched','running') AND started_at > ?")
      .bind(isoAt(Date.now() - 20 * 60000)).first();
    if (busy?.n) throw new HttpError(409, "An X test is already running. Wait for it to finish.");
    await saveSettings(env, { x_status: { ok: null, message: "Testing from GitHub (about 1-3 minutes)...", at: nowIso() } });
    return json({ ok: true, run_id: await dispatch(env, { mode: "xtest", digestId: "", trigger: "dashboard" }) });
  }
  if (path === "/api/x-cookies" && method === "DELETE") {
    await saveSettings(env, { x_auth_token: null, x_ct0: null, x_status: null });
    return json({ ok: true });
  }
  if (path === "/api/app-secret" && method === "POST") {
    const secret = String((await body(request)).secret || "").trim();
    if (!/^[a-f0-9]{24,64}$/i.test(secret)) throw new HttpError(400, "That doesn't look like an app secret (32 letters and numbers).");
    await saveSettings(env, { fb_app_secret: secret });
    return json({ ok: true });
  }
  if (path === "/api/test-email" && method === "POST") {
    const s = await getSettings(env);
    const html = render.testEmail(s);
    try {
      await sendEmail(env, s, `${s.digest_title}: test email`, html);
    } catch (err) {
      throw new HttpError(502, err.message);
    }
    await addUsage(env, "emails", 1);
    return json({ ok: true, to: s.email_recipients });
  }
  throw new HttpError(404, "unknown route");
}
