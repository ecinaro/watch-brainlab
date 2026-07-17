#!/usr/bin/env python3
"""BrainWatch — video analysis entry point.

Downloads a video (or uses a local file), extracts frames, gets a transcript,
and prints a markdown report to stdout. The agent then reads each frame path
to see the images and combines them with the transcript to answer the user.

BrainLab community tool. Cross-platform, agent-agnostic, pure stdlib.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path


SCRIPT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(SCRIPT_DIR))

from media import (  # noqa: E402
    auto_fps,
    auto_fps_focus,
    download_video,
    extract_at_timestamps,
    extract_frames_keyframe,
    extract_frames_scene,
    fetch_captions_only,
    format_time,
    get_metadata,
    is_url,
    MAX_FPS,
    merge_frames,
    parse_time,
    parse_timestamps,
)
from transcript import (  # noqa: E402
    filter_range,
    format_transcript,
    load_whisper_key,
    parse_vtt,
    transcribe_with_whisper,
)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
CONFIG_DIR = Path.home() / ".config" / "brainwatch"
CONFIG_FILE = CONFIG_DIR / ".env"
DEFAULT_DETAIL = "balanced"
DETAILS = {"transcript", "efficient", "balanced", "token-burner"}
DETAIL_CAPS = {"transcript": None, "efficient": 50, "balanced": 100, "token-burner": None}


def _read_env_value(name: str) -> str | None:
    """Read a value from environment or config file."""
    value = os.environ.get(name)
    if value and value.strip():
        return value.strip()
    if not CONFIG_FILE.exists():
        return None
    try:
        for line in CONFIG_FILE.read_text(encoding="utf-8").splitlines():
            raw = line.strip()
            if not raw or raw.startswith("#") or "=" not in raw:
                continue
            key, _, val = raw.partition("=")
            val = val.strip()
            if len(val) >= 2 and val[0] in ('"', "'") and val[-1] == val[0]:
                val = val[1:-1]
            else:
                # Strip inline comment
                for i, ch in enumerate(val):
                    if ch == "#" and i > 0 and val[i - 1] in " \t":
                        val = val[:i].rstrip()
                        break
            if key.strip() == name:
                return val or None
    except OSError:
        pass
    return None


def _get_detail() -> str:
    detail = _read_env_value("WATCH_DETAIL") or DEFAULT_DETAIL
    return detail if detail in DETAILS else DEFAULT_DETAIL


def _frame_cap(detail: str) -> int | None:
    return DETAIL_CAPS.get(detail, 100)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(
        prog="brainwatch",
        description="BrainWatch: download video, extract frames, surface transcript.",
    )
    ap.add_argument("source", help="Video URL or local file path")
    ap.add_argument("--max-frames", type=int, default=None, help="Override frame cap")
    ap.add_argument("--resolution", type=int, default=512, help="Frame width px (default 512)")
    ap.add_argument("--fps", type=float, default=None, help="Override auto-fps")
    ap.add_argument(
        "--detail",
        choices=["transcript", "efficient", "balanced", "token-burner"],
        default=None,
        help="Fidelity dial: transcript | efficient | balanced | token-burner",
    )
    ap.add_argument(
        "--timestamps", type=str, default=None,
        help="Comma-separated timestamps to grab frames at (transcript cues)",
    )
    ap.add_argument("--start", type=str, default=None, help="Range start (SS, MM:SS, HH:MM:SS)")
    ap.add_argument("--end", type=str, default=None, help="Range end (SS, MM:SS, HH:MM:SS)")
    ap.add_argument("--out-dir", type=str, default=None, help="Working directory (default: tmp)")
    ap.add_argument("--no-whisper", action="store_true", help="Disable Whisper fallback")
    ap.add_argument(
        "--whisper", choices=["groq", "openai"], default=None,
        help="Force a specific Whisper backend",
    )
    ap.add_argument("--no-dedup", action="store_true", help="Keep near-duplicate frames")
    args = ap.parse_args()

    # Config
    detail = args.detail or _get_detail()
    configured_cap = _frame_cap(detail)
    max_frames = args.max_frames if args.max_frames is not None else configured_cap
    if max_frames is not None and max_frames < 1:
        raise SystemExit("--max-frames must be > 0")
    budget_cap = max_frames if max_frames is not None else 100
    cue_timestamps = parse_timestamps(args.timestamps)

    # Working directory
    if args.out_dir:
        work = Path(args.out_dir).expanduser().resolve()
    else:
        work = Path(tempfile.mkdtemp(prefix="brainwatch-"))
    work.mkdir(parents=True, exist_ok=True)
    print(f"[brainwatch] working dir: {work}", file=sys.stderr)

    # State
    url_source = is_url(args.source)
    dl: dict = {"subtitle_path": None, "info": {}, "downloaded": False}
    transcript_segments: list[dict] = []
    transcript_text: str | None = None
    transcript_source: str | None = None
    video_path: str | None = None

    # -----------------------------------------------------------------------
    # Phase 1: Captions (try to get them before downloading video)
    # -----------------------------------------------------------------------
    if url_source:
        print("[brainwatch] checking metadata/captions via yt-dlp…", file=sys.stderr)
        dl = fetch_captions_only(args.source, work / "download")
        if dl.get("subtitle_path"):
            try:
                transcript_segments = parse_vtt(dl["subtitle_path"])
                transcript_text = format_transcript(transcript_segments)
                transcript_source = "captions"
            except Exception as exc:
                print(f"[brainwatch] subtitle parse failed: {exc}", file=sys.stderr)
                transcript_segments = []

    # -----------------------------------------------------------------------
    # Phase 2: Download video (skip if transcript-only and we have captions)
    # -----------------------------------------------------------------------
    audio_only = detail == "transcript" and not cue_timestamps
    if detail == "transcript" and transcript_segments and not cue_timestamps:
        video_path = None
    else:
        if url_source:
            label = "audio" if audio_only else "video"
            print(f"[brainwatch] downloading {label} via yt-dlp…", file=sys.stderr)
            dl = download_video(args.source, work / "download", audio_only=audio_only)
        else:
            print("[brainwatch] using local file…", file=sys.stderr)
            dl = download_video(args.source, work / "download")
        video_path = dl.get("video_path")

    # -----------------------------------------------------------------------
    # Phase 3: Metadata & timing
    # -----------------------------------------------------------------------
    meta = get_metadata(video_path) if video_path else {
        "duration_seconds": float((dl.get("info") or {}).get("duration") or 0),
        "width": None, "height": None, "codec": None, "has_audio": False,
    }
    full_duration = meta["duration_seconds"]

    start_sec = parse_time(args.start)
    end_sec = parse_time(args.end)
    if start_sec is not None and start_sec < 0:
        raise SystemExit("--start must be non-negative")
    if end_sec is not None and start_sec is not None and end_sec <= start_sec:
        raise SystemExit("--end must be greater than --start")
    if full_duration > 0 and start_sec is not None and start_sec >= full_duration:
        raise SystemExit(f"--start {start_sec:.1f}s is past end of video ({full_duration:.1f}s)")

    effective_start = start_sec if start_sec is not None else 0.0
    effective_end = end_sec if end_sec is not None else full_duration
    effective_duration = max(0.0, effective_end - effective_start)
    focused = start_sec is not None or end_sec is not None

    if focused:
        fps, target = auto_fps_focus(effective_duration, max_frames=budget_cap)
    else:
        fps, target = auto_fps(effective_duration, max_frames=budget_cap)
    if args.fps is not None:
        fps = min(args.fps, MAX_FPS)
        target = max(1, int(round(fps * effective_duration)))

    if transcript_segments and focused:
        transcript_segments = filter_range(transcript_segments, start_sec, end_sec)
        transcript_text = format_transcript(transcript_segments)

    scope = (
        f"{format_time(effective_start)}-{format_time(effective_end)} ({effective_duration:.1f}s)"
        if focused else f"full {effective_duration:.1f}s"
    )

    # -----------------------------------------------------------------------
    # Phase 4: Frame extraction
    # -----------------------------------------------------------------------
    frames: list[dict] = []
    frame_meta: dict = {"engine": "none", "candidate_count": 0, "selected_count": 0, "fallback": False}
    cue_frames: list[dict] = []
    cue_meta: dict = {}

    # Transcript cues are extracted first and counted against the cap
    if cue_timestamps and video_path:
        cue_frames, cue_meta = extract_at_timestamps(
            video_path, work / "frames", cue_timestamps,
            resolution=args.resolution, max_frames=max_frames,
            start_seconds=start_sec, end_seconds=end_sec,
        )
        if cue_meta.get("dropped_out_of_window"):
            print(
                f"[brainwatch] {cue_meta['dropped_out_of_window']} cue timestamp(s) outside focus range — dropped",
                file=sys.stderr,
            )

    detail_budget = max_frames if max_frames is None else max(0, max_frames - len(cue_frames))
    if detail != "transcript" and video_path and detail_budget != 0:
        cap_label = "unlimited" if detail_budget is None else str(detail_budget)
        engine_label = "keyframes" if detail == "efficient" else "scene-aware frames"
        print(
            f"[brainwatch] extracting {engine_label} over {scope} "
            f"(target {target}, cap {cap_label})…",
            file=sys.stderr,
        )
        if detail == "efficient":
            frames, frame_meta = extract_frames_keyframe(
                video_path, work / "frames",
                resolution=args.resolution, max_frames=detail_budget,
                start_seconds=start_sec, end_seconds=end_sec,
                dedup=not args.no_dedup,
            )
        else:  # balanced, token-burner
            frames, frame_meta = extract_frames_scene(
                video_path, work / "frames",
                fps=fps, target_frames=target,
                resolution=args.resolution, max_frames=detail_budget,
                start_seconds=start_sec, end_seconds=end_sec,
                dedup=not args.no_dedup,
            )

    if cue_frames:
        frames = merge_frames(frames, cue_frames)

    # -----------------------------------------------------------------------
    # Phase 5: Transcript (fallback paths)
    # -----------------------------------------------------------------------
    if not transcript_segments and dl.get("subtitle_path"):
        try:
            all_segs = parse_vtt(dl["subtitle_path"])
            transcript_segments = filter_range(all_segs, start_sec, end_sec) if focused else all_segs
            transcript_text = format_transcript(transcript_segments)
            transcript_source = "captions"
        except Exception as exc:
            print(f"[brainwatch] subtitle parse failed: {exc}", file=sys.stderr)

    if not transcript_segments and not args.no_whisper and video_path and meta.get("has_audio"):
        backend, api_key = load_whisper_key(args.whisper)
        if backend and api_key:
            try:
                all_segs, used_backend = transcribe_with_whisper(
                    video_path, work / "audio.mp3",
                    backend=backend, api_key=api_key,
                )
                transcript_segments = filter_range(all_segs, start_sec, end_sec) if focused else all_segs
                transcript_text = format_transcript(transcript_segments)
                transcript_source = f"whisper ({used_backend})"
            except SystemExit as exc:
                print(f"[brainwatch] whisper fallback failed: {exc}", file=sys.stderr)
        else:
            hint = (
                f"--whisper {args.whisper} was set but the matching API key is missing"
                if args.whisper else
                "no subtitles and no Whisper API key found"
            )
            print(
                f"[brainwatch] {hint} — run setup.py to configure",
                file=sys.stderr,
            )
    elif not transcript_segments and video_path and not meta.get("has_audio"):
        print("[brainwatch] no audio stream — proceeding without transcription", file=sys.stderr)

    # -----------------------------------------------------------------------
    # Phase 6: Report
    # -----------------------------------------------------------------------
    info = dl.get("info") or {}

    print()
    print("# brainwatch: video report")
    print()
    print(f"- **Source:** {args.source}")
    if info.get("title"):
        print(f"- **Title:** {info['title']}")
    if info.get("uploader"):
        print(f"- **Uploader:** {info['uploader']}")
    print(f"- **Duration:** {format_time(full_duration)} ({full_duration:.1f}s)")
    if focused:
        print(
            f"- **Focus range:** {format_time(effective_start)} → {format_time(effective_end)} "
            f"({effective_duration:.1f}s)"
        )
    if meta.get("width") and meta.get("height"):
        print(f"- **Resolution:** {meta['width']}x{meta['height']} ({meta.get('codec') or 'unknown'})")
    range_mode = "focused" if focused else "full"
    print(f"- **Detail:** {detail}")

    detail_count = frame_meta.get("selected_count", 0)
    if detail != "transcript":
        cap_label = "unlimited" if detail_budget is None else str(detail_budget)
        engine = frame_meta.get("engine", "scene")
        fallback = " with uniform fallback" if frame_meta.get("fallback") else ""
        deduped = frame_meta.get("deduped_count", 0)
        dedup_note = f", {deduped} near-duplicate{'s' if deduped != 1 else ''} dropped" if deduped else ""
        print(
            f"- **Frames:** {detail_count} selected from {frame_meta.get('candidate_count', detail_count)} "
            f"candidates ({engine}{fallback}{dedup_note}, {range_mode} range, budget {target}, cap {cap_label})"
        )
    elif not cue_frames:
        print("- **Frames:** skipped (transcript detail)")

    if cue_frames:
        dropped = cue_meta.get("dropped_out_of_window", 0)
        drop_note = f", {dropped} dropped outside range" if dropped else ""
        print(
            f"- **Cue frames:** {len(cue_frames)} at transcript-flagged timestamps "
            f"(transcript-cue{drop_note})"
        )
    if frames:
        print(f"- **Frame size:** max {args.resolution}px wide, max 1998px tall")
    if transcript_segments:
        in_range = " in range" if focused else ""
        print(
            f"- **Transcript:** {len(transcript_segments)} segments{in_range} "
            f"(via {transcript_source or 'captions'})"
        )
    else:
        print("- **Transcript:** none available")

    if detail == "token-burner" and len(frames) > 250:
        print()
        print(
            f"> **Warning:** token-burner detail selected {len(frames)} frames. "
            "This may use a large number of image tokens."
        )

    if not focused and full_duration > 600 and detail not in ("transcript", "token-burner"):
        mins = int(full_duration // 60)
        print()
        print(
            f"> **Warning:** This is a {mins}-minute video. Frame coverage is sparse at this length "
            f"under `{detail}` detail. Re-run with `--start HH:MM:SS --end HH:MM:SS` to zoom into "
            "a section, or use `--detail token-burner` for full coverage."
        )

    print()
    print("## Frames")
    print()
    if frames:
        print(f"Frames live at: `{work / 'frames'}`")
        print()
        print(
            "**Read each frame path below to view the image.** "
            "Frames are chronological; `t=MM:SS` is the absolute timestamp."
        )
        print()
        for frame in frames:
            print(
                f"- `{frame['path']}` "
                f"(t={format_time(frame['timestamp_seconds'])}, reason={frame.get('reason', 'selected')})"
            )
    else:
        print("_No frames extracted._")

    print()
    print("## Transcript")
    print()
    if transcript_text:
        label = transcript_source or "captions"
        if focused:
            print(f"_Source: {label}. Filtered to {format_time(effective_start)} → {format_time(effective_end)}:_")
        else:
            print(f"_Source: {label}._")
        print()
        print("```")
        print(transcript_text)
        print("```")
    elif detail == "transcript":
        print(
            "_No transcript available. Captions missing and Whisper unavailable. "
            "Re-run with `--detail balanced` for frames._"
        )
    elif focused and dl.get("subtitle_path"):
        print(f"_No transcript lines inside {format_time(effective_start)} → {format_time(effective_end)}._")
    else:
        print(
            "_No transcript available — proceed with frames only. "
            "Run setup.py to enable Whisper, then re-run._"
        )

    print()
    print("---")
    print(f"_Work dir: `{work}` — delete when done._")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
