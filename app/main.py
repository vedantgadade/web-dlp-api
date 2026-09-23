import html
import ipaddress
import json
import logging
import os
import re
import socket
import subprocess
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response
from starlette.background import BackgroundTask
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


ROOT = Path(__file__).resolve().parent
DOWNLOADS = ROOT / "downloads"
DOWNLOADS.mkdir(exist_ok=True)

app = FastAPI(title="VGSAVE", version="1.4.0", docs_url="/docs", redoc_url="/redoc")

# Allow the free Cloudflare Pages frontend to call the Railway API.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://vgsave.pages.dev"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)
logger = logging.getLogger("vgsave")

# Set VGSAVE_CANONICAL_ORIGIN (for example, https://your-domain.example) at launch.
# Until a final domain is chosen, the request origin is used rather than inventing one.
CANONICAL_ORIGIN = os.getenv("VGSAVE_CANONICAL_ORIGIN", "").rstrip("/")
DOWNLOAD_TTL_SECONDS = int(os.getenv("DOWNLOAD_TTL_SECONDS", "3600"))
FAILED_JOB_TTL_SECONDS = int(os.getenv("FAILED_JOB_TTL_SECONDS", "900"))
PROCESSING_JOB_TTL_SECONDS = int(os.getenv("PROCESSING_JOB_TTL_SECONDS", "3600"))
MAX_ACTIVE_JOBS = int(os.getenv("MAX_ACTIVE_JOBS", "2"))
MAX_REQUEST_BYTES = int(os.getenv("MAX_REQUEST_BYTES", "16384"))
RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "60"))
RATE_LIMIT_REQUESTS = int(os.getenv("RATE_LIMIT_REQUESTS", "20"))

jobs = {}
jobs_lock = threading.Lock()
rate_buckets = {}
active_file_reads = set()

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
    """Reject malformed and internal targets before giving a URL to yt-dlp.

    DNS is resolved once here to reject private answers. This protects direct and
    obvious SSRF attempts; a DNS rebind after validation remains an upstream
    downloader/process boundary, so deployments should also use network egress
    rules where available.
    """
    try:
        parsed = urlparse(value.strip())
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return False
        if parsed.username or parsed.password or parsed.port not in (80, 443, None):
            return False
        host = (parsed.hostname or "").rstrip(".").lower()
        if not host or host == "localhost" or host.endswith(".localhost") or ".local" in host:
            return False
        addresses = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        if not addresses:
            return False
        for address in addresses:
            ip = ipaddress.ip_address(address[4][0])
            if not ip.is_global:
                return False
        return True
    except (OSError, ValueError):
        return False


def cleanup_expired_jobs():
    """Bound temporary storage while preserving files currently being served."""
    now = time.time()
    remove = []
    with jobs_lock:
        for job_id, job in jobs.items():
            age = now - job.get("updated_at", job.get("created_at", now))
            status = job.get("status")
            ttl = DOWNLOAD_TTL_SECONDS if status == "finished" else FAILED_JOB_TTL_SECONDS
            if status in ("queued", "processing"):
                ttl = PROCESSING_JOB_TTL_SECONDS
            if age > ttl and job_id not in active_file_reads:
                remove.append((job_id, job.get("filename")))
        for job_id, _ in remove:
            jobs.pop(job_id, None)
    for job_id, filename in remove:
        for path in DOWNLOADS.glob(f"{job_id}.*"):
            if path.is_file():
                path.unlink(missing_ok=True)
        if filename:
            (DOWNLOADS / filename).unlink(missing_ok=True)


