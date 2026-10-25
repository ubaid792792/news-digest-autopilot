"""Collect posts from RSS feeds (incl. FetchRSS Facebook feeds), X profiles and Instagram profiles."""
from __future__ import annotations

import asyncio
import calendar
import hashlib
import html
import logging
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import feedparser
import requests
from bs4 import BeautifulSoup

from .worker_api import WorkerAPI

log = logging.getLogger("sources")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
BOILERPLATE = re.compile(r"cookie|subscribe|get the latest|follow us|google news|whatsapp channel|"
                         r"click here|all rights reserved|copyrighted|may not be published", re.I)


@dataclass
class Post:
    id: str
    source_id: int
    source_name: str
    kind: str
    url: str
    posted_at: datetime | None
    title: str
    text: str
    video_url: str | None = None
    video_seconds: float | None = None
    transcript: str = ""
    transcript_lang: str = ""
    notes: list[str] = field(default_factory=list)


def clean_text(value: str) -> str:
    text = BeautifulSoup(html.unescape(value or ""), "lxml").get_text(" ")
    return re.sub(r"\s+", " ", text).strip()


def first_sentence(text: str, limit: int = 160) -> str:
    return re.split(r"(?<=[.!?۔])\s", text.strip(), maxsplit=1)[0][:limit]


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        pass
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S%z").astimezone(timezone.utc)
    except ValueError:
        return None


# ---------- RSS ----------

def fetch_rss(src: dict, limit: int) -> list[Post]:
    r = requests.get(src["url"], timeout=30, headers={"User-Agent": UA, "Accept": "application/rss+xml, application/xml, */*"})
    r.raise_for_status()
    parsed = feedparser.parse(r.content)
    if parsed.bozo and not parsed.entries:
        raise RuntimeError(f"invalid feed: {parsed.bozo_exception}"[:200])
    name = src.get("name") or clean_text(parsed.feed.get("title", "")) or src["url"]
    posts = []
    for e in parsed.entries[:limit]:
        link = e.get("link", "")
        title = clean_text(e.get("title", ""))
        content = ""
        if e.get("content"):
            content = clean_text(" ".join(c.get("value", "") for c in e["content"]))
        summary = clean_text(e.get("summary", "") or e.get("description", ""))
        text = content if len(content) > len(summary) else summary
        if not (title or text):
            continue
        stamp = e.get("published_parsed") or e.get("updated_parsed")
        posted = datetime.fromtimestamp(calendar.timegm(stamp), tz=timezone.utc) if stamp else None
        guid = e.get("id") or link or title
        posts.append(Post(id="rss:" + hashlib.sha1(f"{src['url']}|{guid}".encode()).hexdigest()[:20], source_id=src["id"],
                          source_name=name, kind=src["kind"], url=link, posted_at=posted,
                          title=title or first_sentence(text), text=text[:4000]))
    return posts


def fetch_article_text(url: str, limit: int = 4000) -> str:
    try:
        r = requests.get(url, timeout=25, headers={"User-Agent": UA})
        r.raise_for_status()
    except requests.RequestException as exc:
        log.info("article fetch failed (%s): %s", url, exc)
        return ""
    soup = BeautifulSoup(r.text, "lxml")
    for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form", "figure", "noscript"]):
        tag.decompose()
    # The story body is the element whose own <p> children hold the most text.
    best, best_len = None, 0
    for el in soup.find_all(["article", "div", "section", "main"]):
        own = [p.get_text(" ", strip=True) for p in el.find_all("p", recursive=False)]
        size = sum(len(t) for t in own if len(t) > 40)
        if size > best_len:
            best, best_len = el, size
    root = best if best_len > 300 else (soup.find("article") or soup.body or soup)
    paras = [re.sub(r"\s+", " ", p.get_text(" ")).strip() for p in root.find_all("p", recursive=root is not best)]
    paras = [p for p in paras if len(p) > 60 and not BOILERPLATE.search(p)]
    return "\n".join(paras)[:limit]


# ---------- Instagram (Business Discovery through the Worker) ----------

def fetch_instagram(src: dict, worker: WorkerAPI, since: datetime, limit: int) -> list[Post]:
    handle = src.get("handle") or src["url"].rstrip("/").split("/")[-1]
    posts, after = [], ""
    for _page in range(4):
        res = worker.instagram_media(handle, limit=min(50, limit), after=after)
        media = res.get("media", [])
        older = False
        for m in media:
            posted = parse_time(m.get("timestamp"))
            if posted and posted < since:
                older = True
                continue
            caption = re.sub(r"[ \t]+", " ", m.get("caption") or "").strip()
            video_url = None
            notes = []
            mtype = m.get("media_type")
            if mtype == "VIDEO":
                video_url = m.get("media_url")
            elif mtype == "CAROUSEL_ALBUM":
                kids = (m.get("children") or {}).get("data", [])
                vids = [k for k in kids if k.get("media_type") == "VIDEO"]
                video_url = next((k.get("media_url") for k in vids if k.get("media_url")), None)
                if vids and not video_url:
                    mtype = "VIDEO"
            if mtype == "VIDEO" and not video_url:
                notes.append("Video not transcribed (Instagram hides this video); caption only.")
            if not caption and not video_url:
                continue
            posts.append(Post(id=f"ig:{m.get('id')}", source_id=src["id"], source_name=src.get("name") or f"@{handle}",
                              kind="instagram", url=m.get("permalink") or src["url"], posted_at=posted,
                              title=first_sentence(caption) if caption else "Instagram video", text=caption[:4000],
                              video_url=video_url, notes=notes))
        after = res.get("after") or ""
        if older or not after or not media or len(posts) >= limit:
            break
    return posts[:limit]


