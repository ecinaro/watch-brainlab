#!/usr/bin/env python3
"""BrainWatch transcript: VTT parsing + Whisper API transcription.

Handles native captions (VTT) and Whisper API fallback (Groq/OpenAI).
Pure stdlib — no pip dependencies.
"""
from __future__ import annotations

import io
import json
import math
import mimetypes
import os
import re
import shutil
import ssl
import subprocess
import sys
import time
import uuid
import urllib.error
from pathlib import Path
from urllib.request import Request, urlopen


# ---------------------------------------------------------------------------
# VTT parsing
# ---------------------------------------------------------------------------
_TS_RE = re.compile(
    r"(\d{2}):(\d{2}):(\d{2})[.,](\d{3})\s+-->\s+(\d{2}):(\d{2}):(\d{2})[.,](\d{3})"
)
_TAG_RE = re.compile(r"<[^>]+>")


def _to_seconds(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def parse_vtt(path: str) -> list[dict]:
    """Parse a WebVTT file into [{start, end, text}, ...] segments.
    
    Deduplicates rolling captions common in YouTube auto-subs.
    """
    text = Path(path).read_text(encoding="utf-8", errors="ignore")
    lines = text.splitlines()
    segments: list[dict] = []
    i = 0
    while i < len(lines):
        match = _TS_RE.match(lines[i])
        if not match:
            i += 1
            continue
        start = _to_seconds(*match.groups()[:4])
        end = _to_seconds(*match.groups()[4:])
        i += 1
        cue_lines: list[str] = []
        while i < len(lines) and lines[i].strip():
            cleaned = _TAG_RE.sub("", lines[i]).strip()
            if cleaned:
                cue_lines.append(cleaned)
            i += 1
        cue_text = " ".join(cue_lines).strip()
        if cue_text:
            segments.append({"start": round(start, 2), "end": round(end, 2), "text": cue_text})
        i += 1
    return _dedupe_segments(segments)


def _dedupe_segments(segments: list[dict]) -> list[dict]:
    """Collapse rolling duplicates from YouTube auto-subs."""
    out: list[dict] = []
    for seg in segments:
        if out and seg["text"] == out[-1]["text"]:
            out[-1]["end"] = seg["end"]
            continue
        if out and seg["text"].startswith(out[-1]["text"] + " "):
            out[-1]["text"] = seg["text"]
            out[-1]["end"] = seg["end"]
            continue
        out.append(seg)
    return out


def filter_range(
    segments: list[dict],
    start_seconds: float | None,
    end_seconds: float | None,
) -> list[dict]:
    """Return segments overlapping [start, end]."""
    if start_seconds is None and end_seconds is None:
        return segments
    lo = start_seconds if start_seconds is not None else float("-inf")
    hi = end_seconds if end_seconds is not None else float("inf")
    return [s for s in segments if s["end"] >= lo and s["start"] <= hi]


def format_transcript(segments: list[dict]) -> str:
    """Format segments as timestamped lines: [MM:SS] text."""
    lines = []
    for seg in segments:
        t = int(seg["start"])
        lines.append(f"[{t // 60:02d}:{t % 60:02d}] {seg['text']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Config / API key loading
# ---------------------------------------------------------------------------
CONFIG_DIR = Path.home() / ".config" / "brainwatch"
CONFIG_FILE = CONFIG_DIR / ".env"


def _read_dotenv_key(path: Path, name: str) -> str | None:
    """Read a single key from a .env file."""
    if not path.exists():
        return None
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() != name:
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] in ('"', "'") and value[-1] == value[0]:
                value = value[1:-1]
            return value or None
    except OSError:
        return None
    return None


def load_whisper_key(preferred: str | None = None) -> tuple[str, str] | tuple[None, None]:
    """Return (backend, api_key). Prefers Groq, falls back to OpenAI."""
    dotenv_paths = [CONFIG_FILE, Path.cwd() / ".env"]
    candidates = [("GROQ_API_KEY", "groq"), ("OPENAI_API_KEY", "openai")]
    if preferred is not None:
        candidates = [c for c in candidates if c[1] == preferred]
    for key_name, backend in candidates:
        value = os.environ.get(key_name, "").strip() or None
        if not value:
            for p in dotenv_paths:
                value = _read_dotenv_key(p, key_name)
                if value:
                    break
        if value:
            return backend, value
    return None, None


# ---------------------------------------------------------------------------
# Audio extraction
# ---------------------------------------------------------------------------
def extract_audio(video_path: str, out_path: Path) -> Path:
    """Extract mono 16kHz 64kbps mp3 (~480 kB/min)."""
    if shutil.which("ffmpeg") is None:
        raise SystemExit("ffmpeg not found. Install ffmpeg to continue.")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(Path(video_path).resolve()),
        "-vn", "-acodec", "libmp3lame",
        "-ar", "16000", "-ac", "1", "-b:a", "64k",
        str(out_path.resolve()),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(f"ffmpeg audio extraction failed: {result.stderr.strip()}")
    if not out_path.exists() or out_path.stat().st_size == 0:
        raise SystemExit("ffmpeg produced no audio — video may have no audio track")
    return out_path


def _audio_duration(audio_path: Path) -> float:
    """Get audio duration via ffprobe."""
    if shutil.which("ffprobe") is None:
        raise SystemExit("ffprobe not found. Install ffmpeg to continue.")
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_format", str(audio_path.resolve())],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise SystemExit(f"ffprobe failed: {result.stderr.strip()}")
    fmt = json.loads(result.stdout or "{}").get("format", {})
    return float(fmt.get("duration") or 0.0)


# ---------------------------------------------------------------------------
# Whisper API
# ---------------------------------------------------------------------------
GROQ_ENDPOINT = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_MODEL = "whisper-large-v3"
OPENAI_ENDPOINT = "https://api.openai.com/v1/audio/transcriptions"
OPENAI_MODEL = "whisper-1"
MAX_UPLOAD_BYTES = 24 * 1024 * 1024  # 24 MB (margin under 25 MB limit)
MAX_ATTEMPTS = 4
MAX_429_RETRIES = 2
RETRY_BASE_DELAY = 2.0


def _build_multipart(fields: dict[str, str], file_path: Path) -> tuple[bytes, str]:
    """Build multipart/form-data body for Whisper API upload."""
    boundary = f"----BrainWatchBoundary{uuid.uuid4().hex}"
    eol = b"\r\n"
    buf = io.BytesIO()
    for name, value in fields.items():
        buf.write(f"--{boundary}".encode())
        buf.write(eol)
        buf.write(f'Content-Disposition: form-data; name="{name}"'.encode())
        buf.write(eol)
        buf.write(eol)
        buf.write(str(value).encode())
        buf.write(eol)
    mimetype = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    buf.write(f"--{boundary}".encode())
    buf.write(eol)
    buf.write(
        f'Content-Disposition: form-data; name="file"; filename="{file_path.name}"'.encode()
    )
    buf.write(eol)
    buf.write(f"Content-Type: {mimetype}".encode())
    buf.write(eol)
    buf.write(eol)
    buf.write(file_path.read_bytes())
    buf.write(eol)
    buf.write(f"--{boundary}--".encode())
    buf.write(eol)
    return buf.getvalue(), boundary


def _read_error_body(exc: urllib.error.HTTPError) -> str:
    try:
        body = exc.read()
    except Exception:
        return ""
    if not body:
        return ""
    try:
        return f" — {body.decode('utf-8', errors='replace')[:400]}"
    except Exception:
        return ""


def _retry_after(exc: urllib.error.HTTPError) -> float | None:
    header = exc.headers.get("Retry-After") if getattr(exc, "headers", None) else None
    if not header:
        return None
    try:
        return float(header)
    except ValueError:
        return None


def _post_whisper(endpoint: str, api_key: str, model: str, audio_path: Path) -> dict:
    """Upload audio to Whisper API with retries."""
    fields = {"model": model, "response_format": "verbose_json", "temperature": "0"}
    body, boundary = _build_multipart(fields, audio_path)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "User-Agent": "brainwatch/1.0 (python-urllib)",
    }
    context = ssl.create_default_context()
    rate_limit_hits = 0
    last_exc: Exception | None = None
    last_detail = ""
    for attempt in range(MAX_ATTEMPTS):
        request = Request(endpoint, data=body, headers=headers, method="POST")
        try:
            with urlopen(request, timeout=300, context=context) as response:
                payload = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = _read_error_body(exc)
            last_exc, last_detail = exc, detail
            if 400 <= exc.code < 500 and exc.code != 429:
                raise SystemExit(f"Whisper request failed: {exc}{detail}")
            if exc.code == 429:
                rate_limit_hits += 1
                if rate_limit_hits >= MAX_429_RETRIES:
                    raise SystemExit(f"Whisper request failed: {exc}{detail}")
                delay = _retry_after(exc) or RETRY_BASE_DELAY * (2 ** attempt) + 1
            else:
                delay = RETRY_BASE_DELAY * (2 ** attempt)
            if attempt < MAX_ATTEMPTS - 1:
                print(
                    f"[brainwatch] whisper HTTP {exc.code} — retrying in {delay:.1f}s "
                    f"(attempt {attempt + 2}/{MAX_ATTEMPTS})",
                    file=sys.stderr,
                )
                time.sleep(delay)
            continue
        except (urllib.error.URLError, TimeoutError, ConnectionResetError, OSError) as exc:
            last_exc, last_detail = exc, ""
            if attempt < MAX_ATTEMPTS - 1:
                delay = RETRY_BASE_DELAY * (attempt + 1)
                print(
                    f"[brainwatch] whisper network error ({type(exc).__name__}) — "
                    f"retrying in {delay:.1f}s (attempt {attempt + 2}/{MAX_ATTEMPTS})",
                    file=sys.stderr,
                )
                time.sleep(delay)
            continue
        try:
            return json.loads(payload)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"Whisper returned non-JSON: {exc}: {payload[:200]}")
    raise SystemExit(
        f"Whisper failed after {MAX_ATTEMPTS} attempts: {last_exc}{last_detail}"
    )


