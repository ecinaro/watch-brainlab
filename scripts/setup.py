#!/usr/bin/env python3
"""BrainWatch setup / preflight.

Modes:
  setup.py --check    Silent preflight. Exit 0 if ready, 2/3/4 on failure.
  setup.py --json     Machine-readable status.
  setup.py            Installer: scaffold config, print install hints.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path


REQUIRED_BINARIES = ["ffmpeg", "ffprobe", "yt-dlp"]
CONFIG_DIR = Path.home() / ".config" / "brainwatch"
CONFIG_FILE = CONFIG_DIR / ".env"

ENV_TEMPLATE = """# BrainWatch configuration
#
# Whisper transcription fallback — used when yt-dlp cannot get captions.
#
# Groq (preferred — faster, cheaper):  https://console.groq.com/keys
# OpenAI (fallback):                   https://platform.openai.com/api-keys
#
# Leave both blank to disable Whisper. Videos without captions → frames only.

GROQ_API_KEY=
OPENAI_API_KEY=

# Detail mode: transcript | efficient | balanced | token-burner
# WATCH_DETAIL=balanced
"""


def _which(name: str) -> str | None:
    return shutil.which(name)


def _check_binaries() -> list[str]:
    return [b for b in REQUIRED_BINARIES if not _which(b)]


def _read_env_key(name: str) -> str | None:
    value = os.environ.get(name)
    if value and value.strip():
        return value.strip()
    if not CONFIG_FILE.exists():
        return None
    try:
        for line in CONFIG_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, raw = line.partition("=")
            if key.strip() != name:
                continue
            raw = raw.strip()
            if len(raw) >= 2 and raw[0] in ('"', "'") and raw[-1] == raw[0]:
                raw = raw[1:-1]
            return raw or None
    except OSError:
        return None
    return None


def _have_api_key() -> tuple[bool, str | None]:
    if _read_env_key("GROQ_API_KEY"):
        return True, "groq"
    if _read_env_key("OPENAI_API_KEY"):
        return True, "openai"
    return False, None


def _is_first_run() -> bool:
    return _read_env_key("SETUP_COMPLETE") != "true"


def _get_detail() -> str:
    detail = (
        os.environ.get("WATCH_DETAIL")
        or _read_env_key("WATCH_DETAIL")
        or "balanced"
    )
    if detail not in ("transcript", "efficient", "balanced", "token-burner"):
        detail = "balanced"
    return detail


def _scaffold_env() -> bool:
    if CONFIG_FILE.exists():
        return False
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(ENV_TEMPLATE, encoding="utf-8")
    try:
        CONFIG_FILE.chmod(0o600)
    except OSError:
        pass
    return True


def _write_setup_complete() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    existing = ""
    if CONFIG_FILE.exists():
        existing = CONFIG_FILE.read_text(encoding="utf-8")
        for line in existing.splitlines():
            if line.strip().startswith("SETUP_COMPLETE="):
                return
        if existing and not existing.endswith("\n"):
            existing += "\n"
        CONFIG_FILE.write_text(existing + "SETUP_COMPLETE=true\n", encoding="utf-8")
    else:
        CONFIG_FILE.write_text(ENV_TEMPLATE + "\nSETUP_COMPLETE=true\n", encoding="utf-8")
    try:
        CONFIG_FILE.chmod(0o600)
    except OSError:
        pass


def _status() -> dict:
    missing = _check_binaries()
    has_key, backend = _have_api_key()
    setup_complete = not _is_first_run()
    if not missing and has_key:
        status = "ready"
    elif missing and not has_key:
        status = "needs_install_and_key"
    elif missing:
        status = "needs_install"
    else:
        status = "needs_key"
    can_proceed = (not missing) and (has_key or setup_complete)
    return {
        "status": status,
        "can_proceed": can_proceed,
        "first_run": not setup_complete,
        "setup_complete": setup_complete,
        "missing_binaries": missing,
        "whisper_backend": backend,
        "has_api_key": has_key,
        "config_file": str(CONFIG_FILE),
        "watch_detail": _get_detail(),
        "platform": platform.system(),
    }


def _install_hints(missing: list[str]) -> str:
    system = platform.system()
    parts: list[str] = []
    has_ffmpeg = any(b in ("ffmpeg", "ffprobe") for b in missing)
    has_ytdlp = "yt-dlp" in missing
    if system == "Darwin":
        if has_ffmpeg:
            parts.append("brew install ffmpeg")
        if has_ytdlp:
            parts.append("brew install yt-dlp")
    elif system == "Windows":
        if has_ffmpeg:
            parts.append("winget install Gyan.FFmpeg")
        if has_ytdlp:
            parts.append("winget install yt-dlp.yt-dlp  (or: pip install yt-dlp)")
    else:  # Linux
        if has_ffmpeg:
            parts.append("sudo apt install ffmpeg  (or: sudo dnf install ffmpeg)")
        if has_ytdlp:
            parts.append("pipx install yt-dlp  (or: pip install --user yt-dlp)")
    return "\n  ".join(parts)


def _try_auto_install(missing: list[str]) -> bool:
    """Auto-install via brew on macOS. Returns True if all resolved."""
    if platform.system() != "Darwin" or not _which("brew"):
        return False
    pkgs: list[str] = []
    if any(b in ("ffmpeg", "ffprobe") for b in missing):
        pkgs.append("ffmpeg")
    if "yt-dlp" in missing:
        pkgs.append("yt-dlp")
    if not pkgs:
        return True
    print(f"[brainwatch] running: brew install {' '.join(pkgs)}", file=sys.stderr)
    result = subprocess.run(["brew", "install", *pkgs])
    if result.returncode != 0:
        return False
    return not _check_binaries()


# ---------------------------------------------------------------------------
# CLI modes
# ---------------------------------------------------------------------------
def cmd_check() -> int:
    """Silent preflight. Exit 0 = ready, 2/3/4 = needs action."""
    s = _status()
    if s["can_proceed"]:
        return 0
    parts = []
    if s["missing_binaries"]:
        parts.append(f"missing: {', '.join(s['missing_binaries'])}")
    if not s["has_api_key"] and not s["setup_complete"]:
        parts.append("no Whisper API key")
    print(
        f"[brainwatch] setup incomplete ({'; '.join(parts)}). "
        f"Run: python {Path(__file__).resolve()}",
        file=sys.stderr,
    )
    if s["missing_binaries"] and not s["has_api_key"]:
        return 4
    if s["missing_binaries"]:
        return 2
    return 3


def cmd_json() -> int:
    """Machine-readable status."""
    json.dump(_status(), sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


def cmd_install() -> int:
    """Interactive installer."""
    missing = _check_binaries()
    if missing:
        if _try_auto_install(missing):
            print("[brainwatch] dependencies installed via brew", file=sys.stderr)
        else:
            print("[brainwatch] dependencies missing — please install:", file=sys.stderr)
            print(f"  {_install_hints(missing)}", file=sys.stderr)
            return 2

    created = _scaffold_env()
    if created:
        print(f"[brainwatch] created config: {CONFIG_FILE}")
    else:
        print(f"[brainwatch] config exists: {CONFIG_FILE}")

    has_key, backend = _have_api_key()
    if has_key:
        _write_setup_complete()
        print(f"[brainwatch] ready. whisper backend: {backend}")
        return 0

    print("")
    print("[brainwatch] optional: add a Whisper API key for transcription.")
    print("")
    print(f"  Edit {CONFIG_FILE} and set either:")
    print("    GROQ_API_KEY=...    (preferred — faster, cheaper)")
    print("    OPENAI_API_KEY=...  (fallback)")
    print("")
    print("  Without a key, videos without captions → frames only.")
    return 3


def main() -> int:
    if len(sys.argv) > 1:
        arg = sys.argv[1]
        if arg == "--check":
            return cmd_check()
        if arg == "--json":
            return cmd_json()
    return cmd_install()


if __name__ == "__main__":
    raise SystemExit(main())
