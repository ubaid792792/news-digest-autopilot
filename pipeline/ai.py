"""Text generation with free-tier fallbacks: Gemini models in order (free tier, no card), then the
Cloudflare Workers AI LLM through the Worker."""
from __future__ import annotations

import json
import logging
import re
import time

import requests

from .worker_api import WorkerAPI

log = logging.getLogger("ai")

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
# Each Gemini model has its own free daily allowance, so a long chain keeps the digest going.
DEFAULT_TEXT_MODELS = [
    "gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash", "gemini-flash-latest",
    "gemini-3-flash-preview", "gemini-2.5-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite",
    "gemini-flash-lite-latest", "gemini-2.5-flash-lite", "gemma-4-31b-it", "gemma-4-26b-a4b-it",
]
MAX_RATE_WAIT = 65  # seconds; longer waits mean the model is out for now


def parse_json(text: str):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        chunk = text[start:end + 1] if start >= 0 and end > start else text
        try:
            return json.loads(chunk)
        except json.JSONDecodeError:
            # Smaller models sometimes leave trailing commas or unquoted keys; repair instead of failing.
            from json_repair import repair_json
            data = repair_json(chunk, return_objects=True)
            if isinstance(data, dict) and data:
                return data
            raise


def _retry_delay(body: str) -> float | None:
    m = re.search(r'"retryDelay":\s*"(\d+(?:\.\d+)?)s"', body)
    return float(m.group(1)) if m else None


class TextAI:
    def __init__(self, gemini_key: str, worker: WorkerAPI, models: list[str] | None = None):
        self.key = gemini_key
        self.worker = worker
        self.models = models or DEFAULT_TEXT_MODELS
        self.exhausted: set[str] = set()
        self.used: list[str] = []
        self.calls = 0

    def _gemini(self, model: str, system: str, user: str) -> str:
        if model.startswith("gemma"):
            # Gemma has no system instruction or JSON mode in the API: fold both into the prompt.
            body = {"contents": [{"role": "user", "parts": [{"text": f"{system}\n\n{user}\n\nReply with valid JSON only."}]}],
                    "generationConfig": {"temperature": 0.2}}
        else:
            body = {
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2},
            }
        rate_waits = 0
        server_errors = 0
        while True:
            self.calls += 1
            r = requests.post(GEMINI_URL.format(model=model), json=body, timeout=180,
                              headers={"x-goog-api-key": self.key})
            if r.status_code in (500, 502, 503, 504) and server_errors < 1:
                server_errors += 1
                time.sleep(8)
                continue
            if r.status_code == 429:
                # Per-minute limits clear quickly: wait and retry. Daily limits mean move to the next model.
                delay = _retry_delay(r.text)
                daily = "PerDay" in r.text or (delay is not None and delay > MAX_RATE_WAIT)
                if not daily and rate_waits < 3:
                    rate_waits += 1
                    wait = min(MAX_RATE_WAIT, (delay or 20) + 2)
                    log.info("%s: per-minute limit, waiting %.0fs", model, wait)
                    time.sleep(wait)
                    continue
                self.exhausted.add(model)
                raise RuntimeError(f"{model}: daily free limit reached")
            if r.status_code in (400, 403, 404):
                self.exhausted.add(model)
                raise RuntimeError(f"{model}: HTTP {r.status_code} {r.text[:160]}")
            r.raise_for_status()
            cand = (r.json().get("candidates") or [{}])[0]
            parts = cand.get("content", {}).get("parts", [])
            text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
            if not text:
                raise RuntimeError(f"{model}: empty response ({cand.get('finishReason')})")
            return text

    def _worker(self, system: str, user: str, size: str):
        self.calls += 1
        data = parse_json(self.worker.llm(system, user + "\n\nReturn only valid JSON.", size))
        self.used.append(f"workers-ai-{size}")
        return data

    def json(self, system: str, user: str, check=None):
        """Gemini chain, then Cloudflare's large model. `check(data)` may raise to reject a malformed
        answer, which moves on to the next model."""
        errors = []
        if self.key:
            for model in self.models:
                if model in self.exhausted:
                    continue
                try:
                    data = parse_json(self._gemini(model, system, user))
                    if check:
                        check(data)
                    self.used.append(model)
                    return data
                except Exception as exc:  # noqa: BLE001 - try the next free model
                    errors.append(str(exc)[:200])
                    log.warning("text model failed: %s", str(exc)[:200])
        try:
            data = self._worker(system, user, "large")
            if check:
                check(data)
            return data
        except Exception as exc:  # noqa: BLE001
            errors.append(f"workers-ai-large: {exc}")
        raise RuntimeError("all text models failed: " + " | ".join(errors[-4:]))
