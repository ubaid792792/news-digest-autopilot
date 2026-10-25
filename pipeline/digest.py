"""Turn collected posts into the digest: group same-event posts, sort them under the user's topic
headings, then write each story short in the chosen language while keeping every fact."""
from __future__ import annotations

import difflib
import logging
import re
from datetime import datetime, timezone

from . import prompts
from .ai import TextAI
from .sources import Post

log = logging.getLogger("digest")

SORT_BATCH = 120
WRITE_CHAR_BUDGET = 14000
KIND_LABEL = {"rss": "website", "facebook": "Facebook", "x": "X", "instagram": "Instagram"}
STOPWORDS = set("a an the of in on at to for and or but with from by as is are was were be after over".split())


def title_key(title: str) -> str:
    words = re.findall(r"\w+", (title or "").lower())
    return " ".join(w for w in words if w not in STOPWORDS)


def is_similar(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if difflib.SequenceMatcher(None, a, b).ratio() >= 0.72:
        return True
    wa, wb = set(a.split()), set(b.split())
    return bool(wa and wb) and len(wa & wb) / len(wa | wb) >= 0.6


def when(p: Post) -> str:
    return p.posted_at.strftime("%d %b %H:%M UTC") if p.posted_at else "time unknown"


def body_of(p: Post) -> str:
    """Post text without the title repeated at its start."""
    text = p.text or ""
    if p.title and text.startswith(p.title[:60]):
        return text
    return f"{p.title}\n{text}".strip() if p.title and p.title not in text else text


# ---------- step 1: group and sort ----------

def _keyword_topic(p: Post, topics: list[dict], other: str) -> str:
    text = f"{p.title} {p.text}".lower()
    best, score = other, 0
    for t in topics:
        words = {w for w in re.findall(r"\w{4,}", f"{t['name']} {t.get('description', '')}".lower())}
        hits = sum(1 for w in words if w in text)
        if hits > score:
            best, score = t["name"], hits
    return best


def _fallback_groups(batch: list[Post], topics: list[dict], other: str) -> list[dict]:
    groups: list[dict] = []
    for p in batch:
        key = title_key(p.title)
        match = next((g for g in groups if is_similar(key, g["key"])), None)
        if match:
            match["posts"].append(p)
        else:
            groups.append({"topic": _keyword_topic(p, topics, other), "posts": [p], "key": key})
    return groups


def sort_posts(ai: TextAI, posts: list[Post], topics: list[dict], s: dict, warnings: list[str]) -> list[dict]:
    other = s.get("other_topic_name") or "Other News"
    names = {t["name"].lower(): t["name"] for t in topics}
    extra = (s.get("sort_prompt") or "").strip()
    system = prompts.SORT_SYSTEM.format(topics=prompts.topics_text(topics), other=other,
                                        extra=f"\nExtra instructions from the editor:\n{extra}\n" if extra else "")
    groups: list[dict] = []
    for start in range(0, len(posts), SORT_BATCH):
        batch = posts[start:start + SORT_BATCH]
        lines = []
        for n, p in enumerate(batch, 1):
            snippet = re.sub(r"\s+", " ", f"{p.text} {p.transcript}".replace(p.title, "", 1)).strip()[:260]
            lines.append(f"[{n}] {p.source_name} ({KIND_LABEL.get(p.kind, p.kind)}) · {when(p)} · {p.title} — {snippet}")

        def check(data):
            if not isinstance(data, dict) or not isinstance(data.get("groups"), list):
                raise ValueError("answer has no groups list")

        try:
            data = ai.json(system, prompts.SORT_USER.format(posts="\n".join(lines)), check=check)
        except Exception as exc:  # noqa: BLE001
            log.warning("sorting failed, using simple grouping: %s", exc)
            warnings.append("AI sorting was unavailable for some posts; they were grouped by headline similarity.")
            groups.extend(_fallback_groups(batch, topics, other))
            continue
        used: set[int] = set()
        batch_groups = []
        for g in data["groups"]:
            if not isinstance(g, dict):
                continue
            nums = []
            for n in g.get("posts") or g.get("items") or []:
                try:
                    n = int(n)
                except (TypeError, ValueError):
                    continue
                if 1 <= n <= len(batch) and n not in used:
                    used.add(n)
                    nums.append(n)
            if not nums:
                continue
            topic = names.get(str(g.get("topic", "")).strip().lower(), other)
            members = [batch[n - 1] for n in nums]
            batch_groups.append({"topic": topic, "posts": members, "key": title_key(members[0].title)})
        missing = [p for n, p in enumerate(batch, 1) if n not in used]
        if missing:
            log.info("%d posts were not grouped by the AI; adding them separately", len(missing))
            batch_groups.extend(_fallback_groups(missing, topics, other))
        groups.extend(batch_groups)
    if len(posts) > SORT_BATCH:
        groups = _merge_across_batches(groups)
    return groups


def _merge_across_batches(groups: list[dict]) -> list[dict]:
    merged: list[dict] = []
    for g in groups:
        match = next((m for m in merged if m["topic"] == g["topic"] and is_similar(g["key"], m["key"])), None)
        if match:
            match["posts"].extend(g["posts"])
        else:
            merged.append(g)
    return merged


# ---------- step 2: write ----------

def _story_material(sid: str, g: dict, share: int) -> str:
    parts = [f"### Story {sid} (topic: {g['topic']})"]
    per_post = max(400, share // max(1, len(g["posts"])))
    for i, p in enumerate(g["posts"], 1):
        text = body_of(p)
        transcript = p.transcript
        if len(text) + len(transcript) > per_post:
            # Keep proportionally more of whichever is longer, never dropping either completely.
            t_share = int(per_post * len(transcript) / (len(text) + len(transcript))) if transcript else 0
            text = text[: per_post - t_share]
            transcript = transcript[:t_share]
        block = f"Source {i}: {p.source_name} ({KIND_LABEL.get(p.kind, p.kind)}) · {when(p)}\n{text}"
        if transcript:
            block += f"\nVideo transcript ({p.transcript_lang or 'original language'}): {transcript}"
        parts.append(block)
    return "\n\n".join(parts)


def _fallback_story(g: dict) -> dict:
    p = g["posts"][0]
    text = re.sub(r"\s+", " ", body_of(p)).strip()
    if len(text) > 700:
        text = text[:700].rsplit(" ", 1)[0] + "…"
    return {"headline": p.title[:160], "text": text, "bullets": [],
            "notes": ["Not shortened or translated: the AI writer was unavailable for this story."]}


def write_stories(ai: TextAI, groups: list[dict], s: dict, warnings: list[str]) -> list[dict]:
    style = s.get("item_style") if s.get("item_style") in prompts.BODY_RULES else "paragraph"
    body_rule, body_json = prompts.BODY_RULES[style]
    system = prompts.WRITE_SYSTEM.format(language=s.get("language") or "English", body_rule=body_rule, body_json=body_json,
                                         words=int(s.get("words_per_story") or 70),
                                         user_prompt=(s.get("writing_prompt") or "").strip() or "(none)")
    per_call = int(s.get("stories_per_call") or 8)
    written: dict[str, dict] = {}
    ids = [f"S{i + 1}" for i in range(len(groups))]

    def chunks(todo: list[int]):
        cur, size = [], 0
        for i in todo:
            g = groups[i]
            need = min(WRITE_CHAR_BUDGET, sum(len(body_of(p)) + len(p.transcript) + 120 for p in g["posts"]))
            if cur and (len(cur) >= per_call or size + need > WRITE_CHAR_BUDGET):
                yield cur
                cur, size = [], 0
            cur.append(i)
            size += need
        if cur:
            yield cur

    def run(todo: list[int]):
        for chunk in chunks(todo):
            share = max(1500, WRITE_CHAR_BUDGET // len(chunk))
            user = "\n\n".join(_story_material(ids[i], groups[i], share) for i in chunk)

            def check(data):
                if not isinstance(data, dict) or not isinstance(data.get("stories"), list):
                    raise ValueError("answer has no stories list")

            try:
                data = ai.json(system, user, check=check)
            except Exception as exc:  # noqa: BLE001
                log.warning("writing failed for %d stories: %s", len(chunk), exc)
                continue
            for st in data["stories"]:
                if not isinstance(st, dict):
                    continue
                sid = str(st.get("id", "")).strip()
                if sid in ids and sid not in written and (st.get("text") or st.get("bullets")):
                    bullets = [str(b).strip() for b in st.get("bullets") or [] if str(b).strip()] if isinstance(st.get("bullets"), list) else []
                    written[sid] = {"headline": str(st.get("headline") or "").strip(), "text": str(st.get("text") or "").strip(),
                                    "bullets": bullets, "notes": []}

    run(list(range(len(groups))))
    missing = [i for i, sid in enumerate(ids) if sid not in written]
    if missing:
        log.info("retrying %d stories the AI skipped", len(missing))
        run(missing)
    out = []
    fallbacks = 0
    for i, g in enumerate(groups):
        st = written.get(ids[i])
        if not st:
            st = _fallback_story(g)
            fallbacks += 1
        if not st["headline"]:
            st["headline"] = g["posts"][0].title[:160]
        out.append(st)
    if fallbacks:
        warnings.append(f"{fallbacks} stories are shown in their original wording because the free AI limit was reached.")
    return out


# ---------- assemble ----------

def build_digest(ai: TextAI, posts: list[Post], cfg: dict, warnings: list[str]) -> dict:
    s = cfg["settings"]
    topics = cfg.get("topics", [])
    other = s.get("other_topic_name") or "Other News"
    data = {"title": s.get("digest_title") or "News Digest", "generated_at": datetime.now(timezone.utc).isoformat(),
            "window_start": cfg["digest"]["window_start"], "window_end": cfg["digest"]["window_end"], "topics": [],
            "warnings": warnings}
    if not posts:
        return data
    posts = sorted(posts, key=lambda p: p.posted_at.timestamp() if p.posted_at else 0)
    groups = sort_posts(ai, posts, topics, s, warnings)
    log.info("%d posts grouped into %d stories", len(posts), len(groups))
    stories = write_stories(ai, groups, s, warnings)

    by_topic: dict[str, list[dict]] = {}
    for g, st in zip(groups, stories):
        sources, seen_src, notes = [], set(), list(st.get("notes", []))
        for p in sorted(g["posts"], key=lambda p: p.posted_at.timestamp() if p.posted_at else 0, reverse=True):
            key = (p.source_name, p.url)
            if key not in seen_src:
                seen_src.add(key)
                sources.append({"name": p.source_name, "kind": p.kind, "url": p.url,
                                "posted_at": p.posted_at.isoformat() if p.posted_at else None})
            for n in p.notes:
                note = f"{p.source_name}: {n}"
                if note not in notes:
                    notes.append(note)
        times = [p.posted_at for p in g["posts"] if p.posted_at]
        st.update({"sources": sources, "latest_at": max(times).isoformat() if times else None, "notes": notes,
                   "_outlets": len({p.source_name for p in g["posts"]}), "_ts": max(times).timestamp() if times else 0})
        by_topic.setdefault(g["topic"], []).append(st)

    order = [t["name"] for t in topics] + [other]
    for name in order:
        stories_here = by_topic.get(name)
        if not stories_here:
            continue
        if s.get("story_order") == "newest":
            stories_here.sort(key=lambda x: -x["_ts"])
        else:
            stories_here.sort(key=lambda x: (-x["_outlets"], -x["_ts"]))
        for st in stories_here:
            st.pop("_outlets", None)
            st.pop("_ts", None)
        emoji = next((t.get("emoji", "") for t in topics if t["name"] == name), "")
        data["topics"].append({"name": name, "emoji": emoji, "stories": stories_here})
    return data
