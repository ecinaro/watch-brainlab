#!/usr/bin/env python3
"""BrainWatch media operations: download, probe, extract frames.

Wraps yt-dlp, ffmpeg, and ffprobe. Pure stdlib — no pip dependencies.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
MAX_FPS = 2.0
SCENE_THRESHOLD = 0.20
SCENE_MIN_FRAMES = 8
KEYFRAME_MIN = 4
MAX_HEIGHT = 1998
DEDUP_THUMB = 16
DEDUP_THRESHOLD = 2.0
SHOWINFO_TS_RE = re.compile(r"pts_time:([0-9.]+)")
VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi", ".flv", ".wmv"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _scale_filter(resolution: int) -> str:
    return (
        f"scale=w='min({resolution},iw)':h='min({MAX_HEIGHT},ih)':"
        "force_original_aspect_ratio=decrease:force_divisible_by=2"
    )


def _clamp_fps(fps: float, duration: float, max_frames: int) -> tuple[float, int]:
    fps = min(fps, MAX_FPS)
    target = min(max_frames, max(1, int(round(fps * duration))))
    return fps, target


def _even_indices(count: int, n: int) -> list[int]:
    """Indices of n evenly-spaced items out of count (first + last kept)."""
    if n >= count:
        return list(range(count))
    if n <= 1:
        return [0]
    return [round(i * (count - 1) / (n - 1)) for i in range(n)]


# ---------------------------------------------------------------------------
# Time parsing / formatting
# ---------------------------------------------------------------------------
def parse_time(value: str | float | int | None) -> float | None:
    """Parse SS, MM:SS, or HH:MM:SS into seconds."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s:
        return None
    parts = s.split(":")
    try:
        if len(parts) == 1:
            return float(parts[0])
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    except ValueError:
        pass
    raise SystemExit(f"Cannot parse time: {value!r} (expected SS, MM:SS, or HH:MM:SS)")


def format_time(seconds: float) -> str:
    """Format seconds as MM:SS or H:MM:SS."""
    total = int(round(seconds))
    hours, rem = divmod(total, 3600)
    minutes, sec = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{sec:02d}"
    return f"{minutes:02d}:{sec:02d}"


def parse_timestamps(value: str | None) -> list[float]:
    """Parse comma-separated timestamps into sorted, deduplicated seconds."""
    if not value:
        return []
    out: list[float] = []
    for token in value.split(","):
        token = token.strip()
        if not token:
            continue
        seconds = parse_time(token)
        if seconds is not None:
            out.append(float(seconds))
    return sorted(set(out))


# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------
def is_url(source: str) -> bool:
    """True if source looks like an HTTP(S) URL."""
    if source.startswith("-"):
        return False
    parsed = urlparse(source)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


