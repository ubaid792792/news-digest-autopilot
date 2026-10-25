"""Video transcription on the GitHub Actions runner (faster-whisper on CPU, free and unlimited)."""
from __future__ import annotations

import logging
import os
import tempfile
import time
from pathlib import Path

import requests

from .sources import UA, Post

log = logging.getLogger("media")

MAX_BYTES = 250 * 1024 * 1024


def _download(url: str, folder: Path) -> Path:
    path = folder / "video.mp4"
    with requests.get(url, stream=True, timeout=60, headers={"User-Agent": UA}) as r:
        r.raise_for_status()
        size = 0
        with open(path, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                size += len(chunk)
                if size > MAX_BYTES:
                    raise RuntimeError("video file too large")
                f.write(chunk)
    return path


def _duration(path: Path) -> float | None:
    try:
        import av
        with av.open(str(path)) as c:
            return float(c.duration / 1_000_000) if c.duration else None
    except Exception:  # noqa: BLE001
        return None


def _load_audio(path: Path):
    """16 kHz mono float32 samples. Decoded here with PyAV's current API because faster-whisper's own
    decoder passes an option newer PyAV versions removed."""
    import av
    import numpy as np
    chunks = []
    with av.open(str(path)) as c:
        stream = next((s for s in c.streams if s.type == "audio"), None)
        if stream is None:
            return None
        resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
        for frame in c.decode(stream):
            chunks.extend(f.to_ndarray() for f in resampler.resample(frame))
        chunks.extend(f.to_ndarray() for f in resampler.resample(None))
    if not chunks:
        return None
    return np.concatenate(chunks, axis=1).flatten().astype(np.float32) / 32768.0


def transcribe_posts(posts: list[Post], s: dict) -> dict:
    """Fill post.transcript for posts with a video URL, newest first, within the per-run minute budget."""
    videos = [p for p in posts if p.video_url]
    stats = {"videos": len(videos), "transcribed": 0, "skipped": 0, "seconds": 0.0}
    if not videos:
        return stats
    if not s.get("transcribe_videos", True):
        for p in videos:
            p.notes.append("Video not transcribed (turned off in settings); text only.")
        stats["skipped"] = len(videos)
        return stats
    from faster_whisper import WhisperModel

    model_name = s.get("whisper_model") or "small"
    started = time.time()
    model = WhisperModel(model_name, device="cpu", compute_type="int8", cpu_threads=os.cpu_count() or 4)
    log.info("loaded whisper %s in %.0fs", model_name, time.time() - started)
    budget = float(s.get("max_video_minutes_per_run") or 0) * 60
    max_each = float(s.get("max_video_minutes_each") or 15) * 60
    videos.sort(key=lambda p: p.posted_at.timestamp() if p.posted_at else 0, reverse=True)
    for p in videos:
        if p.video_seconds and p.video_seconds > max_each:
            p.notes.append(f"Video not transcribed (longer than {int(max_each // 60)} minutes); text only.")
            stats["skipped"] += 1
            continue
        if budget - stats["seconds"] <= 0:
            p.notes.append("Video not transcribed (video limit for this run reached); text only.")
            stats["skipped"] += 1
            continue
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = _download(p.video_url, Path(tmp))
                seconds = _duration(path) or p.video_seconds or 0
                if seconds > max_each:
                    p.notes.append(f"Video not transcribed (longer than {int(max_each // 60)} minutes); text only.")
                    stats["skipped"] += 1
                    continue
                audio = _load_audio(path)
                if audio is None or not len(audio):
                    p.notes.append("Video has no sound; text only.")
                    continue
                t0 = time.time()
                segments, info = model.transcribe(audio, vad_filter=True, beam_size=1,
                                                  condition_on_previous_text=False)
                text = " ".join(seg.text.strip() for seg in segments).strip()
                stats["seconds"] += info.duration or seconds
                if text:
                    p.transcript = text[:12000]
                    p.transcript_lang = info.language or ""
                    stats["transcribed"] += 1
                    log.info("transcribed %s (%.0fs audio, %s) in %.0fs", p.id, info.duration or 0, info.language, time.time() - t0)
                else:
                    p.notes.append("Video has no speech; text only.")
        except Exception as exc:  # noqa: BLE001
            log.warning("transcription failed for %s: %s", p.id, exc)
            p.notes.append("Video could not be transcribed; text only.")
            stats["skipped"] += 1
    return stats
