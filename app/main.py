import json
import os
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent
DOWNLOADS = ROOT / "downloads"
DOWNLOADS.mkdir(exist_ok=True)

app = FastAPI(title="VGSAVE", version="1.0.0")
jobs = {}
jobs_lock = threading.Lock()

QUALITIES = [144, 240, 360, 480, 720, 1080, 1440, 2160]
POT_PROVIDER = os.getenv("POT_PROVIDER_URL", "").strip().rstrip("/")


class DetectBody(BaseModel):
    url: str = Field(min_length=8, max_length=4096)


class DownloadBody(BaseModel):
    url: str = Field(min_length=8, max_length=4096)
    kind: str = "video"
    quality: str = "best"


def public_url(value: str) -> bool:
    try:
        p = urlparse(value.strip())
        if p.scheme not in ("http", "https") or not p.netloc:
            return False
        host = p.hostname.lower()
        blocked = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
        return host not in blocked
    except Exception:
        return False


def base_ytdlp():
    args = [
        "yt-dlp",
        "--no-playlist",
        "--no-warnings",
        "--js-runtimes", "deno",
        "--remote-components", "ejs:npm",
    ]
    if POT_PROVIDER:
        args += [
            "--extractor-args",
            f"youtubepot-bgutilhttp:base_url={POT_PROVIDER}",
        ]
    return args


def probe(url: str):
    cmd = base_ytdlp() + [
        "--dump-single-json",
        "--skip-download",
        url,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or "Unsupported URL")[-2500:])
    return json.loads(r.stdout)


def available(info):
    found = set()
    for f in info.get("formats") or []:
        try:
            h = int(f.get("height") or 0)
            if h in QUALITIES and f.get("vcodec") not in (None, "none"):
                found.add(h)
        except Exception:
            pass
    return [f"{h}p" for h in QUALITIES if h in found]


def set_job(job_id, **values):
    with jobs_lock:
        jobs.setdefault(job_id, {}).update(values)


def run_download(job_id, body):
    try:
        set_job(job_id, status="processing", progress=5)

        out = str(DOWNLOADS / f"{job_id}.%(ext)s")
        common = base_ytdlp() + [
            "--output", out,
            "--no-part",
        ]

        if body.kind == "audio":
            cmd = common + [
                "--extract-audio",
                "--audio-format", "mp3",
                "--audio-quality", "192K",
                body.url,
            ]
            final_ext = ".mp3"
        else:
            quality = body.quality
            if quality == "best":
                selector = "bestvideo+bestaudio/best"
            else:
                h = int(re.sub(r"\D", "", quality))
                selector = f"bestvideo[height<={h}]+bestaudio/best[height<={h}]"
            cmd = common + [
                "--format", selector,
                "--merge-output-format", "mp4",
                body.url,
            ]
            final_ext = ".mp4"

        set_job(job_id, progress=15)
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)

        if r.returncode != 0:
            set_job(job_id, status="error", progress=0,
                    error=(r.stderr or r.stdout or "Download failed")[-1800:])
            return

        candidates = [
            p for p in DOWNLOADS.glob(f"{job_id}.*")
            if p.is_file() and p.suffix not in {".part", ".ytdl"}
        ]
        if not candidates:
            set_job(job_id, status="error", progress=0,
                    error="No output file was created.")
            return

        source = max(candidates, key=lambda p: p.stat().st_mtime)
        target = DOWNLOADS / f"{job_id}{final_ext}"
        if source != target:
            if target.exists():
                target.unlink()
            source.rename(target)

        set_job(job_id, status="finished", progress=100, filename=target.name)
    except subprocess.TimeoutExpired:
        set_job(job_id, status="error", progress=0,
                error="Download timed out. Please try again.")
    except Exception as exc:
        set_job(job_id, status="error", progress=0, error=str(exc)[-1800:])


@app.get("/api/health")
def health():
    return {"ok": True, "name": "VGSAVE", "version": "1.0.0"}


@app.post("/api/detect")
def detect(body: DetectBody):
    if not public_url(body.url):
        raise HTTPException(400, "Enter a valid public http/https URL.")
    try:
        info = probe(body.url)
        return {
            "ok": True,
            "title": info.get("title") or "Video",
            "uploader": info.get("uploader") or "",
            "extractor": info.get("extractor_key") or "",
            "video": available(info),
            "audio": ["mp3"],
        }
    except Exception:
        raise HTTPException(
            422,
            "This public URL could not be processed right now."
        )


@app.post("/api/download")
def download(body: DownloadBody):
    if not public_url(body.url):
        raise HTTPException(400, "Enter a valid public http/https URL.")
    if body.kind not in ("video", "audio"):
        raise HTTPException(400, "Invalid download type.")
    if body.kind == "video" and body.quality != "best":
        try:
            if int(re.sub(r"\D", "", body.quality)) not in QUALITIES:
                raise ValueError
        except Exception:
            raise HTTPException(400, "Invalid quality.")

    job_id = uuid.uuid4().hex
    set_job(job_id, status="queued", progress=0)
    threading.Thread(target=run_download, args=(job_id, body), daemon=True).start()
    return {"ok": True, "id": job_id}


@app.get("/api/status/{job_id}")
def status(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found.")
    return {"ok": True, **job}


@app.get("/api/file/{job_id}")
def file(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job or job.get("status") != "finished":
        raise HTTPException(404, "File is not ready.")
    path = DOWNLOADS / job["filename"]
    if not path.exists():
        raise HTTPException(404, "File no longer exists.")
    media = "audio/mpeg" if path.suffix == ".mp3" else "video/mp4"
    return FileResponse(path, media_type=media, filename=path.name)


app.mount("/", StaticFiles(directory=ROOT.parent / "public", html=True), name="public")