def _segments_from_response(data: dict) -> list[dict]:
    """Convert Whisper verbose_json to [{start, end, text}, ...]."""
    out: list[dict] = []
    for seg in data.get("segments") or []:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        out.append({
            "start": round(float(seg.get("start") or 0.0), 2),
            "end": round(float(seg.get("end") or 0.0), 2),
            "text": text,
        })
    if not out:
        full = (data.get("text") or "").strip()
        if full:
            out.append({"start": 0.0, "end": 0.0, "text": full})
    return out


def _plan_chunks(
    total_seconds: float, total_bytes: int, max_bytes: int = MAX_UPLOAD_BYTES,
) -> list[tuple[float, float]]:
    """Split duration into contiguous (offset, duration) chunks under max_bytes."""
    if total_bytes <= max_bytes or total_seconds <= 0:
        return [(0.0, total_seconds)]
    n = math.ceil(total_bytes / max_bytes)
    chunk = total_seconds / n
    plan: list[tuple[float, float]] = []
    for i in range(n):
        offset = i * chunk
        duration = (total_seconds - offset) if i == n - 1 else chunk
        plan.append((round(offset, 3), round(duration, 3)))
    return plan


def _split_audio(
    full_audio: Path, work_dir: Path, plan: list[tuple[float, float]],
) -> list[tuple[Path, float]]:
    """Slice audio into chunks via stream copy."""
    if shutil.which("ffmpeg") is None:
        raise SystemExit("ffmpeg not found. Install ffmpeg to continue.")
    work_dir.mkdir(parents=True, exist_ok=True)
    chunks: list[tuple[Path, float]] = []
    for idx, (offset, duration) in enumerate(plan):
        out = work_dir / f"chunk_{idx:03d}.mp3"
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{offset:.3f}",
            "-i", str(full_audio.resolve()),
            "-t", f"{duration:.3f}",
            "-c", "copy", str(out.resolve()),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0 or not out.exists() or out.stat().st_size == 0:
            raise SystemExit(f"ffmpeg chunk split failed (chunk {idx + 1}): {result.stderr.strip()}")
        chunks.append((out, offset))
    return chunks