# ---------------------------------------------------------------------------
# Metadata (ffprobe)
# ---------------------------------------------------------------------------
def get_metadata(video_path: str) -> dict:
    """Probe video for duration, resolution, codec, audio presence."""
    if shutil.which("ffprobe") is None:
        raise SystemExit("ffprobe not found. Install ffmpeg to continue.")
    result = subprocess.run(
        [
            "ffprobe", "-v", "quiet",
            "-print_format", "json",
            "-show_format", "-show_streams",
            str(Path(video_path).resolve()),
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise SystemExit(f"ffprobe failed: {result.stderr.strip()}")
    data = json.loads(result.stdout or "{}")
    streams = data.get("streams", [])
    fmt = data.get("format", {})
    vs = next((s for s in streams if s.get("codec_type") == "video"), {})
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    duration = float(fmt.get("duration") or vs.get("duration") or 0)
    return {
        "duration_seconds": duration,
        "width": vs.get("width"),
        "height": vs.get("height"),
        "codec": vs.get("codec_name"),
        "has_audio": has_audio,
    }


# ---------------------------------------------------------------------------
# Download (yt-dlp)
# ---------------------------------------------------------------------------
def _pick_subtitle(out_dir: Path) -> Path | None:
    candidates = sorted(out_dir.glob("video*.vtt"))
    if not candidates:
        return None
    preferred = [
        c for c in candidates
        if any(m in c.name for m in (".en.", ".en-US.", ".en-GB.", ".en-orig."))
    ]
    return preferred[0] if preferred else candidates[0]


def _pick_video(out_dir: Path) -> Path | None:
    for ext in (".mp4", ".mkv", ".webm", ".mov", ".m4a", ".mp3", ".opus"):
        for f in out_dir.glob(f"video*{ext}"):
            return f
    for f in out_dir.glob("video.*"):
        if f.suffix.lower() in VIDEO_EXTS:
            return f
    return None


def _read_info(info_path: Path, url: str) -> dict:
    if not info_path.exists():
        return {"url": url}
    try:
        raw = json.loads(info_path.read_text(encoding="utf-8"))
        return {
            "title": raw.get("title"),
            "uploader": raw.get("uploader") or raw.get("channel"),
            "duration": raw.get("duration"),
            "url": raw.get("webpage_url") or url,
        }
    except Exception as exc:
        print(f"[brainwatch] info.json parse failed: {exc}", file=sys.stderr)
        return {"url": url}


def fetch_captions_only(url: str, out_dir: Path) -> dict:
    """Fetch metadata + captions without downloading video."""
    if shutil.which("yt-dlp") is None:
        raise SystemExit("yt-dlp not found. Install yt-dlp to continue.")
    out_dir.mkdir(parents=True, exist_ok=True)
    tpl = str(out_dir / "video.%(ext)s")
    cmd = [
        "yt-dlp", "--skip-download",
        "--write-info-json", "--write-subs", "--write-auto-subs",
        "--sub-langs", "en.*", "--sub-format", "vtt", "--convert-subs", "vtt",
        "--no-playlist", "--ignore-errors", "-o", tpl, "--", url,
    ]
    subprocess.run(cmd, stdout=sys.stderr, stderr=sys.stderr)
    return {
        "video_path": None,
        "subtitle_path": str(s) if (s := _pick_subtitle(out_dir)) else None,
        "info": _read_info(out_dir / "video.info.json", url),
        "downloaded": False,
    }


def download_video(source: str, out_dir: Path, audio_only: bool = False) -> dict:
    """Download video (or resolve local path). Returns paths + metadata dict."""
    if not is_url(source):
        p = Path(source).expanduser().resolve()
        if not p.exists():
            raise SystemExit(f"File not found: {p}")
        if p.suffix.lower() not in VIDEO_EXTS:
            print(f"[brainwatch] warning: {p.suffix} not a known video ext", file=sys.stderr)
        return {
            "video_path": str(p),
            "subtitle_path": None,
            "info": {"title": p.name, "url": str(p)},
            "downloaded": False,
        }

    if shutil.which("yt-dlp") is None:
        raise SystemExit("yt-dlp not found. Install yt-dlp to continue.")
    out_dir.mkdir(parents=True, exist_ok=True)
    tpl = str(out_dir / "video.%(ext)s")
    fmt = "ba/bestaudio" if audio_only else "bv*[height<=720]+ba/b[height<=720]/bv+ba/b"
    cmd = [
        "yt-dlp", "-N", "8", "-f", fmt,
        "--merge-output-format", "mp4",
        "--write-info-json", "--write-subs", "--write-auto-subs",
        "--sub-langs", "en.*", "--sub-format", "vtt", "--convert-subs", "vtt",
        "--no-playlist", "--ignore-errors", "-o", tpl, "--", source,
    ]
    result = subprocess.run(cmd, stdout=sys.stderr, stderr=sys.stderr)
    video = _pick_video(out_dir)
    if video is None:
        raise SystemExit(
            f"yt-dlp did not produce a video file in {out_dir} (exit {result.returncode})"
        )
    return {
        "video_path": str(video),
        "subtitle_path": str(s) if (s := _pick_subtitle(out_dir)) else None,
        "info": _read_info(out_dir / "video.info.json", source),
        "downloaded": True,
    }


# ---------------------------------------------------------------------------
# FPS budget
# ---------------------------------------------------------------------------
def auto_fps(duration: float, max_frames: int = 100) -> tuple[float, int]:
    """Pick fps targeting a sensible frame budget for full-video scans."""
    if duration <= 0:
        return 1.0, 1
    if duration <= 30:
        target = min(max_frames, max(12, int(round(duration))))
    elif duration <= 60:
        target = min(max_frames, 40)
    elif duration <= 180:
        target = min(max_frames, 60)
    elif duration <= 600:
        target = min(max_frames, 80)
    else:
        target = max_frames
    return _clamp_fps(target / duration, duration, max_frames)


def auto_fps_focus(duration: float, max_frames: int = 100) -> tuple[float, int]:
    """Denser budget for user-specified ranges."""
    if duration <= 0:
        return min(MAX_FPS, 2.0), 2
    if duration <= 5:
        target = min(max_frames, max(10, int(round(duration * 6))))
    elif duration <= 15:
        target = min(max_frames, max(30, int(round(duration * 4))))
    elif duration <= 30:
        target = min(max_frames, 60)
    elif duration <= 60:
        target = min(max_frames, 80)
    else:
        target = max_frames
    return _clamp_fps(target / duration, duration, max_frames)


# ---------------------------------------------------------------------------
# Frame extraction: uniform
# ---------------------------------------------------------------------------
def _extract_uniform(
    video_path: str,
    out_dir: Path,
    fps: float,
    resolution: int = 512,
    max_frames: int = 100,
    start_seconds: float | None = None,
    end_seconds: float | None = None,
) -> list[dict]:
    """Extract frames at a fixed fps."""
    if shutil.which("ffmpeg") is None:
        raise SystemExit("ffmpeg not found. Install ffmpeg to continue.")
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("frame_*.jpg"):
        old.unlink()
    pattern = str(out_dir / "frame_%04d.jpg")
    cmd: list[str] = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    if start_seconds is not None:
        cmd += ["-ss", f"{start_seconds:.3f}"]
    if end_seconds is not None:
        cmd += ["-to", f"{end_seconds:.3f}"]
    cmd += [
        "-i", str(Path(video_path).resolve()),
        "-vf", f"fps={fps},{_scale_filter(resolution)}",
        "-frames:v", str(max_frames),
        "-q:v", "4",
        pattern,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(f"ffmpeg frame extraction failed: {result.stderr.strip()}")
    offset = start_seconds or 0.0
    frames = sorted(out_dir.glob("frame_*.jpg"))
    return [
        {
            "index": i,
            "timestamp_seconds": round(offset + (i / fps if fps > 0 else 0.0), 2),
            "path": str(p),
            "reason": "uniform",
        }
        for i, p in enumerate(frames)
    ]


# ---------------------------------------------------------------------------
# Frame extraction: scene-aware
# ---------------------------------------------------------------------------
def _extract_scene_candidates(
    video_path: str,
    out_dir: Path,
    resolution: int = 512,
    start_seconds: float | None = None,
    end_seconds: float | None = None,
    threshold: float = SCENE_THRESHOLD,
) -> list[dict]:
    """Extract first frame + scene-change frames (uncapped detection)."""
    if shutil.which("ffmpeg") is None:
        raise SystemExit("ffmpeg not found. Install ffmpeg to continue.")
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("frame_*.jpg"):
        old.unlink()
    pattern = str(out_dir / "frame_%04d.jpg")
    cmd: list[str] = ["ffmpeg", "-hide_banner", "-loglevel", "info", "-y"]
    if start_seconds is not None:
        cmd += ["-ss", f"{start_seconds:.3f}"]
    if end_seconds is not None:
        cmd += ["-to", f"{end_seconds:.3f}"]
    vf = f"select='eq(n\\,0)+gt(scene\\,{threshold})',{_scale_filter(resolution)},showinfo"
    cmd += [
        "-i", str(Path(video_path).resolve()),
        "-vf", vf,
        "-vsync", "vfr",
        "-q:v", "4",
        pattern,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(f"ffmpeg scene extraction failed: {result.stderr.strip()}")
    offset = start_seconds or 0.0
    timestamps = [
        round(offset + float(m.group(1)), 2)
        for m in SHOWINFO_TS_RE.finditer(result.stderr)
    ]
    files = sorted(out_dir.glob("frame_*.jpg"))
    return [
        {
            "index": i,
            "timestamp_seconds": timestamps[i] if i < len(timestamps) else offset,
            "path": str(p),
            "reason": "first-frame" if i == 0 else "scene-change",
        }
        for i, p in enumerate(files)
    ]


# ---------------------------------------------------------------------------
# Frame dedup (perceptual)
# ---------------------------------------------------------------------------
def _frame_delta(a: bytes, b: bytes) -> float:
    """Mean absolute per-pixel difference between two grayscale thumbnails."""
    if not a or len(a) != len(b):
        return float("inf")
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def _thumb_frames(paths: list[Path]) -> list[bytes]:
    """Generate tiny grayscale thumbnails via a single ffmpeg pass."""
    if not paths:
        return []
    paths = [Path(p) for p in paths]
    m = re.match(r"(.*?)(\d+)(\.[A-Za-z0-9]+)$", paths[0].name)
    if m is None:
        return []
    prefix, digits, ext = m.group(1), m.group(2), m.group(3)
    pattern = str(paths[0].parent / f"{prefix}%0{len(digits)}d{ext}")
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-start_number", str(int(digits)),
        "-i", pattern,
        "-vf", f"scale={DEDUP_THUMB}:{DEDUP_THUMB},format=gray",
        "-f", "rawvideo", "-",
    ]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        return []
    chunk = DEDUP_THUMB * DEDUP_THUMB
    data = result.stdout
    if len(data) != chunk * len(paths):
        return []
    return [data[i * chunk:(i + 1) * chunk] for i in range(len(paths))]


def _dedupe_perceptual(
    candidates: list[dict], threshold: float = DEDUP_THRESHOLD,
) -> tuple[list[dict], int]:
    """Drop near-identical consecutive frames. Returns (kept, drop_count)."""
    if len(candidates) <= 1:
        return candidates, 0
    thumbs = _thumb_frames([Path(c["path"]) for c in candidates])
    if len(thumbs) != len(candidates):
        return candidates, 0  # fail-open
    kept = [candidates[0]]
    last = thumbs[0]
    dropped: list[dict] = []
    for cand, thumb in zip(candidates[1:], thumbs[1:]):
        if _frame_delta(thumb, last) <= threshold:
            dropped.append(cand)
        else:
            kept.append(cand)
            last = thumb
    for cand in dropped:
        try:
            Path(cand["path"]).unlink()
        except OSError:
            pass
    for i, frame in enumerate(kept):
        frame["index"] = i
    return kept, len(dropped)


# ---------------------------------------------------------------------------
# Even-sample (cap enforcement)
# ---------------------------------------------------------------------------
def _even_sample(candidates: list[dict], n: int) -> list[dict]:
    """Pick n evenly-spaced candidates, delete the rest, reindex."""
    selected = [candidates[i] for i in _even_indices(len(candidates), n)]
    keep_paths = {s["path"] for s in selected}
    for cand in candidates:
        if cand["path"] not in keep_paths:
            try:
                Path(cand["path"]).unlink()
            except OSError:
                pass
    for i, frame in enumerate(selected):
        frame["index"] = i
    return selected


# ---------------------------------------------------------------------------
# High-level frame extraction
# ---------------------------------------------------------------------------
def extract_frames_scene(
    video_path: str,
    out_dir: Path,
    fps: float,
    target_frames: int,
    resolution: int = 512,
    max_frames: int | None = 100,
    start_seconds: float | None = None,
    end_seconds: float | None = None,
    dedup: bool = True,
) -> tuple[list[dict], dict]:
    """Scene-aware extraction with uniform fallback for static video."""
    candidates = _extract_scene_candidates(
        video_path, out_dir, resolution=resolution,
        start_seconds=start_seconds, end_seconds=end_seconds,
    )
    if len(candidates) >= SCENE_MIN_FRAMES:
        deduped, n_dropped = _dedupe_perceptual(candidates) if dedup else (candidates, 0)
        cap = len(deduped) if max_frames is None else max_frames
        selected = _even_sample(deduped, cap)
        return selected, {
            "engine": "scene", "candidate_count": len(candidates),
            "deduped_count": n_dropped, "selected_count": len(selected), "fallback": False,
        }
    # Fallback to uniform
    fallback_cap = target_frames if max_frames is None else min(max_frames, target_frames)
    frames = _extract_uniform(
        video_path, out_dir, fps=fps, resolution=resolution,
        max_frames=fallback_cap, start_seconds=start_seconds, end_seconds=end_seconds,
    )
    n_dropped = 0
    if dedup:
        frames, n_dropped = _dedupe_perceptual(frames)
    return frames, {
        "engine": "uniform", "candidate_count": len(candidates),
        "deduped_count": n_dropped, "selected_count": len(frames), "fallback": True,
    }


def extract_frames_keyframe(
    video_path: str,
    out_dir: Path,
    resolution: int = 512,
    max_frames: int | None = 50,
    start_seconds: float | None = None,
    end_seconds: float | None = None,
    dedup: bool = True,
) -> tuple[list[dict], dict]:
    """Keyframe-only extraction (I-frames). Fast, near-instant."""
    if shutil.which("ffmpeg") is None:
        raise SystemExit("ffmpeg not found. Install ffmpeg to continue.")
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("frame_*.jpg"):
        old.unlink()
    pattern = str(out_dir / "frame_%04d.jpg")
    cmd: list[str] = ["ffmpeg", "-hide_banner", "-loglevel", "info", "-y"]
    if start_seconds is not None:
        cmd += ["-ss", f"{start_seconds:.3f}"]
    if end_seconds is not None:
        cmd += ["-to", f"{end_seconds:.3f}"]
    cmd += [
        "-skip_frame", "nokey",
        "-i", str(Path(video_path).resolve()),
        "-vf", f"{_scale_filter(resolution)},showinfo",
        "-vsync", "vfr", "-q:v", "4", pattern,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(f"ffmpeg keyframe extraction failed: {result.stderr.strip()}")
    offset = start_seconds or 0.0
    timestamps = [
        round(offset + float(m.group(1)), 2)
        for m in SHOWINFO_TS_RE.finditer(result.stderr)
    ]
    files = sorted(out_dir.glob("frame_*.jpg"))
    candidates = [
        {
            "index": i,
            "timestamp_seconds": timestamps[i] if i < len(timestamps) else offset,
            "path": str(p),
            "reason": "keyframe",
        }
        for i, p in enumerate(files)
    ]
    # Too few keyframes → uniform fallback
    if len(candidates) < KEYFRAME_MIN:
        for c in candidates:
            try:
                Path(c["path"]).unlink()
            except OSError:
                pass
        meta = get_metadata(video_path)
        eff_start = start_seconds or 0.0
        eff_end = end_seconds if end_seconds is not None else meta["duration_seconds"]
        eff_dur = max(0.0, eff_end - eff_start)
        budget = max_frames if max_frames is not None else 100
        fb_fps, _ = auto_fps(eff_dur, max_frames=budget)
        frames = _extract_uniform(
            video_path, out_dir, fps=fb_fps, resolution=resolution,
            max_frames=budget, start_seconds=start_seconds, end_seconds=end_seconds,
        )
        n_dropped = 0
        if dedup:
            frames, n_dropped = _dedupe_perceptual(frames)
        return frames, {
            "engine": "uniform", "candidate_count": len(candidates),
            "deduped_count": n_dropped, "selected_count": len(frames), "fallback": True,
        }
    # Normal path: dedup then even-sample
    count = len(candidates)
    deduped, n_dropped = _dedupe_perceptual(candidates) if dedup else (candidates, 0)
    cap = len(deduped) if max_frames is None else max_frames
    selected = _even_sample(deduped, cap)
    return selected, {
        "engine": "keyframe", "candidate_count": count,
        "deduped_count": n_dropped, "selected_count": len(selected), "fallback": False,
    }


def extract_at_timestamps(
    video_path: str,
    out_dir: Path,
    timestamps: list[float],
    resolution: int = 512,
    max_frames: int | None = None,
    start_seconds: float | None = None,
    end_seconds: float | None = None,
) -> tuple[list[dict], dict]:
    """Grab one frame at each requested timestamp (transcript cues)."""
    if shutil.which("ffmpeg") is None:
        raise SystemExit("ffmpeg not found. Install ffmpeg to continue.")
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("cue_*.jpg"):
        old.unlink()
    lo = start_seconds or 0.0
    hi = end_seconds if end_seconds is not None else float("inf")
    requested = sorted(set(round(float(t), 2) for t in timestamps))
    in_window = [t for t in requested if lo <= t <= hi]
    dropped = len(requested) - len(in_window)
    if max_frames is not None and len(in_window) > max_frames:
        points = [in_window[i] for i in _even_indices(len(in_window), max_frames)]
    else:
        points = in_window
    out: list[dict] = []
    for t in points:
        path = out_dir / f"cue_{len(out):04d}.jpg"
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{t:.3f}",
            "-i", str(Path(video_path).resolve()),
            "-frames:v", "1",
            "-vf", _scale_filter(resolution),
            "-q:v", "4", str(path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0 and path.exists():
            out.append({
                "index": len(out),
                "timestamp_seconds": t,
                "path": str(path),
                "reason": "transcript-cue",
            })
    return out, {
        "engine": "timestamps", "candidate_count": len(requested),
        "selected_count": len(out), "dropped_out_of_window": dropped, "fallback": False,
    }


def merge_frames(primary: list[dict], pinned: list[dict]) -> list[dict]:
    """Combine two frame lists chronologically and reindex."""
    merged = sorted([*primary, *pinned], key=lambda f: f["timestamp_seconds"])
    for i, frame in enumerate(merged):
        frame["index"] = i
    return merged
