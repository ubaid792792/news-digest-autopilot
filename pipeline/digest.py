"""Turn collected posts into the digest, in small checked steps so a weak free model can't scramble it:
1. classify each post under one of the user's headings or "none" (a heading's must-mention keywords
   decide which headings a post may go under at all),
2. group posts about the same event inside each heading,
3. write each story short in the chosen language, then check it against its own sources (numbers must
   come from the sources, and the model must name the story's first source) before accepting it.
Posts under no heading become a one-line "other news" list (or are dropped, per settings)."""
from __future__ import annotations

import difflib
import logging
import re
from datetime import datetime, timezone

from . import prompts
from .ai import TextAI
from .sources import Post

log = logging.getLogger("digest")

CLASSIFY_BATCH = 30
GROUP_BATCH = 100
WRITE_CHAR_BUDGET = 9000
MAX_POSTS_PER_STORY = 6
POST_CHARS = 1500
KIND_LABEL = {"rss": "website", "facebook": "Facebook", "x": "X", "instagram": "Instagram", "research": "web search"}
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
    """Post text with the title in front unless the text already starts with it."""
    text = (p.text or "").strip()
    if not p.title or text.startswith(p.title[:60]):
        return text
    return f"{p.title}\n{text}" if p.title not in text else text


def snippet(p: Post, n: int) -> str:
    return re.sub(r"\s+", " ", f"{p.text} {p.transcript}".replace(p.title, "", 1)).strip()[:n]


# ---------- step 1: headings ----------

def keyword_groups(t: dict) -> list[list[str]]:
    """'Iran, Tehran + Israel, IDF' -> [["iran", "tehran"], ["israel", "idf"]]: a post must mention a word
    from every group (so a war heading can require both sides)."""
    groups = []
    for part in (t.get("keywords") or "").split("+"):
        words = [k.strip().lower() for k in re.split(r"[,;\n]", part) if k.strip()]
        if words:
            groups.append(words)
    return groups


def keyword_hits(text: str, kws: list[str]) -> int:
    return sum(1 for k in kws if re.search(rf"(?<!\w){re.escape(k)}(?!\w)", text))


def options_for(p: Post, topics: list[dict]) -> list[tuple[str, int]]:
    """Headings this post may go under: those whose keyword groups it all mentions, plus headings without keywords."""
    text = f"{p.title} {p.text} {p.transcript}".lower()
    out = []
    for t in topics:
        groups = keyword_groups(t)
        if not groups:
            out.append((t["name"], 0))
            continue
        hits = [keyword_hits(text, g) for g in groups]
        if all(hits):
            out.append((t["name"], sum(hits)))
    return out


def _fallback_heading(opts: list[tuple[str, int]]) -> str | None:
    """Without an AI answer: the single matching heading, or a clear keyword winner; otherwise none."""
    keyed = sorted((o for o in opts if o[1] > 0), key=lambda o: -o[1])
    if len(keyed) == 1:
        return keyed[0][0]
    if len(keyed) > 1 and keyed[0][1] >= keyed[1][1] + 2:
        return keyed[0][0]
    return None


def classify(ai: TextAI, posts: list[Post], topics: list[dict], s: dict, warnings: list[str]) -> dict[str, str | None]:
    heading: dict[str, str | None] = {}
    todo = []
    for p in posts:
        opts = options_for(p, topics)
        if not opts:
            heading[p.id] = None
        else:
            todo.append((p, opts))
    log.info("%d posts mention a heading's keywords, %d mention none", len(todo), len(posts) - len(todo))
    extra = (s.get("sort_prompt") or "").strip()
    system = prompts.CLASSIFY_SYSTEM.format(topics=prompts.topics_text(topics),
                                            extra=f"\nExtra instructions from the editor:\n{extra}\n" if extra else "")
    failed = 0
    for start in range(0, len(todo), CLASSIFY_BATCH):
        batch = todo[start:start + CLASSIFY_BATCH]
        lines = []
        for n, (p, opts) in enumerate(batch, 1):
            choices = " | ".join([o[0] for o in opts] + ["none"])
            hint = f" · found by a search for news on {p.topic_hint}" if p.topic_hint else ""
            lines.append(f"[{n}] options: {choices}\n    {p.source_name}{hint} · {when(p)} · {p.title} — {snippet(p, 300)}")

        def check(data):
            if not isinstance(data, dict) or not isinstance(data.get("posts"), list):
                raise ValueError("answer has no posts list")

        answers: dict[int, str] = {}
        try:
            data = ai.json(system, "Posts:\n" + "\n".join(lines), check=check)
            for a in data["posts"]:
                if isinstance(a, dict):
                    try:
                        answers[int(a.get("n"))] = str(a.get("heading") or "").strip()
                    except (TypeError, ValueError):
                        continue
        except Exception as exc:  # noqa: BLE001
            log.warning("classification failed for %d posts: %s", len(batch), exc)
            failed += len(batch)
        for n, (p, opts) in enumerate(batch, 1):
            allowed = {o[0].lower(): o[0] for o in opts}
            ans = answers.get(n)
            if ans is None:
                heading[p.id] = _fallback_heading(opts)
            else:
                heading[p.id] = allowed.get(ans.lower())  # anything outside the post's options counts as none
    if failed:
        warnings.append(f"AI sorting was unavailable for {failed} posts; they were sorted by heading keywords only.")
    return heading


