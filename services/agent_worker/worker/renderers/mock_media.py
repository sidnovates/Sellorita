"""Mock-mode media: a real FFmpeg encode of a simple brand-coloured animation.

MOCK_MODE skips the LLM and HyperFrames, but still runs a real encoder, so the pipeline
has the same shape as production (scratch dir -> encode -> upload -> cleanup), uses
real CPU (useful when load-testing KEDA scaling) and produces a file a browser can play.

The real renderers (HyperFrames for REEL, Playwright for STATIC_POST) replace these in
Phase 2 behind the same function signatures.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from common.settings import get_settings


class RenderError(RuntimeError):
    pass


def _ffmpeg_color(hex_color: str) -> str:
    return "0x" + hex_color.lstrip("#")


def _run(args: list[str], timeout: int = 120) -> None:
    try:
        subprocess.run(args, check=True, capture_output=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise RenderError(f"{args[0]} not found; install FFmpeg or set FFMPEG_BINARY") from exc
    except subprocess.CalledProcessError as exc:
        tail = exc.stderr.decode(errors="replace")[-500:]
        raise RenderError(f"ffmpeg exited with {exc.returncode}: {tail}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RenderError(f"ffmpeg timed out after {timeout}s") from exc


def _scene(primary: str, secondary: str, size: str, seconds: float) -> str:
    # Solid brand-colour background with a box in the secondary colour sliding down.
    # `t` (time in seconds) in drawbox's y expression animates it frame by frame.
    return (
        f"color=c={_ffmpeg_color(primary)}:s={size}:d={seconds}:r=30,"
        f"drawbox=x=(iw-400)/2:y=mod(t*500\\,ih-400):w=400:h=400:"
        f"color={_ffmpeg_color(secondary)}:t=fill"
    )


def render_mock_reel(out_path: Path, primary: str, secondary: str) -> Path:
    """1080x1920 H.264 MP4 (the Instagram Reel format)."""
    seconds = get_settings().mock_reel_seconds
    _run(
        [
            get_settings().ffmpeg_binary, "-v", "error", "-y",
            "-f", "lavfi", "-i", _scene(primary, secondary, "1080x1920", seconds),
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            # moov atom at the start: the browser can start playing before the download ends
            "-movflags", "+faststart",
            str(out_path),
        ]
    )  # fmt: skip
    return out_path


def render_mock_post(out_path: Path, primary: str, secondary: str) -> Path:
    """1080x1080 PNG (a square feed post)."""
    _run(
        [
            get_settings().ffmpeg_binary, "-v", "error", "-y",
            "-f", "lavfi", "-i", _scene(primary, secondary, "1080x1080", 1),
            "-frames:v", "1",
            str(out_path),
        ]
    )  # fmt: skip
    return out_path


def probe(path: Path) -> dict[str, Any]:
    """First video stream's codec, width, height (via ffprobe). Used by tests."""
    ffprobe = str(Path(get_settings().ffmpeg_binary).with_name("ffprobe"))
    result = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=codec_name,width,height", "-of", "json", str(path)],
        check=True, capture_output=True, timeout=30,
    )  # fmt: skip
    return json.loads(result.stdout)["streams"][0]
