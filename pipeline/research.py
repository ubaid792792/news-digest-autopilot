"""Research prompts: the editor's requests become Google News searches over the digest window; the AI
keeps the matching articles, which are read and added to the digest like any other post."""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import feedparser
import requests

from . import prompts
from .ai import TextAI
from .digest import is_similar, title_key
from .sources import UA, Post, clean_text, fetch_article_text

log = logging.getLogger("research")

GOOGLE_NEWS = "https://news.google.com/rss/search"
CANDIDATES_PER_REQUEST = 40


def _search(query: str, hours: int, edition: str) -> list[dict]:
    country, lang = (edition or "PK:en").split(":")
    span = f"when:{hours}h" if hours <= 48 else f"when:{math.ceil(hours / 24)}d"
    for attempt in range(4):
        r = requests.get(GOOGLE_NEWS, timeout=30, headers={"User-Agent": UA},
                         params={"q": f"{query} {span}", "hl": f"{lang}-{country}", "gl": country, "ceid": f"{country}:{lang}"})
        if r.status_code in (429, 500, 502, 503) and attempt < 3:
            time.sleep(4 * (attempt + 1))  # Google News throttles busy servers; back off and retry
            continue
        break
    r.raise_for_status()
    out = []
    for e in feedparser.parse(r.content).entries:
        outlet = (e.get("source") or {}).get("title") or ""
        title = clean_text(e.get("title", ""))
        if outlet:
            title = re.sub(rf"\s+-\s+{re.escape(outlet)}$", "", title)
        stamp = e.get("published_parsed")
        posted = datetime(*stamp[:6], tzinfo=timezone.utc) if stamp else None
        out.append({"title": title, "outlet": outlet or "Google News", "link": e.get("link", ""), "posted": posted})
    return out


