"""Client for the Cloudflare Worker control plane (config, digests, AI fallback, Instagram reading)."""
from __future__ import annotations

import time

import requests


class WorkerError(RuntimeError):
    pass


class WorkerAPI:
    def __init__(self, base_url: str, secret: str):
        self.base = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {secret}"

    def _call(self, method: str, path: str, *, retries: int = 2, timeout: int = 60, **kw):
        last = None
        for attempt in range(retries + 1):
            try:
                r = self.session.request(method, self.base + path, timeout=timeout, **kw)
                if r.status_code >= 500 and attempt < retries:
                    time.sleep(3 * (attempt + 1))
                    continue
                if not r.ok:
                    raise WorkerError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
                return r.json() if r.content else {}
            except requests.RequestException as exc:
                last = exc
                time.sleep(3 * (attempt + 1))
        raise WorkerError(f"{method} {path} failed: {last}")

    def config(self, digest_id: str) -> dict:
        return self._call("GET", "/pipeline/config", params={"digest_id": digest_id})

    def x_config(self) -> dict:
        return self._call("GET", "/pipeline/x-config")

    def x_report(self, payload: dict) -> None:
        self._call("POST", "/pipeline/x-report", json=payload)

    def new_preview_digest(self) -> str:
        return self._call("POST", "/pipeline/digests", json={})["id"]

    def finish_digest(self, digest_id: str, payload: dict) -> None:
        self._call("POST", f"/pipeline/digests/{digest_id}", json=payload, timeout=120)

    def start_run(self, run_id: str, digest_id: str, mode: str, trigger: str, gh_run_url: str) -> str:
        res = self._call("POST", "/pipeline/runs", json={
            "id": run_id, "digest_id": digest_id, "mode": mode, "trigger": trigger, "gh_run_url": gh_run_url})
        return res["id"]

    def finish_run(self, run_id: str, status: str, summary: str, log: str) -> None:
        self._call("PATCH", f"/pipeline/runs/{run_id}", json={
            "status": status, "summary": summary, "log": log[-20000:]})

    def llm(self, system: str, user: str, size: str = "large") -> str:
        res = self._call("POST", "/pipeline/ai/text", timeout=180, retries=1, json={
            "system": system, "user": user, "size": size})
        return res.get("text", "")

    def instagram_media(self, username: str, limit: int = 25, after: str = "") -> dict:
        return self._call("GET", "/pipeline/ig-media", params={"username": username, "limit": limit, "after": after},
                          timeout=60)