def active_job_count():
    with jobs_lock:
        return sum(job.get("status") in ("queued", "processing") for job in jobs.values())


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
            "--extractor-args",
            "youtube:player-client=mweb",
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
    """Create or update a job while maintaining its cleanup lifecycle timestamps."""
    now = time.time()
    with jobs_lock:
        job = jobs.setdefault(job_id, {})
        # `created_at` is immutable for the lifetime of a job; every first write
        # receives it, even when the caller only supplies a status/progress field.
        job.setdefault("created_at", now)
        job.update(values)
        # Always refresh this after caller values so it cannot become stale.
        job["updated_at"] = now


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
                    error="The download could not be completed. Try another public URL.",
                )

                return

            source = find_output_file(job_id)

            if not source:
                set_job(
                    job_id,
                    status="error",
                    progress=0,
                    error="The download did not produce a file. Please try again.",
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
                error="The download could not be completed. Try another public URL.",
            )

            return

        source = find_output_file(job_id)

        if not source:

            set_job(
                job_id,
                status="error",
                progress=0,
                error="The download did not produce a file. Please try again.",
            )

            return

        target = DOWNLOADS / f"{job_id}.mp4"

        # =========================
        # EXACT QUALITY CONVERSION
        # =========================

        if requested_height is not None:

            source_height = get_file_video_height(source)

            # Already exactly the requested quality.
            # No FFmpeg conversion is needed.
            if source_height == requested_height:

                if source != target:

                    if target.exists():
                        target.unlink()

                    source.rename(target)

            # Never upscale a smaller source.
            elif source_height > 0 and source_height < requested_height:

                if source != target:

                    if target.exists():
                        target.unlink()

                    source.rename(target)

            else:

                # Source is higher than requested.
                # Convert to a temporary file first.
                temp_target = (
                    DOWNLOADS /
                    f"{job_id}_converted.mp4"
                )

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
            error="The download could not be completed. Please try again.",
        )


# =========================
# SAFETY MIDDLEWARE
# =========================
RATE_LIMITED_ENDPOINTS = {
    ("POST", "/api/detect"),
    ("POST", "/api/download"),
    ("POST", "/formats"),
    ("POST", "/download"),
}


@app.middleware("http")
async def public_api_guard(request: Request, call_next):
    # Keep write requests small, without applying an arbitrary body limit to
    # normal reads such as file transfers.
    if request.method in ("POST", "PUT", "PATCH"):
        length = request.headers.get("content-length")
        try:
            too_large = length and int(length) > MAX_REQUEST_BYTES
        except ValueError:
            return Response("Invalid request size.", status_code=400)
        if too_large:
            return Response("Request body is too large.", status_code=413)

    # Detection and download creation start subprocess work. Status polling and
    # file responses deliberately bypass this bucket so active jobs are not
    # interrupted by the browser's normal polling/download behavior.
    if (request.method, request.url.path) in RATE_LIMITED_ENDPOINTS:
        client = request.client.host if request.client else "unknown"
        now = time.monotonic()
        bucket = rate_buckets.setdefault(client, [])
        bucket[:] = [stamp for stamp in bucket if now - stamp < RATE_LIMIT_WINDOW_SECONDS]
        if len(bucket) >= RATE_LIMIT_REQUESTS:
            return Response("Too many requests. Please wait and try again.", status_code=429)
        bucket.append(now)
    return await call_next(request)


# =========================
# HEALTH
# =========================
@app.get("/api/health")
def health():
    return {"ok": True, "name": "VGSAVE", "version": "1.4.0"}


# =========================
# DETECT
# =========================
@app.post("/api/detect")
@app.post("/formats")
def detect(body: DetectBody):
    cleanup_expired_jobs()
    if not public_url(body.url):
        raise HTTPException(400, "Enter a valid public http/https URL.")
    try:
        info = probe(body.url)
        return {"ok": True, "title": info.get("title") or "Video", "uploader": info.get("uploader") or "", "extractor": info.get("extractor_key") or "", "video": standard_qualities(info), "audio": ["mp3"]}
    except Exception:
        logger.exception("Media detection failed")
        raise HTTPException(422, "This public URL could not be processed right now.")


# =========================
# DOWNLOAD
# =========================
@app.post("/api/download")
@app.post("/download")
def download(body: DownloadBody):
    cleanup_expired_jobs()
    if not public_url(body.url):
        raise HTTPException(400, "Enter a valid public http/https URL.")
    if body.kind not in ("video", "audio"):
        raise HTTPException(400, "Invalid download type.")
    if body.kind == "video" and body.quality != "best":
        match = re.fullmatch(r"(\d+)p", body.quality)
        if not match or int(match.group(1)) not in QUALITY_LADDER:
            raise HTTPException(400, "Invalid quality.")
    if active_job_count() >= MAX_ACTIVE_JOBS:
        raise HTTPException(429, "Downloads are busy. Please wait a moment and try again.")
    job_id = uuid.uuid4().hex
    set_job(job_id, status="queued", progress=0)
    threading.Thread(target=run_download, args=(job_id, body), daemon=True).start()
    return {"ok": True, "id": job_id}