# ---------- step 2: same-event groups ----------

def _similar_groups(posts: list[Post]) -> list[list[Post]]:
    groups: list[list[Post]] = []
    for p in posts:
        key = title_key(p.title)
        match = next((g for g in groups if is_similar(key, title_key(g[0].title))), None)
        (match.append(p) if match else groups.append([p]))
    return groups


def group_events(ai: TextAI, by_heading: dict[str, list[Post]]) -> list[tuple[str, list[Post]]]:
    """Same-event groups. Posts filed under neighbouring headings (e.g. two Iran headings) can join one
    group, which then goes under the heading most of its posts were filed under."""
    order = list(by_heading)
    out: list[tuple[str, list[Post]]] = []
    sections = [(h, ps) for h, ps in by_heading.items() if ps]
    calls: list[list[tuple[str, list[Post]]]] = []
    cur, size = [], 0
    for h, ps in sections:
        if cur and size + len(ps) > GROUP_BATCH:
            calls.append(cur)
            cur, size = [], 0
        cur.append((h, ps))
        size += len(ps)
    if cur:
        calls.append(cur)

    for call in calls:
        numbered: list[tuple[str, Post]] = []
        parts = []
        for h, ps in call:
            parts.append(f"## Section: {h}")
            for p in ps:
                numbered.append((h, p))
                parts.append(f"[{len(numbered)}] {p.source_name} · {when(p)} · {p.title} — {snippet(p, 160)}")
        if len(numbered) == 1:
            out.append((numbered[0][0], [numbered[0][1]]))
            continue

        def check(data):
            if not isinstance(data, dict) or not isinstance(data.get("groups"), list):
                raise ValueError("answer has no groups list")

        try:
            data = ai.json(prompts.GROUP_SYSTEM, "\n".join(parts), check=check)
        except Exception as exc:  # noqa: BLE001
            log.warning("grouping failed, using headline similarity: %s", exc)
            for h, ps in call:
                out.extend((h, g) for g in _similar_groups(ps))
            continue
        # A weak model sometimes lumps a whole list into one "group"; then trust headline similarity instead.
        biggest = max((len(g) for g in data["groups"] if isinstance(g, list)), default=0)
        if len(numbered) > 10 and biggest > max(6, 0.4 * len(numbered)):
            log.warning("grouping answer lumped %d of %d posts together; using headline similarity", biggest, len(numbered))
            for h, ps in call:
                out.extend((h, g) for g in _similar_groups(ps))
            continue
        used: set[int] = set()
        for g in data["groups"]:
            members = []
            for n in g if isinstance(g, list) else []:
                try:
                    n = int(n)
                except (TypeError, ValueError):
                    continue
                if 1 <= n <= len(numbered) and n not in used:
                    used.add(n)
                    members.append(numbered[n - 1])
            if members:
                counts = {h: sum(1 for m in members if m[0] == h) for h in dict.fromkeys(m[0] for m in members)}
                best = max(counts, key=lambda h: (counts[h], -order.index(h)))
                out.append((best, [m[1] for m in members]))
        for n, (h, p) in enumerate(numbered, 1):
            if n not in used:
                out.append((h, [p]))
    return out


# ---------- step 3: write and check ----------

COMMON_CAPS = set("""Monday Tuesday Wednesday Thursday Friday Saturday Sunday January February March April June July
August September October November December Source Sources According Also Meanwhile However Government Minister
President Prime Officials Separately Both They This That These Their There Earlier Later After Before While During
Following Despite Under Over Since Until Some Many Most Other Others Previously Currently Overall""".split())
NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _numbers(text: str) -> set[str]:
    out = set()
    for m in NUM_RE.findall(text or ""):
        n = m.replace(",", "").rstrip(".")
        if "." in n:
            n = n.rstrip("0").rstrip(".")
        out.add(n)
    return out


def _story_posts(posts: list[Post]) -> list[Post]:
    """Up to MAX_POSTS_PER_STORY posts, richest first and one per outlet before repeating outlets."""
    ranked = sorted(posts, key=lambda p: -(len(p.text) + len(p.transcript)))
    picked, outlets = [], set()
    for p in ranked:
        if p.source_name not in outlets:
            picked.append(p)
            outlets.add(p.source_name)
    for p in ranked:
        if p not in picked:
            picked.append(p)
    return picked[:MAX_POSTS_PER_STORY]