def _resolve(url: str) -> str:
    """news.google.com/rss/articles/... link -> the publisher's article URL."""
    from urllib.parse import quote
    if "news.google.com" not in url or "/articles/" not in url:
        return url
    try:
        gid = url.split("/articles/")[1].split("?")[0]
        page = requests.get(f"https://news.google.com/rss/articles/{gid}", timeout=20, headers={"User-Agent": UA}).text
        sig = re.search(r'data-n-a-sg="([^"]+)"', page)
        ts = re.search(r'data-n-a-ts="([^"]+)"', page)
        if not (sig and ts):
            return url
        req = [[["Fbv4je", json.dumps(["garturlreq", [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1, None, None,
                 None, None, None, 0, 1], "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0], gid, int(ts.group(1)),
                 sig.group(1)]), None, "generic"]]]
        r = requests.post("https://news.google.com/_/DotsSplashUi/data/batchexecute", timeout=20,
                          headers={"User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
                          data="f.req=" + quote(json.dumps(req)))
        part = r.text.split("\n\n")[1]
        return json.loads(json.loads(part)[0][2])[1]
    except Exception as exc:  # noqa: BLE001
        log.info("google news link not decoded: %s", exc)
        return url


def _queries(ai: TextAI, reqs: list[dict]) -> dict[int, list[str]]:
    fallback = {r["id"]: [re.sub(r"\s+", " ", r["prompt"])[:90]] for r in reqs}
    user = "\n".join(f"Request {r['id']}: {r['prompt']}" for r in reqs)

    def check(data):
        if not isinstance(data, dict) or not isinstance(data.get("requests"), list):
            raise ValueError("answer has no requests list")

    try:
        data = ai.json(prompts.QUERY_SYSTEM.format(), user, check=check)
    except Exception as exc:  # noqa: BLE001
        log.warning("search queries not generated, using the prompts as they are: %s", exc)
        return fallback
    out = dict(fallback)
    for item in data["requests"]:
        try:
            rid = int(item.get("id"))
        except (TypeError, ValueError, AttributeError):
            continue
        qs = [str(q).strip()[:120] for q in item.get("queries") or [] if str(q).strip()][:3]
        if rid in out and qs:
            out[rid] = qs
    return out


def _match(ai: TextAI, reqs: list[dict], cands: dict[int, list[dict]]) -> dict[int, list[int]]:
    blocks = []
    for r in reqs:
        lines = [f"  [{n}] {c['title']} ({c['outlet']}, {c['posted'].strftime('%d %b %H:%M UTC') if c['posted'] else 'time unknown'})"
                 for n, c in enumerate(cands[r["id"]], 1)]
        if lines:
            blocks.append(f"Request {r['id']}: {r['prompt']}\n" + "\n".join(lines))
    fallback = {r["id"]: list(range(1, len(cands[r["id"]]) + 1)) for r in reqs}
    if not blocks:
        return fallback

    def check(data):
        if not isinstance(data, dict) or not isinstance(data.get("requests"), list):
            raise ValueError("answer has no requests list")

    try:
        data = ai.json(prompts.MATCH_SYSTEM.format(), "\n\n".join(blocks), check=check)
    except Exception as exc:  # noqa: BLE001
        log.warning("search results not checked by AI, keeping the top results: %s", exc)
        return fallback
    out = {r["id"]: [] for r in reqs}
    for item in data["requests"]:
        try:
            rid = int(item.get("id"))
        except (TypeError, ValueError, AttributeError):
            continue
        if rid in out:
            for n in item.get("picks") or []:
                try:
                    n = int(n)
                except (TypeError, ValueError):
                    continue
                if 1 <= n <= len(cands[rid]) and n not in out[rid]:
                    out[rid].append(n)
    return out


def run_research(ai: TextAI, reqs: list[dict], since: datetime, s: dict, seen: set[str]) -> tuple[list[Post], list[dict]]:
    if not reqs:
        return [], []
    hours = max(1, math.ceil((datetime.now(timezone.utc) - since).total_seconds() / 3600))
    edition = s.get("research_edition") or "PK:en"
    queries = _queries(ai, reqs)
    status = {r["id"]: {"id": r["id"], "queries": queries[r["id"]], "count": 0, "error": None} for r in reqs}

    cands: dict[int, list[dict]] = {}
    for r in reqs:
        found, keys = [], []
        for q in queries[r["id"]]:
            try:
                results = _search(q, hours, edition)
            except Exception as exc:  # noqa: BLE001
                status[r["id"]]["error"] = f"Google News search failed: {str(exc)[:150]}"
                continue
            for c in results:
                if c["posted"] and c["posted"] < since:
                    continue
                c["id"] = "gn:" + hashlib.sha1(c["link"].encode()).hexdigest()[:20]
                key = title_key(c["title"])
                if c["id"] in seen or any(is_similar(key, k) for k in keys):
                    continue
                keys.append(key)
                found.append(c)
        cands[r["id"]] = found[:CANDIDATES_PER_REQUEST]
        log.info("research %d: %d search results for %s", r["id"], len(cands[r["id"]]), queries[r["id"]])

    picks = _match(ai, reqs, cands)
    chosen: list[tuple[dict, dict]] = []
    for r in reqs:
        for n in picks[r["id"]][: int(r.get("max_items") or 8)]:
            chosen.append((r, cands[r["id"]][n - 1]))

    def read(pair):
        r, c = pair
        url = _resolve(c["link"])
        return r, c, url, fetch_article_text(url) if url != c["link"] else ""

    posts: list[Post] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for r, c, url, text in pool.map(read, chosen):
            notes = [] if len(text) > 200 else ["Only the headline was available for this search result."]
            posts.append(Post(id=c["id"], source_id=0, source_name=c["outlet"], kind="research", url=url,
                              posted_at=c["posted"], title=c["title"], text=text or c["title"], notes=notes,
                              topic_hint=r.get("topic") or ""))
            status[r["id"]]["count"] += 1
    return posts, list(status.values())