# ---------- X (burner-account cookies, twscrape) ----------

class XLoginError(RuntimeError):
    pass


def _best_video(media) -> tuple[str | None, float | None]:
    for v in getattr(media, "videos", None) or []:
        mp4 = [x for x in (v.variants or []) if "mp4" in (x.contentType or "")]
        if not mp4:
            continue
        # The smallest mp4 is plenty for speech and fastest to download.
        pick = min(mp4, key=lambda x: x.bitrate or 0)
        seconds = (v.duration or 0) / 1000 if v.duration else None
        return pick.url, seconds
    return None, None


def _tweet_text(t) -> str:
    text = t.rawContent or ""
    q = getattr(t, "quotedTweet", None)
    if q and q.rawContent:
        text += f"\n\n[Quoting @{q.user.username}: {q.rawContent}]"
    return text.strip()


async def _fetch_x(sources: list[dict], cookies: dict, since: datetime, limit: int, skip_retweets: bool,
                   include_replies: bool) -> tuple[list[Post], dict[int, str]]:
    from twscrape import API

    db = Path(tempfile.mkdtemp()) / "accounts.db"
    # Rate limits: wait up to 10 minutes for the account to free up; a dead login raises at once.
    api = API(str(db), raise_when_no_account=True, wait_timeout=600)
    cookie = f"auth_token={cookies['auth_token']}; ct0={cookies['ct0']}"
    await api.pool.add_account("digest_burner", "unused", "unused@example.com", "unused", cookies=cookie)
    posts: list[Post] = []
    errors: dict[int, str] = {}
    for src in sources:
        handle = src.get("handle") or src["url"].rstrip("/").split("/")[-1]
        try:
            user = await asyncio.wait_for(api.user_by_login(handle), 90)
            if not user:
                errors[src["id"]] = f"X account @{handle} not found"
                continue
            older = 0
            count = 0
            timeline = api.user_tweets_and_replies(user.id, limit=limit * 2) if include_replies else api.user_tweets(user.id, limit=limit * 2)
            async for t in timeline:
                posted = t.date.astimezone(timezone.utc) if t.date else None
                if posted and posted < since:
                    older += 1  # a pinned post can be old; stop after a few old ones in a row
                    if older >= 4:
                        break
                    continue
                older = 0
                if t.user and t.user.id != user.id:
                    continue
                if skip_retweets and t.retweetedTweet:
                    continue
                if not include_replies and t.inReplyToTweetId and t.inReplyToUser and t.inReplyToUser.id != user.id:
                    continue
                video_url, seconds = _best_video(t.media)
                text = _tweet_text(t)
                text = re.sub(r"\s*https://t\.co/\w+\s*$", "", text).strip() or text
                posts.append(Post(id=f"x:{t.id}", source_id=src["id"], source_name=src.get("name") or f"@{handle}",
                                  kind="x", url=t.url, posted_at=posted, title=first_sentence(text) or "X video",
                                  text=text[:4000], video_url=video_url, video_seconds=seconds))
                count += 1
                if count >= limit:
                    break
        except Exception as exc:  # noqa: BLE001
            msg = str(exc) or exc.__class__.__name__
            if "No account available" in msg or "NoAccount" in exc.__class__.__name__:
                accounts = await api.pool.get_all()
                if not any(a.active for a in accounts):
                    raise XLoginError("X refused the saved cookies (logged out or locked). Save fresh cookies in Connections.") from exc
                # Still rate-limited after waiting: keep what was read, skip the rest this run.
                for rest in sources[sources.index(src):]:
                    errors[rest["id"]] = "X rate limit reached; this profile is read in the next run"
                break
            errors[src["id"]] = f"X @{handle}: {msg[:200]}"
    return posts, errors


def fetch_x(sources: list[dict], cookies: dict | None, since: datetime, s: dict) -> tuple[list[Post], dict[int, str], dict]:
    """Returns posts, per-source errors and an overall status for the Connections tab."""
    if not sources:
        return [], {}, None
    if not cookies:
        msg = "X cookies are not saved (Connections tab); X profiles were skipped."
        return [], {src["id"]: msg for src in sources}, {"ok": False, "message": msg}
    limit = int(s.get("max_posts_per_source") or 60)
    try:
        posts, errors = asyncio.run(asyncio.wait_for(
            _fetch_x(sources, cookies, since, limit, bool(s.get("skip_retweets", True)), bool(s.get("include_replies"))),
            timeout=900))
    except XLoginError as exc:
        return [], {src["id"]: str(exc) for src in sources}, {"ok": False, "message": str(exc)}
    except Exception as exc:  # noqa: BLE001
        msg = f"X reading failed: {str(exc)[:200] or exc.__class__.__name__}"
        return [], {src["id"]: msg for src in sources}, {"ok": False, "message": msg}
    ok_sources = len(sources) - len(errors)
    status = {"ok": ok_sources > 0, "message": f"Read {ok_sources} of {len(sources)} X profiles ({len(posts)} posts)."}
    return posts, errors, status