def _material(sid: str, heading: str, posts: list[Post]) -> str:
    parts = [f"### Story {sid} (heading: {heading})"]
    for i, p in enumerate(_story_posts(posts), 1):
        block = f"Source {i}: {p.source_name} ({KIND_LABEL.get(p.kind, p.kind)}) · {when(p)}\nTitle: {p.title}\n{body_of(p)[:POST_CHARS]}"
        if p.transcript:
            block += f"\nVideo transcript ({p.transcript_lang or 'original language'}): {p.transcript[:POST_CHARS]}"
        parts.append(block)
    return "\n\n".join(parts)


def _check(st: dict, posts: list[Post]) -> str | None:
    """Why a written story can't be trusted, or None when it passes."""
    used = _story_posts(posts)
    out = " ".join([st["headline"], st["text"], *st["bullets"]])
    src = " ".join(f"{p.title} {p.text} {p.transcript}" for p in used)
    invented = [n for n in _numbers(out) if len(n) >= 3 and n not in _numbers(src)]
    if invented:
        return f"numbers not in its sources: {', '.join(sorted(invented)[:4])}"
    echoed = st.get("source1_title", "")
    if echoed and difflib.SequenceMatcher(None, echoed.lower(), used[0].title.lower()).ratio() < 0.6:
        return "the AI described a different story"
    latin = sum(c.isascii() for c in src) / max(1, len(src))
    if latin > 0.8:
        # Proper names must come from the sources (catches misspelt or invented names). A shared 5-letter
        # prefix is enough, so "Pakistani" passes when the source says "Pakistan's".
        low = src.lower()
        odd = [w for w in set(re.findall(r"\b[A-Z][a-z]{3,}\b", out))
               if w not in COMMON_CAPS and w[:5].lower() not in low]
        if odd:
            return f"names not in its sources: {', '.join(sorted(odd)[:4])}"
        words = {w for w in re.findall(r"[a-z]{5,}", out.lower())}
        if words:
            hit = sum(1 for w in words if w in src.lower())
            if hit / len(words) < 0.45:
                return "the summary does not match its sources"
    return None


def _extractive(posts: list[Post]) -> dict:
    """Original wording from the first source, so facts stay exact when the AI result can't be trusted."""
    p = _story_posts(posts)[0]
    sentences = re.split(r"(?<=[.!?۔])\s+", re.sub(r"\s+", " ", p.text or "").strip())
    bullets = []
    for snt in sentences:
        if snt and snt != p.title and len(" ".join(bullets + [snt]).split()) <= 70:
            bullets.append(snt)
        if len(bullets) >= 3:
            break
    return {"headline": p.title[:160], "text": "", "bullets": bullets or [p.title],
            "notes": ["Shown in the source's own words (the AI summary did not pass the accuracy check)."]}


def write_stories(ai: TextAI, groups: list[tuple[str, list[Post]]], s: dict, warnings: list[str]) -> list[dict]:
    style = s.get("item_style") if s.get("item_style") in prompts.BODY_RULES else "bullets"
    body_rule, body_json = prompts.BODY_RULES[style]
    system = prompts.WRITE_SYSTEM.format(language=s.get("language") or "English", body_rule=body_rule, body_json=body_json,
                                         words=int(s.get("words_per_story") or 35),
                                         user_prompt=(s.get("writing_prompt") or "").strip() or "(none)")
    per_call = max(1, min(6, int(s.get("stories_per_call") or 4)))
    ids = [f"S{i + 1}" for i in range(len(groups))]
    written: dict[str, dict] = {}
    rejected: dict[str, str] = {}

    def run(todo: list[int], size: int) -> None:
        chunk, chars = [], 0
        batches = []
        for i in todo:
            need = len(_material(ids[i], *groups[i]))
            if chunk and (len(chunk) >= size or chars + need > WRITE_CHAR_BUDGET):
                batches.append(chunk)
                chunk, chars = [], 0
            chunk.append(i)
            chars += need
        if chunk:
            batches.append(chunk)
        for b in batches:
            user = "\n\n".join(_material(ids[i], *groups[i]) for i in b)

            def check(data):
                if not isinstance(data, dict) or not isinstance(data.get("stories"), list):
                    raise ValueError("answer has no stories list")

            try:
                data = ai.json(system, user, check=check)
            except Exception as exc:  # noqa: BLE001
                log.warning("writing failed for %d stories: %s", len(b), exc)
                continue
            wanted = {ids[i]: i for i in b}
            for st in data["stories"]:
                if not isinstance(st, dict):
                    continue
                sid = str(st.get("id", "")).strip()
                if sid not in wanted or sid in written:
                    continue
                bullets = [str(x).strip() for x in st.get("bullets") or [] if str(x).strip()] if isinstance(st.get("bullets"), list) else []
                story = {"headline": str(st.get("headline") or "").strip(), "text": str(st.get("text") or "").strip(),
                         "bullets": bullets, "source1_title": str(st.get("source1_title") or "").strip(), "notes": []}
                if not (story["text"] or story["bullets"]):
                    continue
                why = _check(story, groups[wanted[sid]][1])
                if why:
                    rejected[sid] = why
                    log.info("story %s rejected: %s", sid, why)
                    continue
                story.pop("source1_title")
                written[sid] = story

    run(list(range(len(groups))), per_call)
    missing = [i for i, sid in enumerate(ids) if sid not in written]
    if missing:
        log.info("rewriting %d stories one at a time", len(missing))
        run(missing, 1)
    out, fallbacks = [], 0
    for i, (heading, posts) in enumerate(groups):
        st = written.get(ids[i])
        if not st:
            st = _extractive(posts)
            fallbacks += 1
        if not st["headline"]:
            st["headline"] = posts[0].title[:160]
        out.append(st)
    if fallbacks:
        warnings.append(f"{fallbacks} stories are shown in the source's own words because the AI summary failed the accuracy check or the free AI limit was reached.")
    return out