def _shift_segments(segments: list[dict], offset: float) -> list[dict]:
    """Shift segment timestamps by offset."""
    if offset == 0:
        return segments
    return [
        {"start": round(s["start"] + offset, 2), "end": round(s["end"] + offset, 2), "text": s["text"]}
        for s in segments
    ]


def _transcribe_file(backend: str, api_key: str, audio_path: Path) -> list[dict]:
    """Upload one audio file and return 0-based segments."""
    if backend == "groq":
        resp = _post_whisper(GROQ_ENDPOINT, api_key, GROQ_MODEL, audio_path)
    elif backend == "openai":
        resp = _post_whisper(OPENAI_ENDPOINT, api_key, OPENAI_MODEL, audio_path)
    else:
        raise SystemExit(f"Unknown whisper backend: {backend}")
    return _segments_from_response(resp)


def transcribe_with_whisper(
    video_path: str,
    audio_out: Path,
    backend: str | None = None,
    api_key: str | None = None,
) -> tuple[list[dict], str]:
    """Full flow: extract audio → upload → parse segments.
    
    Returns (segments, backend_used).
    """
    if backend is None or api_key is None:
        det_backend, det_key = load_whisper_key()
        backend = backend or det_backend
        api_key = api_key or det_key
    if not backend or not api_key:
        raise SystemExit(
            "No Whisper API key available. Set GROQ_API_KEY or OPENAI_API_KEY "
            "in ~/.config/brainwatch/.env or as environment variables."
        )
    print(f"[brainwatch] extracting audio for Whisper ({backend})…", file=sys.stderr)
    audio_path = extract_audio(video_path, audio_out)
    audio_bytes = audio_path.stat().st_size

    def do_one(path: Path) -> list[dict]:
        return _transcribe_file(backend, api_key, path)

    if audio_bytes <= MAX_UPLOAD_BYTES:
        print(
            f"[brainwatch] audio: {audio_bytes / 1024:.0f} kB — uploading to {backend}…",
            file=sys.stderr,
        )
        segments = do_one(audio_path)
    else:
        duration = _audio_duration(audio_path)
        plan = _plan_chunks(duration, audio_bytes)
        print(
            f"[brainwatch] audio: {audio_bytes / (1024*1024):.0f} MB — "
            f"splitting into {len(plan)} chunks…",
            file=sys.stderr,
        )
        chunks = _split_audio(audio_path, audio_out.parent / "chunks", plan)
        segments: list[dict] = []
        failures = 0
        for idx, (path, offset) in enumerate(chunks):
            try:
                chunk_segs = do_one(path)
            except SystemExit as exc:
                failures += 1
                print(f"[brainwatch] chunk {idx+1}/{len(chunks)} failed — skipping ({exc})", file=sys.stderr)
                continue
            segments.extend(_shift_segments(chunk_segs, offset))
            print(f"[brainwatch] chunk {idx+1}/{len(chunks)} → {len(chunk_segs)} segments", file=sys.stderr)
        if failures == len(chunks):
            raise SystemExit("Whisper failed on every audio chunk")
    if not segments:
        raise SystemExit("Whisper returned no transcript segments")
    print(f"[brainwatch] transcribed {len(segments)} segments via {backend}", file=sys.stderr)
    return segments, backend
