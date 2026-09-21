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
            "--no-part",
            "--no-playlist",
        ]

        # =========================
        # MP3
        # =========================

        if body.kind == "audio":

            command = common + [
                "--extract-audio",
                "--audio-format",
                "mp3",
                "--audio-quality",
                "192K",
                body.url,
            ]

            final_extension = ".mp3"

            set_job(
                job_id,
                progress=15,
            )

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
                    or "Audio download failed."
                )[-2500:]

                set_job(
                    job_id,
                    status="error",
                    progress=0,
                    error=error,
                )

                return

            source = find_output_file(job_id)

            if not source:
                set_job(
                    job_id,
                    status="error",
                    progress=0,
                    error="No output file was created.",
                )

                return

            target = DOWNLOADS / f"{job_id}.mp3"

            if source != target:
                if target.exists():
                    target.unlink()

                source.rename(target)

            set_job(
                job_id,
                status="finished",
                progress=100,
                filename=target.name,
            )

            return

        # =========================
        # VIDEO
        # =========================

        quality = body.quality

        if quality == "best":

            selector = (
                "bestvideo+bestaudio/"
                "best"
            )

            requested_height = None

        else:

            match = re.fullmatch(
                r"(\d+)p",
                quality,
            )

            if not match:
                raise ValueError(
                    "Invalid video quality."
                )

            requested_height = int(
                match.group(1)
            )

            if requested_height not in QUALITY_LADDER:
                raise ValueError(
                    "Invalid video quality."
                )

            # Probe the source so we know whether
            # a usable source exists at or below
            # the requested quality.
            info = probe(body.url)

            source_has_lower_format = (
                has_video_at_or_below(
                    info,
                    requested_height,
                )
            )

            if source_has_lower_format:

                # Prefer the closest source quality
                # at or below the requested height.
                selector = (
                    f"bestvideo[height<={requested_height}]"
                    "+bestaudio/"
                    f"best[height<={requested_height}]"
                )

            else:

                # No suitable lower source exists.
                # Download the best available source
                # and FFmpeg will create the exact
                # requested standard quality.
                selector = (
                    "bestvideo+bestaudio/"
                    "best"
                )

        command = common + [
            "--format",
            selector,
            "--merge-output-format",
            "mp4",
            body.url,
        ]

        set_job(
            job_id,
            progress=15,
        )

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
                or "Video download failed."
            )[-2500:]

            print("[YTDLP DOWNLOAD ERROR]")
            print(error)

            set_job(
                job_id,
                status="error",
                progress=0,
                error=error,
            )

            return

        source = find_output_file(job_id)

        if not source:

            set_job(
                job_id,
                status="error",
                progress=0,
                error="No output file was created.",
            )

            return

        target = DOWNLOADS / f"{job_id}.mp4"

                # =========================
        # EXACT QUALITY CONVERSION
        # =========================

                if requested_height is not None:

            source_info = probe(body.url)

            heights = actual_video_heights(source_info)

            source_height = max(heights) if heights else 0

            # If the downloaded source is already exactly
            # the requested quality, don't re-encode it.
            if source_height == requested_height:

                if source != target:

                    if target.exists():
                        target.unlink()

                    source.rename(target)

            else:

                # Convert to a temporary file first.
                temp_target = DOWNLOADS / f"{job_id}_converted.mp4"

                convert_video_to_quality(
                    source,
                    temp_target,
                    requested_height,
                )

                if source.exists():
                    source.unlink()

                temp_target.rename(target)

        else:

            if source != target:

                if target.exists():
                    target.unlink()

                source.rename(target)

        set_job(
            job_id,
            status="finished",
            progress=100,
            filename=target.name,
        )

    except subprocess.TimeoutExpired:

        set_job(
            job_id,
            status="error",
            progress=0,
            error=(
                "Download timed out. "
                "Please try again."
            ),
        )

    except Exception as error:

        print("[DOWNLOAD FAILED]")
        print(str(error))

        set_job(
            job_id,
            status="error",
            progress=0,
            error=str(error)[-2500:],
        )


# =========================
# HEALTH
# =========================

@app.get("/api/health")
def health():
    return {
        "ok": True,
        "name": "VGSAVE",
        "version": "1.4.0",
    }


# =========================
# DETECT
# =========================

@app.post("/api/detect")
@app.post("/formats")
def detect(body: DetectBody):

    if not public_url(body.url):
        raise HTTPException(
            400,
            "Enter a valid public http/https URL.",
        )

    try:

        info = probe(body.url)

        qualities = standard_qualities(
            info
        )

        return {
            "ok": True,
            "title": (
                info.get("title")
                or "Video"
            ),
            "uploader": (
                info.get("uploader")
                or ""
            ),
            "extractor": (
                info.get("extractor_key")
                or ""
            ),
            "video": qualities,
            "audio": ["mp3"],
        }

    except Exception as error:

        print("[DETECT FAILED]")
        print(str(error))

        raise HTTPException(
            422,
            "This public URL could not be processed right now.",
        )


# =========================
# DOWNLOAD
# =========================

@app.post("/api/download")
@app.post("/download")
def download(body: DownloadBody):

    if not public_url(body.url):
        raise HTTPException(
            400,
            "Enter a valid public http/https URL.",
        )

    if body.kind not in (
        "video",
        "audio",
    ):
        raise HTTPException(
            400,
            "Invalid download type.",
        )

    if body.kind == "video":

        if body.quality != "best":

            match = re.fullmatch(
                r"(\d+)p",
                body.quality,
            )

            if not match:
                raise HTTPException(
                    400,
                    "Invalid quality.",
                )

            requested = int(
                match.group(1)
            )

            if requested not in QUALITY_LADDER:
                raise HTTPException(
                    400,
                    "Invalid quality.",
                )

    job_id = uuid.uuid4().hex

    set_job(
        job_id,
        status="queued",
        progress=0,
    )

    threading.Thread(
        target=run_download,
        args=(job_id, body),
        daemon=True,
    ).start()

    return {
        "ok": True,
        "id": job_id,
    }


# =========================
# STATUS
# =========================

@app.get("/api/status/{job_id}")
@app.get("/status/{job_id}")
def status(job_id: str):

    with jobs_lock:
        job = jobs.get(job_id)

    if not job:
        raise HTTPException(
            404,
            "Job not found.",
        )

    return {
        "ok": True,
        **job,
    }


# =========================
# FILE
# =========================

@app.get("/api/file/{job_id}")
@app.get("/file/{job_id}")
def file(job_id: str):

    with jobs_lock:
        job = jobs.get(job_id)

    if not job:
        raise HTTPException(
            404,
            "Job not found.",
        )

    if job.get("status") != "finished":
        raise HTTPException(
            404,
            "File is not ready.",
        )

    path = (
        DOWNLOADS /
        job["filename"]
    )

    if not path.exists():
        raise HTTPException(
            404,
            "File no longer exists."
        )

    media_type = (
        "audio/mpeg"
        if path.suffix.lower() == ".mp3"
        else "video/mp4"
    )

    return FileResponse(
        path,
        media_type=media_type,
        filename=path.name,
    )


# =========================
# FRONTEND
# =========================

app.mount(
    "/",
    StaticFiles(
        directory=ROOT.parent / "public",
        html=True,
    ),
    name="public",
)
