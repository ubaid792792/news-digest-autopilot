"""One collection run: read every source for the digest's window -> transcribe videos -> sort and write
the digest -> hand it to the Worker, which emails it at the delivery time.

Started by GitHub Actions (dispatched by the Worker before each delivery time, or from the dashboard).
Environment: WORKER_URL, PIPELINE_SECRET, GEMINI_API_KEY, MODE (live|preview), RUN_ID, DIGEST_ID, TRIGGER.
"""
from __future__ import annotations

import io
import logging
import os
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from .ai import TextAI
from .digest import build_digest
from .media import transcribe_posts
from .research import run_research
from .sources import Post, fetch_article_text, fetch_instagram, fetch_rss, fetch_x, parse_time
from .worker_api import WorkerAPI

LOG_BUFFER = io.StringIO()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout), logging.StreamHandler(LOG_BUFFER)])
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("faster_whisper").setLevel(logging.WARNING)
log = logging.getLogger("pipeline")


def mask(*values: str) -> None:
    """Hide secrets in the public GitHub Actions log."""
    if os.environ.get("GITHUB_ACTIONS"):
        for v in values:
            if v:
                print(f"::add-mask::{v}", flush=True)


def collect(cfg: dict, worker: WorkerAPI, since: datetime) -> tuple[list[Post], list[dict], list[str], dict | None]:
    s = cfg["settings"]
    seen = set(cfg.get("seen", []))
    limit = int(s.get("max_posts_per_source") or 60)
    sources = cfg.get("sources", [])
    status: dict[int, dict] = {}
    warnings: list[str] = []
    found: list[Post] = []

    def fresh(posts: list[Post]) -> list[Post]:
        # Undated posts count once: the "seen" list stops them coming back in later digests.
        return [p for p in posts if p.id not in seen and (p.posted_at is None or p.posted_at >= since)]

    def read(src: dict) -> tuple[dict, list[Post], str | None]:
        try:
            if src["kind"] == "instagram":
                return src, fetch_instagram(src, worker, since, limit), None
            return src, fetch_rss(src, limit), None
        except Exception as exc:  # noqa: BLE001
            return src, [], str(exc)[:240]

    others = [x for x in sources if x["kind"] != "x"]
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(read, src) for src in others]
        x_posts, x_errors, x_status = fetch_x([x for x in sources if x["kind"] == "x"], cfg.get("x_cookies"), since, s)
        results = [f.result() for f in futures]
    for src, posts, err in results:
        new = fresh(posts)
        found.extend(new)
        status[src["id"]] = {"id": src["id"], "error": err, "count": len(new)}
        log.info("%s %s: %d new of %d%s", src["kind"], src["name"], len(new), len(posts), f" (error: {err})" if err else "")
        if err:
            warnings.append(f"{src['name']}: {err}")
    new_x = fresh(x_posts)
    found.extend(new_x)
    for src in (x for x in sources if x["kind"] == "x"):
        n = sum(1 for p in new_x if p.source_id == src["id"])
        err = x_errors.get(src["id"])
        status[src["id"]] = {"id": src["id"], "error": err, "count": n}
        log.info("x %s: %d new%s", src["name"], n, f" (error: {err})" if err else "")
    if x_status and not x_status.get("ok"):
        warnings.append(x_status["message"])
    elif x_errors:
        warnings.extend(sorted(set(x_errors.values()))[:5])

    if s.get("fetch_full_articles"):
        short = [p for p in found if p.kind == "rss" and p.url and len(p.text) < 800][:60]
        with ThreadPoolExecutor(max_workers=8) as pool:
            for p, text in zip(short, pool.map(lambda p: fetch_article_text(p.url), short)):
                if len(text) > len(p.text):
                    p.text = text
    return found, list(status.values()), warnings, x_status