# ---------- assemble ----------

def build_digest(ai: TextAI, posts: list[Post], cfg: dict, warnings: list[str]) -> dict:
    s = cfg["settings"]
    topics = cfg.get("topics", [])
    other_name = s.get("other_topic_name") or "Other News"
    other_mode = s.get("other_news_mode") or "list"
    data = {"title": s.get("digest_title") or "News Digest", "generated_at": datetime.now(timezone.utc).isoformat(),
            "window_start": cfg["digest"]["window_start"], "window_end": cfg["digest"]["window_end"],
            "topics": [{"name": t["name"], "emoji": t.get("emoji", ""), "stories": []} for t in topics],
            "other": [], "other_name": other_name, "warnings": warnings}
    if not posts:
        return data
    posts = sorted(posts, key=lambda p: p.posted_at.timestamp() if p.posted_at else 0, reverse=True)
    heading = classify(ai, posts, topics, s, warnings)
    by_heading: dict[str, list[Post]] = {t["name"]: [] for t in topics}
    unsorted = []
    for p in posts:
        h = heading.get(p.id)
        (by_heading[h].append(p) if h in by_heading else unsorted.append(p))
    log.info("%d posts under headings, %d under none", len(posts) - len(unsorted), len(unsorted))
    if other_mode == "full" and unsorted:
        by_heading[other_name] = unsorted
        data["topics"].append({"name": other_name, "emoji": "", "stories": []})

    groups = group_events(ai, by_heading)
    log.info("%d stories after grouping", len(groups))
    stories = write_stories(ai, groups, s, warnings)

    slots = {t["name"]: t for t in data["topics"]}
    for (h, ps), st in zip(groups, stories):
        sources, seen_urls, notes = [], set(), list(st.get("notes", []))
        for p in sorted(ps, key=lambda p: p.posted_at.timestamp() if p.posted_at else 0, reverse=True):
            if p.url not in seen_urls:
                seen_urls.add(p.url)
                sources.append({"name": p.source_name, "kind": p.kind, "url": p.url,
                                "posted_at": p.posted_at.isoformat() if p.posted_at else None})
            for n in p.notes:
                note = f"{p.source_name}: {n}"
                if note not in notes:
                    notes.append(note)
        times = [p.posted_at for p in ps if p.posted_at]
        st.update({"sources": sources, "latest_at": max(times).isoformat() if times else None, "notes": notes,
                   "_outlets": len({p.source_name for p in ps}), "_ts": max(times).timestamp() if times else 0})
        slots[h]["stories"].append(st)

    for t in data["topics"]:
        if s.get("story_order") == "newest":
            t["stories"].sort(key=lambda x: -x["_ts"])
        else:
            t["stories"].sort(key=lambda x: (-x["_outlets"], -x["_ts"]))
        for st in t["stories"]:
            st.pop("_outlets", None)
            st.pop("_ts", None)

    if other_mode == "list":
        limit = int(s.get("other_news_limit") or 20)
        keys: list[str] = []
        for p in unsorted:
            k = title_key(p.title)
            if any(is_similar(k, x) for x in keys):
                continue
            keys.append(k)
            data["other"].append({"title": p.title, "source": p.source_name, "kind": p.kind, "url": p.url,
                                  "posted_at": p.posted_at.isoformat() if p.posted_at else None})
            if len(data["other"]) >= limit:
                break
    return data