# =========================
# STATUS / FILE
# =========================
@app.get("/api/status/{job_id}")
@app.get("/status/{job_id}")
def status(job_id: str):
    cleanup_expired_jobs()
    with jobs_lock:
        job = jobs.get(job_id)
        safe_job = {key: value for key, value in (job or {}).items() if key not in {"created_at", "updated_at"}}
    if not job:
        raise HTTPException(404, "Job not found.")
    return {"ok": True, **safe_job}


def release_file(job_id):
    with jobs_lock:
        active_file_reads.discard(job_id)

@app.get("/api/file/{job_id}")
@app.get("/file/{job_id}")
def file(job_id: str):
    cleanup_expired_jobs()
    with jobs_lock:
        job = jobs.get(job_id)
        if job and job.get("status") == "finished":
            active_file_reads.add(job_id)
    if not job:
        raise HTTPException(404, "Job not found.")
    if job.get("status") != "finished":
        raise HTTPException(404, "File is not ready.")
    path = DOWNLOADS / job["filename"]
    if not path.is_file():
        release_file(job_id)
        raise HTTPException(404, "File no longer exists.")
    media_type = "audio/mpeg" if path.suffix.lower() == ".mp3" else "video/mp4"
    return FileResponse(path, media_type=media_type, filename=path.name, background=BackgroundTask(release_file, job_id))


# =========================
# PUBLIC PAGES AND SEO
# =========================
PUBLIC_PAGES = {
    "/": "index.html",
    "/about/": "about.html",
    "/contact/": "contact.html",
    "/privacy/": "privacy.html",
    "/terms/": "terms.html",
    "/dmca/": "dmca.html",
    "/disclaimer/": "disclaimer.html",
    "/faq/": "faq.html",
    "/guides/": "guides.html",
    "/guides/video-download/": "guides/video-download.html",
    "/guides/mp3/": "guides/mp3.html",
    "/guides/video-quality/": "guides/video-quality.html",
    "/guides/responsible-downloading/": "guides/responsible-downloading.html",
    "/guides/youtube-public-video-download/": "guides/youtube-public-video-download.html",
    "/guides/instagram-public-video-download/": "guides/instagram-public-video-download.html",
    "/guides/download-public-video-android/": "guides/download-public-video-android.html",
    "/guides/download-public-video-pc/": "guides/download-public-video-pc.html",
    "/guides/download-public-video-iphone/": "guides/download-public-video-iphone.html",
    "/guides/troubleshooting/": "guides/troubleshooting.html",
    "/guides/instagram-reels-downloader/": "guides/instagram-reels-downloader.html",
    "/guides/youtube-shorts-downloader/": "guides/youtube-shorts-downloader.html",
    "/guides/mp4-video-downloader/": "guides/mp4-video-downloader.html",
    "/guides/online-video-downloader/": "guides/online-video-downloader.html",
}

def canonical_origin(request: Request):
    return CANONICAL_ORIGIN or str(request.base_url).rstrip("/")

def render_page(request: Request, filename: str):
    source = (ROOT.parent / "public" / filename).read_text(encoding="utf-8")
    return HTMLResponse(source.replace("__CANONICAL_ORIGIN__", html.escape(canonical_origin(request), quote=True)))

@app.get("/robots.txt", include_in_schema=False)
def robots(request: Request):
    origin = canonical_origin(request)
    return PlainTextResponse(f"User-agent: *\nAllow: /\nDisallow: /api/\nDisallow: /docs\nDisallow: /redoc\nDisallow: /openapi.json\nSitemap: {origin}/sitemap.xml\n")

@app.get("/sitemap.xml", include_in_schema=False)
def sitemap(request: Request):
    origin = canonical_origin(request)
    urls = "".join(f"<url><loc>{html.escape(origin + path)}</loc></url>" for path in PUBLIC_PAGES)
    return Response(f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>', media_type="application/xml")

@app.get("/", include_in_schema=False)
@app.get("/about/", include_in_schema=False)
@app.get("/contact/", include_in_schema=False)
@app.get("/privacy/", include_in_schema=False)
@app.get("/terms/", include_in_schema=False)
@app.get("/dmca/", include_in_schema=False)
@app.get("/disclaimer/", include_in_schema=False)
@app.get("/faq/", include_in_schema=False)
def public_page(request: Request):
    return render_page(request, PUBLIC_PAGES[request.url.path])

app.mount("/assets", StaticFiles(directory=ROOT.parent / "public" / "assets"), name="assets")
