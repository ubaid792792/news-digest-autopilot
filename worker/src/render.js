// Digest rendering shared by the email, the public digest page and the dashboard.
// Inline styles only, so Gmail and other mail apps show it the same way as the web page.

const esc = (v) =>
  String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

const KIND_LABEL = { rss: "Web", facebook: "Facebook", x: "X", instagram: "Instagram", research: "Web search" };
const FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif";
const C = { text: "#16181d", muted: "#6b7280", line: "#e5e7eb", accent: "#2563eb", soft: "#f3f4f6", warn: "#b45309" };

function fmt(s, iso, withDate = true) {
  if (!iso) return "";
  try {
    return new Intl.DateTimeFormat("en-GB", {
      timeZone: s.timezone || "UTC", hour: "2-digit", minute: "2-digit", hour12: false,
      ...(withDate ? { day: "numeric", month: "short" } : {}),
    }).format(new Date(iso));
  } catch {
    return iso.slice(0, 16).replace("T", " ");
  }
}

function sameDay(s, a, b) {
  return fmt(s, a).split(",")[0] === fmt(s, b).split(",")[0];
}

const MAX_LINKS = 4;
const safeUrl = (u) => (/^https?:\/\//i.test(String(u || "")) ? String(u) : "");

// One-liner mode: one compact line per story (everything in a single sentence) with small time and links.
function storyLine(st, s) {
  const text = st.text || (Array.isArray(st.bullets) ? st.bullets.filter(Boolean).join("; ") : "") || st.headline || "";
  const meta = [];
  if (s.show_time !== false && st.latest_at) meta.push(esc(fmt(s, st.latest_at)));
  if (s.show_sources !== false && st.sources?.length) {
    const shown = st.sources.slice(0, 3).map((src) => {
      const url = safeUrl(src.url);
      const label = esc(src.name || "Source");
      return url ? `<a href="${esc(url)}" style="color:${C.accent};text-decoration:none">${label}</a>` : label;
    });
    const more = st.sources.length - 3;
    meta.push(shown.join(", ") + (more > 0 ? ` +${more}` : ""));
  }
  const notes = (st.notes || []).map((n) => ` <span style="color:${C.warn};font-size:12px">(${esc(n)})</span>`).join("");
  return `<div style="padding:7px 0;border-bottom:1px solid ${C.line};font-size:15px;line-height:1.45">&bull; ${esc(text)}${meta.length ? ` <span style="font-size:12px;color:${C.muted}">[${meta.join(" &middot; ")}]</span>` : ""}${notes}</div>`;
}

function storyHtml(st, s) {
  if (s.item_style === "oneline") return storyLine(st, s);
  const bullets = Array.isArray(st.bullets) ? st.bullets.filter(Boolean) : [];
  const bodyHtml = bullets.length
    ? `<ul style="margin:6px 0 0;padding-left:20px">${bullets.map((b) => `<li style="margin:3px 0">${esc(b)}</li>`).join("")}</ul>`
    : `<div style="margin-top:4px">${esc(st.text || "")}</div>`;
  const meta = [];
  if (s.show_time !== false && st.latest_at) meta.push(esc(fmt(s, st.latest_at)));
  if (s.show_sources !== false && st.sources?.length) {
    const shown = st.sources.slice(0, MAX_LINKS).map((src) => {
      const label = `${esc(src.name || "Source")}${src.kind ? ` <span style="color:${C.muted}">(${esc(KIND_LABEL[src.kind] || src.kind)})</span>` : ""}`;
      const url = safeUrl(src.url);
      return url ? `<a href="${esc(url)}" style="color:${C.accent};text-decoration:none">${label}</a>` : label;
    });
    const more = st.sources.length - MAX_LINKS;
    meta.push(shown.join(", ") + (more > 0 ? ` and ${more} more` : ""));
  }
  const notes = (st.notes || []).map((n) => `<div style="color:${C.warn};font-size:12px;margin-top:3px">${esc(n)}</div>`).join("");
  return `<div style="padding:12px 0;border-bottom:1px solid ${C.line}">
<div style="font-weight:700;font-size:16px;line-height:1.35">${esc(st.headline || "")}</div>
<div style="font-size:15px;line-height:1.55;color:${C.text}">${bodyHtml}</div>
${meta.length ? `<div style="font-size:12.5px;color:${C.muted};margin-top:6px;line-height:1.5">${meta.join(" &middot; ")}</div>` : ""}${notes}
</div>`;
}

function headingHtml(name, emoji, count, s) {
  const head = `${s.show_emoji !== false && emoji ? `${esc(emoji)} ` : ""}${esc(name)}`;
  return `<div style="font-size:19px;font-weight:800;padding:6px 10px;border-left:4px solid ${C.accent};background:${C.soft};border-radius:4px">${head}
<span style="font-weight:600;color:${C.muted};font-size:14px">&nbsp;${count}</span></div>`;
}

function topicHtml(t, s, idx) {
  const body = t.stories.length
    ? t.stories.map((st) => storyHtml(st, s)).join("\n")
    : `<div style="padding:10px 0;color:${C.muted};font-size:14px;border-bottom:1px solid ${C.line}">No new news in this period.</div>`;
  return `<div style="margin-top:22px" id="t${idx}">
${headingHtml(t.name, t.emoji, t.stories.length, s)}
${body}
</div>`;
}

// One line per off-topic post: headline, outlet link and time.
function otherHtml(data, s, idx) {
  const items = data?.other || [];
  if (!items.length) return "";
  const rows = items.map((o) => {
    const url = safeUrl(o.url);
    const src = url ? `<a href="${esc(url)}" style="color:${C.accent};text-decoration:none">${esc(o.source)}</a>` : esc(o.source);
    return `<li style="margin:5px 0">${esc(o.title)} <span style="color:${C.muted};font-size:12.5px">&middot; ${src}${o.posted_at && s.show_time !== false ? ` &middot; ${esc(fmt(s, o.posted_at))}` : ""}</span></li>`;
  }).join("");
  return `<div style="margin-top:22px" id="t${idx}">
${headingHtml(data.other_name || s.other_topic_name || "Other News", "", items.length, s)}
<ul style="margin:8px 0 0;padding-left:20px;font-size:14px;line-height:1.45">${rows}</ul>
</div>`;
}

// Headings shown: those with stories, plus empty ones when the user wants every heading listed.
const visibleTopics = (data, s) => (data?.topics || []).filter((t) => t.stories?.length || s.show_empty_topics !== false);

function headerHtml(d, data, s, partLabel = "") {
  const title = esc(data?.title || s.digest_title || "News Digest");
  const start = d.window_start;
  const end = d.window_end;
  const range = start && end ? `${fmt(s, start)} &ndash; ${sameDay(s, start, end) ? fmt(s, end, false) : fmt(s, end)}` : "";
  const counts = `${d.story_count ?? 0} stories from ${d.item_count ?? 0} posts`;
  return `<div style="font-size:24px;font-weight:800;line-height:1.2">${title}${partLabel ? ` <span style="font-size:15px;color:${C.muted}">${esc(partLabel)}</span>` : ""}</div>
<div style="color:${C.muted};font-size:13px;margin-top:4px">${range}${range ? " &middot; " : ""}${counts} &middot; times in ${esc(s.timezone)}</div>`;
}

function contentsHtml(topics, s, data) {
  const list = topics.filter((t) => t.stories.length);
  const other = data?.other?.length ? [{ name: data.other_name || "Other News", emoji: "", count: data.other.length, idx: topics.length }] : [];
  const chips = [...list.map((t) => ({ name: t.name, emoji: t.emoji, count: t.stories.length, idx: topics.indexOf(t) })), ...other];
  if (chips.length < 2) return "";
  return `<div style="margin-top:12px">${chips.map((c) =>
    `<a href="#t${c.idx}" style="display:inline-block;margin:6px 6px 0 0;padding:4px 10px;border:1px solid ${C.line};border-radius:999px;color:${C.text};text-decoration:none;font-size:13px">${s.show_emoji !== false && c.emoji ? `${esc(c.emoji)} ` : ""}${esc(c.name)} <b>${c.count}</b></a>`).join("")}</div>`;
}

function footerHtml(data) {
  const warnings = (data?.warnings || []).slice(0, 30);
  if (!warnings.length) return "";
  return `<div style="margin-top:24px;padding:10px 12px;background:#fffbeb;border:1px solid #fde68a;border-radius:8px;font-size:12.5px;color:${C.warn}">
<b>Collection notes</b><br>${warnings.map(esc).join("<br>")}</div>`;
}

function wrap(inner) {
  return `<div style="font-family:${FONT};color:${C.text};background:#ffffff;max-width:760px;margin:0 auto;padding:20px 18px;border-radius:12px">${inner}</div>`;
}

function statusNote(d) {
  const msg = {
    collecting: "This digest is being collected right now. It appears here when the run finishes.",
    failed: `The collection run failed: ${d.error || "unknown error"}`,
  }[d.status];
  return msg ? `<div style="margin-top:16px;color:${C.muted}">${esc(msg)}</div>` : "";
}

export function digestFragment(d, data, s) {
  const topics = visibleTopics(data, s);
  const anything = topics.some((t) => t.stories.length) || data?.other?.length;
  const empty = data && !anything ? `<div style="margin-top:16px;color:${C.muted}">No new posts from your sources in this window.</div>` : "";
  return wrap(`${headerHtml(d, data, s)}${statusNote(d)}${contentsHtml(topics, s, data)}${topics.map((t, i) => topicHtml(t, s, i)).join("\n")}${otherHtml(data, s, topics.length)}${empty}${footerHtml(data)}`);
}

export function digestPage(d, data, s) {
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex"><title>${esc(s.digest_title || "News Digest")} &middot; ${esc(fmt(s, d.slot_at))}</title></head>
<body style="margin:0;background:#eef0f3;padding:12px">${digestFragment(d, data, s)}
<div style="text-align:center;margin:14px 0;font:13px ${FONT}"><a href="/" style="color:${C.accent}">Open dashboard</a></div></body></html>`;
}

// Gmail clips messages above ~102 KB, so long digests are split into parts at topic boundaries.
const PART_LIMIT = 85000;

export function emailParts(d, data, s, link) {
  const topics = visibleTopics(data, s);
  const blocks = [...topics.map((t, i) => topicHtml(t, s, i)), otherHtml(data, s, topics.length)].filter(Boolean);
  const groups = [];
  let cur = [];
  let size = 0;
  for (const b of blocks) {
    if (cur.length && size + b.length > PART_LIMIT) {
      groups.push(cur);
      cur = [];
      size = 0;
    }
    cur.push(b);
    size += b.length;
  }
  if (cur.length || !groups.length) groups.push(cur);
  const linkHtml = `<div style="margin-top:10px;font-size:13px"><a href="${esc(link)}" style="color:${C.accent}">Open this digest in the browser</a></div>`;
  const local = new Date(d.slot_at || Date.now());
  const subjectBase = String(s.email_subject || "{title} | {date}, {time}")
    .replace("{title}", s.digest_title || "News Digest")
    .replace("{date}", new Intl.DateTimeFormat("en-GB", { timeZone: s.timezone, day: "numeric", month: "short" }).format(local))
    .replace("{time}", new Intl.DateTimeFormat("en-GB", { timeZone: s.timezone, hour: "2-digit", minute: "2-digit", hour12: false }).format(local))
    .replace("{stories}", String(d.story_count ?? 0));
  return groups.map((g, i) => {
    const part = groups.length > 1 ? `(part ${i + 1} of ${groups.length})` : "";
    const inner = `${headerHtml(d, data, s, part)}${linkHtml}${i === 0 ? contentsHtml(topics, s, data) : ""}${g.join("\n")}${i === groups.length - 1 ? footerHtml(data) : ""}`;
    return { subject: part ? `${subjectBase} ${part}` : subjectBase, html: `<div style="background:#eef0f3;padding:10px">${wrap(inner)}</div>` };
  });
}

export function testEmail(s) {
  return wrap(`<div style="font-size:20px;font-weight:800">${esc(s.digest_title || "News Digest")}</div>
<p style="font-size:15px">Test email: delivery works. Your digests will arrive at ${esc((s.slots || []).join(", ") || "the times you set")} (${esc(s.timezone)}).</p>`);
}

export function messagePage(title, text, ok) {
  return `<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>${esc(title)}</title>
<body style="font:16px ${FONT};display:grid;place-items:center;min-height:100vh;margin:0;background:#f4f5f7;color:#16181d;padding:16px">
<div style="background:#fff;border:1px solid #e5e7eb;border-radius:16px;padding:28px;max-width:440px;text-align:center">
<div style="font-size:40px">${ok ? "&#10003;" : "&#9888;"}</div><h2 style="margin:8px 0">${esc(title)}</h2><p style="color:#6b7280">${esc(text)}</p>
<a href="/" style="display:inline-block;margin-top:10px;padding:10px 18px;border-radius:10px;background:#16181d;color:#fff;text-decoration:none">Back to the dashboard</a></div></body>`;
}
