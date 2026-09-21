import json
import os
import re
import subprocess
import threading
import uuid
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


ROOT = Path(__file__).resolve().parent
DOWNLOADS = ROOT / "downloads"
DOWNLOADS.mkdir(exist_ok=True)

app = FastAPI(title="VGSAVE", version="1.4.0")

jobs = {}
jobs_lock = threading.Lock()

# Standard VGSAVE quality ladder.
QUALITY_LADDER = [
    144,
    240,
    360,
    480,
    720,
    1080,
    1440,
    2160,
]


class DetectBody(BaseModel):
    url: str = Field(min_length=8, max_length=4096)


class DownloadBody(BaseModel):
    url: str = Field(min_length=8, max_length=4096)
    kind: str = "video"
    quality: str = "best"


def public_url(value: str) -> bool:
    try:
        p = urlparse(value.strip())

        if p.scheme not in ("http", "https"):
            return False

        if not p.netloc:
            return False

        host = (p.hostname or "").lower()

        blocked = {
            "localhost",
            "127.0.0.1",
            "::1",
            "0.0.0.0",
        }

        return host not in blocked

    except Exception:
        return False


def base_ytdlp():
    command = [
        "yt-dlp",
        "--no-playlist",
        "--no-warnings",
        "--js-runtimes",
        "deno",
        "--user-agent",
        (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    ]

    pot_provider_url = os.getenv("POT_PROVIDER_URL")

    if pot_provider_url:
        command += [
            "--extractor-args",
            (
                "youtubepot-bgutilhttp:"
                f"base_url={pot_provider_url}"
            ),
        ]

    return command


def probe(url: str):
    command = base_ytdlp() + [
        "--dump-single-json",
        "--skip-download",
        url,
    ]

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=120,
    )

    if result.returncode != 0:
        error = (
            result.stderr
            or result.stdout
            or "Unsupported URL"
        )

        print("[YTDLP PROBE ERROR]")
        print(error)

        raise RuntimeError(error[-3000:])

    try:
        return json.loads(result.stdout)

    except json.JSONDecodeError:
        raise RuntimeError(
            "yt-dlp returned invalid media information."
        )


def collect_formats(info):
    formats = []

    for item in info.get("formats") or []:
        if isinstance(item, dict):
            formats.append(item)

    for item in info.get("requested_formats") or []:
        if isinstance(item, dict):
            formats.append(item)

    if info.get("url"):
        formats.append(info)

    return formats


def get_height(media_format):
    try:
        height = media_format.get("height")

        if height:
            return int(height)

    except Exception:
        pass

    try:
        height = media_format.get("video_height")

        if height:
            return int(height)

    except Exception:
        pass

    resolution = media_format.get("resolution")

    if resolution:
        match = re.search(
            r"x(\d+)",
            str(resolution),
        )

        if match:
            try:
                return int(match.group(1))
            except Exception:
                pass

    return 0


def is_video_format(media_format):
    vcodec = str(
        media_format.get("vcodec") or ""
    ).lower()

    if vcodec and vcodec != "none":
        return True

    return get_height(media_format) > 0


def actual_video_heights(info):
    heights = set()

    for media_format in collect_formats(info):

        if not is_video_format(media_format):
            continue

        height = get_height(media_format)

        if 1 <= height <= 10000:
            heights.add(height)

    return heights


def standard_qualities(info):
    """
    Convert the source maximum into VGSAVE's
    standard quality ladder.
    """

    heights = actual_video_heights(info)

    if not heights:
        return []

    maximum = max(heights)

    return [
        f"{quality}p"
        for quality in QUALITY_LADDER
        if quality <= maximum
    ]


def has_video_at_or_below(info, requested_height):
    """
    Check whether the source contains a video
    stream at or below the requested height.
    """

    for media_format in collect_formats(info):

        if not is_video_format(media_format):
            continue

        height = get_height(media_format)

        if 0 < height <= requested_height:
            return True

    return False


def set_job(job_id, **values):
    with jobs_lock:
        jobs.setdefault(
            job_id,
            {}
        ).update(values)


def find_output_file(job_id):
    candidates = [
        path
        for path in DOWNLOADS.glob(
            f"{job_id}.*"
        )
        if (
            path.is_file()
            and path.suffix.lower()
            not in {
                ".part",
                ".ytdl",
            }
        )
    ]

    if not candidates:
        return None

    return max(
        candidates,
        key=lambda path: path.stat().st_mtime,
    )


def get_file_video_height(source):
    """
    Read the actual height of the downloaded file.
    Returns 0 if it cannot be determined.
    """

    command = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=height",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(source),
    ]

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=30,
    )

    if result.returncode != 0:
        return 0

    try:
        return int(result.stdout.strip())
    except (TypeError, ValueError):
        return 0


def convert_video_to_quality(
    source,
    target,
    requested_height,
):
    """
    Use FFmpeg to create the exact requested
    VGSAVE quality.

    Width is automatically calculated while
    preserving the original aspect ratio.
    """

    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(source),
        "-vf",
        f"scale=-2:{requested_height}",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        str(target),
    ]

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=900,
    )

    if result.returncode != 0:
        error = (
            result.stderr
            or result.stdout
            or "FFmpeg conversion failed."
        )[-3000:]

        print("[FFMPEG ERROR]")
        print(error)

        raise RuntimeError(error)


def run_download(job_id, body):
    source = None

    try:
        set_job(
            job_id,
            status="processing",
            progress=5,
        )

        output = str(
            DOWNLOADS / f"{job_id}.%(ext)s"
        )

        common = base_ytdlp() + [
            "--output",
            output,
            "--no-part
