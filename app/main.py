import json
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

app = FastAPI(title="VGSAVE", version="1.2.0")

jobs = {}
jobs_lock = threading.Lock()


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
    return [
        "yt-dlp",

        "--no-playlist",
        "--no-warnings",

        # JavaScript runtime for modern sites.
        "--js-runtimes",
        "deno",

        # Normal browser user agent.
        "--user-agent",
        (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),

        # Try normal YouTube clients.
        "--extractor-args",
        "youtube:player_client=android,web",
    ]


def probe(url: str):
    """
    Ask yt-dlp for complete information about the media.
    No media is downloaded here.
    """

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
    """
    Collect all video formats from the extractor.

    Different websites expose formats differently, so we check:
    - normal formats
    - requested_formats
    - top-level media information
    """

    formats = []

    normal_formats = info.get("formats") or []

    for item in normal_formats:
        if isinstance(item, dict):
            formats.append(item)

    requested_formats = info.get("requested_formats") or []

    for item in requested_formats:
        if isinstance(item, dict):
            formats.append(item)

    # Some extractors expose one direct video at top level.
    if info.get("url"):
        formats.append(info)

    return formats


def get_height(media_format):
    """
    Safely get the real video height.
    """

    try:
        height = media_format.get("height")

        if height:
            return int(height)

    except Exception:
        pass

    # Some extractors expose resolution like 1080x1920.
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

    # Other extractors can expose width/height as strings.
    try:
        height = media_format.get("video_height")

        if height:
            return int(height)

    except Exception:
        pass

    return 0


def is_video_format(media_format):
    """
    Check whether a format actually contains video.
    """

    vcodec = str(
        media_format.get("vcodec") or ""
    ).lower()

    if vcodec and vcodec != "none":
        return True

    # Direct media formats may not always expose vcodec.
    height = get_height(media_format)

    if height > 0:
        return True

    return False


def available_qualities(info):
    """
    Return ONLY the real video heights available from the source.

    Example:
        1080p
        720p
        480p
        360p

    No fake quality buttons are created.
    """

    heights = set()

    for media_format in collect_formats(info):

        if not is_video_format(media_format):
            continue

        height = get_height(media_format)

        if height > 0:
            heights.add(height)

    # Remove impossible values.
    heights = {
        h for h in heights
        if 1 <= h <= 10000
    }

    # Highest quality first.
    return [
        f"{height}p"
        for height in sorted(
            heights,
            reverse=True,
        )
    ]


def set_job(job_id, **values):
    with jobs_lock:
        jobs.setdefault(
            job_id,
            {}
        ).update(values)


def run_download(job_id, body):
    try:

        set_job(
            job_id,
            status="processing",
            progress=5,
        )

        output = str(
            DOWNLOADS /
            f"{job_id}.%(ext)s"
        )

        common = base_ytdlp() + [
            "--output",
            output,

            "--no-part",

            # Avoid playlist downloads.
            "--no-playlist",
        ]

        # -------------------------
        # MP3 DOWNLOAD
        # -------------------------

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

        # -------------------------
        # VIDEO DOWNLOAD
        # -------------------------

        else:

            quality = body.quality

            if quality == "best":

                selector = (
                    "bestvideo+bestaudio/"
                    "best"
                )

            else:

                match = re.fullmatch(
                    r"(\d+)p",
                    quality,
                )

                if not match:
                    raise ValueError(
                        "Invalid video quality."
                    )

                height = int(
                    match.group(1)
                )

                if height <= 0:
                    raise ValueError(
                        "Invalid video quality."
                    )

                # Prefer video + audio.
                # If separate streams are not
                # available, fall back to a
                # combined format.
                selector = (
                    f"bestvideo[height<={height}]"
                    "+bestaudio/"
                    f"best[height<={height}]"
                )

            command = common + [
                "--format",
                selector,

                "--merge-output-format",
                "mp4",

                body.url,
            ]

            final_extension = ".mp4"

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
                or "Download failed."
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

        # Find generated file.
        candidates = [
            path
            for path in DOWNLOADS.glob(
                f"{job_id}.*"
            )
            if (
                path.is_file()
                and path.suffix not in {
                    ".part",
                    ".ytdl",
                }
            )
        ]

        if not candidates:

            set_job(
                job_id,
                status="error",
                progress=0,
                error="No output file was created.",
            )

            return

        source = max(
            candidates,
            key=lambda path: path.stat().st_mtime,
        )

        target = (
            DOWNLOADS /
            f"{job_id}{final_extension}"
        )

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

        set_job(
            job_id,
            status="error",
            progress=0,
            error=str(error)[-2500:],
        )


# -------------------------
# HEALTH
# -------------------------

@app.get("/api/health")
def health():

    return {
        "ok": True,
        "name": "VGSAVE",
        "version": "1.2.0",
    }


# -------------------------
# DETECT MEDIA
# -------------------------

@app.post("/api/detect")
@app.post("/formats")
def detect(body: DetectBody):

    if not public_url(body.url):

        raise HTTPException(
            400,
            "Enter a valid public http/https URL.",
        )

    try:

        info = probe(
            body.url
        )

        qualities = available_qualities(
            info
        )

        return {
            "ok": True,

            "title":
                info.get("title")
                or "Video",

            "uploader":
                info.get("uploader")
                or "",

            "extractor":
                info.get("extractor_key")
                or "",

            # REAL qualities only.
            "video": qualities,

            # MP3 is always offered when
            # yt-dlp can process the media.
            "audio": ["mp3"],
        }

    except Exception as error:

        print("[DETECT FAILED]")
        print(str(error))

        raise HTTPException(
            422,
            "This public URL could not be processed right now.",
        )


# -------------------------
# START DOWNLOAD
# -------------------------

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

            if not re.fullmatch(
                r"\d+p",
                body.quality,
            ):

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

    thread = threading.Thread(
        target=run_download,
        args=(job_id, body),
        daemon=True,
    )

    thread.start()

    return {
        "ok": True,
        "id": job_id,
    }


# -------------------------
# DOWNLOAD STATUS
# -------------------------

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


# -------------------------
# DOWNLOAD FILE
# -------------------------

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
            "File no longer exists.",
        )

    if path.suffix == ".mp3":
        media_type = "audio/mpeg"
    else:
        media_type = "video/mp4"

    return FileResponse(
        path,
        media_type=media_type,
        filename=path.name,
    )


# -------------------------
# FRONTEND
# -------------------------

app.mount(
    "/",
    StaticFiles(
        directory=ROOT.parent / "public",
        html=True,
    ),
    name="public",
)