def x_test(worker: WorkerAPI, run_id: str, trigger: str, gh_url: str) -> int:
    """Check the saved X cookies against every X profile in Sources and report each one's status."""
    worker.start_run(run_id, "", "xtest", trigger, gh_url)
    try:
        cfg = worker.x_config()
        ck = cfg.get("x_cookies")
        if ck:
            mask(ck.get("auth_token", ""), ck.get("ct0", ""))
        sources = cfg.get("sources", [])
        since = datetime.now(timezone.utc) - timedelta(hours=24)
        s = {**cfg["settings"], "max_posts_per_source": 15}
        posts, errors, status = fetch_x(sources, ck, since, s)
        results = []
        for src in sources:
            n = sum(1 for p in posts if p.source_id == src["id"])
            err = errors.get(src["id"])
            results.append({"id": src["id"], "error": err, "count": n,
                            "message": err or (f"Working: {n} posts in the last 24 hours" if n else "Working: no posts in the last 24 hours")})
            log.info("x %s: %s", src["name"], results[-1]["message"])
        ok = sum(1 for r in results if not r["error"])
        if status is None:
            status = {"ok": None, "message": "No X profiles in Sources to test."}
        elif status.get("ok"):
            status = {"ok": True, "message": f"Working: {ok} of {len(results)} X profiles read fine."}
        worker.x_report({"x_status": status, "results": results})
        summary = status["message"]
        worker.finish_run(run_id, "done", summary, LOG_BUFFER.getvalue())
        return 0
    except Exception as exc:  # noqa: BLE001
        log.exception("X test failed")
        try:
            worker.x_report({"x_status": {"ok": False, "message": f"X test failed: {str(exc)[:200]}"}, "results": []})
            worker.finish_run(run_id, "error", str(exc)[:300], LOG_BUFFER.getvalue())
        except Exception:  # noqa: BLE001
            pass
        return 1


def main() -> int:
    mode = os.environ.get("MODE") or "preview"
    run_id = os.environ.get("RUN_ID") or uuid.uuid4().hex[:12]
    digest_id = os.environ.get("DIGEST_ID") or ""
    trigger = os.environ.get("TRIGGER") or "manual"
    worker = WorkerAPI(os.environ["WORKER_URL"], os.environ["PIPELINE_SECRET"])
    gh_url = ""
    if os.environ.get("GITHUB_RUN_ID"):
        gh_url = f"{os.environ['GITHUB_SERVER_URL']}/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    if mode == "xtest":
        return x_test(worker, run_id, trigger, gh_url)
    if not digest_id:
        digest_id = worker.new_preview_digest()
        mode = "preview"
    worker.start_run(run_id, digest_id, mode, trigger, gh_url)
    try:
        cfg = worker.config(digest_id)
        if cfg.get("x_cookies"):
            mask(cfg["x_cookies"].get("auth_token", ""), cfg["x_cookies"].get("ct0", ""))
        s = cfg["settings"]
        since = parse_time(cfg["digest"]["window_start"]) or datetime.now(timezone.utc) - timedelta(hours=24)
        log.info("digest %s (%s): posts since %s from %d sources", digest_id, mode, since.isoformat(), len(cfg.get("sources", [])))

        ai = TextAI(os.environ.get("GEMINI_API_KEY", ""), worker, s.get("text_models"))
        posts, source_status, warnings, x_status = collect(cfg, worker, since)
        log.info("%d new posts collected", len(posts))
        found, research_status = run_research(ai, cfg.get("research", []), since, s, set(cfg.get("seen", [])))
        if found:
            known = {p.id for p in posts}
            posts.extend(p for p in found if p.id not in known)
            log.info("%d articles added by research prompts", len(found))
        warnings.extend(f"Research prompt {r['id']}: {r['error']}" for r in research_status if r.get("error"))
        media = transcribe_posts(posts, s)
        if media["videos"]:
            log.info("videos: %d found, %d transcribed (%.0f min), %d skipped", media["videos"], media["transcribed"],
                     media["seconds"] / 60, media["skipped"])

        data = build_digest(ai, posts, cfg, warnings)
        stories = sum(len(t["stories"]) for t in data["topics"])
        worker.finish_digest(digest_id, {
            "status": "ready", "data": data, "item_count": len(posts), "story_count": stories,
            "seen": [p.id for p in posts], "source_status": source_status, "research_status": research_status,
            "x_status": x_status, "ai_calls": ai.calls,
        })
        models = ", ".join(sorted(set(ai.used))) or "none"
        summary = (f"{len(posts)} posts -> {stories} stories; videos {media['transcribed']}/{media['videos']} transcribed; "
                   f"AI: {models}")
        log.info(summary)
        worker.finish_run(run_id, "done", summary, LOG_BUFFER.getvalue())
        return 0
    except Exception as exc:  # noqa: BLE001
        log.exception("run failed")
        try:
            worker.finish_digest(digest_id, {"status": "failed", "error": str(exc)[:900]})
        except Exception:  # noqa: BLE001
            log.exception("could not report the failure to the Worker")
        try:
            worker.finish_run(run_id, "error", str(exc)[:300], LOG_BUFFER.getvalue())
        except Exception:  # noqa: BLE001
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
