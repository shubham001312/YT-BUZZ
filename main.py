import os
import re
import sys
import time
import uuid
import json
import random
import shutil
import asyncio
import yt_dlp
from pathlib import Path
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException, BackgroundTasks, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
try:
    import httpx
except ImportError:
    httpx = None

# Directories and constants (defined early for use by background tasks)
DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)
COOKIES_DIR = Path("cookies")
COOKIES_DIR.mkdir(exist_ok=True)
COOKIES_FILE = os.environ.get("COOKIES_FILE", "")
COOKIE_ADMIN_KEY = os.environ.get("COOKIE_ADMIN_KEY", "")
PROFILE_DIR = Path("browser_profile")
COOKIE_META = COOKIES_DIR / "cookie_meta.json"
POT_TOKENS = COOKIES_DIR / "po_tokens.json"

# ── Keep-Alive Background Task ────────────────────────────
async def keep_alive():
    """Ping self every 10 minutes to prevent Render free-tier spin-down."""
    render_url = os.environ.get("RENDER_EXTERNAL_URL")
    if not render_url:
        return  # Skip keep-alive on local dev
    if not httpx:
        return
    while True:
        await asyncio.sleep(600)  # 10 minutes
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{render_url}/health")
                print(f"[keepalive] ping: {resp.status_code}")
        except Exception as e:
            print(f"[keepalive] ping failed: {e}")


# ── Cookie Auto-Refresh Background Task ──────────────────
async def auto_refresh_cookies():
    """Refresh cookies every 12 hours using Playwright persistent profile.
    
    This runs in the background and keeps cookies fresh automatically.
    Only runs if a browser profile exists (user has logged in at least once).
    """
    import json as _json
    from datetime import datetime, timezone

    while True:
        await asyncio.sleep(12 * 3600)  # 12 hours

        # Only refresh if browser profile exists
        if not PROFILE_DIR.exists():
            print("[cookie-refresh] No browser profile found, skipping.")
            continue

        # Check if cookies are stale (>7 days old)
        try:
            if COOKIE_META.exists():
                meta = _json.loads(COOKIE_META.read_text())
                last = meta.get("last_refresh")
                if last:
                    last_dt = datetime.fromisoformat(last)
                    age_days = (datetime.now(timezone.utc) - last_dt).days
                    if age_days < 7:
                        print(f"[cookie-refresh] Cookies are {age_days}d old, still fresh.")
                        continue
        except Exception:
            pass

        # Run the cookie refresher (async to avoid blocking event loop)
        try:
            print("[cookie-refresh] Starting cookie refresh...")
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "cookie_refresher.py", "refresh",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
                if proc.returncode == 0:
                    print(f"[cookie-refresh] Success: {stdout.decode().strip()}")
                else:
                    print(f"[cookie-refresh] Failed: {stderr.decode().strip()}")
            except asyncio.TimeoutError:
                proc.kill()
                print("[cookie-refresh] Timeout during cookie refresh.")
        except Exception as e:
            print(f"[cookie-refresh] Error: {e}")


# ── In-memory download jobs (job_id -> status dict) ─────
download_jobs: dict[str, dict] = {}
JOB_MAX_AGE = 3600  # Clean up jobs older than 1 hour


def _cleanup_old_jobs():
    """Remove download jobs older than JOB_MAX_AGE to prevent memory leak."""
    now = datetime.now(timezone.utc)
    expired = [
        jid for jid, j in download_jobs.items()
        if (now - datetime.fromisoformat(j.get("created", now.isoformat()))).total_seconds() > JOB_MAX_AGE
    ]
    for jid in expired:
        job = download_jobs.pop(jid, {})
        fp = job.get("filepath")
        if fp:
            shutil.rmtree(Path(fp).parent, ignore_errors=True)

# ── Restore cookies from env var on startup (persists across Render restarts)
def _restore_cookies_from_env():
    """If cookies.txt is missing but COOKIE_DATA env var is set, write it to disk.
    This ensures cookies survive Render restarts/spin-downs."""
    cookie_data = os.environ.get("COOKIE_DATA", "")
    if cookie_data and not COOKIES_DIR.joinpath("cookies.txt").exists():
        COOKIES_DIR.mkdir(exist_ok=True)
        COOKIES_DIR.joinpath("cookies.txt").write_text(cookie_data)
        print(f"[startup] Restored cookies from COOKIE_DATA env var ({len(cookie_data)} bytes)")


def _get_po_tokens() -> dict:
    """Load cached PO tokens from disk."""
    if POT_TOKENS.exists():
        try:
            tokens = json.loads(POT_TOKENS.read_text())
            # Check if tokens are stale (>1 hour old)
            generated = datetime.fromisoformat(tokens.get("generated_at", "2000-01-01"))
            if (datetime.now(timezone.utc) - generated).total_seconds() < 3600:
                return tokens
        except Exception:
            pass
    return {}


async def _periodic_po_token_refresh():
    """Refresh PO tokens every hour in the background."""
    while True:
        await asyncio.sleep(3600)  # 1 hour
        await _generate_po_tokens()


async def _generate_po_tokens():
    """Generate PO tokens using Playwright in background."""
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "po_token_generator.py", "generate",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        if proc.returncode == 0:
            print(f"[po-tokens] Generated successfully")
        else:
            print(f"[po-tokens] Failed: {stderr.decode().strip()[:200]}")
    except Exception as e:
        print(f"[po-tokens] Error: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Restore cookies from env on startup
    _restore_cookies_from_env()
    # Generate PO tokens on startup
    asyncio.create_task(_generate_po_tokens())
    # Start background tasks on startup
    keep_alive_task = asyncio.create_task(keep_alive())
    cookie_refresh_task = asyncio.create_task(auto_refresh_cookies())
    pot_refresh_task = asyncio.create_task(_periodic_po_token_refresh())
    yield
    # Cancel on shutdown
    keep_alive_task.cancel()
    cookie_refresh_task.cancel()
    pot_refresh_task.cancel()

app = FastAPI(title="YT Buzz Downloader", lifespan=lifespan)

def _get_cookies_path() -> str | None:
    """Return a cookies file path from the server's preset cookies directory.
    
    Place cookies.txt files in the cookies/ directory on the server.
    The server randomly picks one for each request to distribute load.
    """
    if COOKIES_FILE and Path(COOKIES_FILE).exists():
        return COOKIES_FILE
    cookie_files = list(COOKIES_DIR.glob("*.txt"))
    if not cookie_files:
        return None
    chosen = random.choice(cookie_files)
    return str(chosen)

app.mount("/static", StaticFiles(directory="static"), name="static")


YOUTUBE_RE = re.compile(r'(youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/)')

# Android/mobile clients work best on cloud servers (bypass PO Token/SABR).
# Web clients fail on datacenter IPs due to YouTube bot detection.
ANDROID_CLIENTS = [["android"], ["android_vr"], ["ios"]]
WEB_CLIENTS = [["web"], ["web_creator"], ["mweb"], ["tv_embedded"]]
ALL_CLIENTS = ANDROID_CLIENTS + WEB_CLIENTS

def _clean_url(url: str) -> str:
    """Strip Mix/radio playlist params (list=RDM...) from URLs.
    These auto-generated playlists can't be downloaded and confuse yt-dlp.
    """
    list_match = re.search(r'[?&]list=(RD[\w-]*)', url)
    if list_match:
        url = re.sub(r'[?&]list=RD[\w-]*', '', url)
        url = url.rstrip('?&')
    return url

def _format_size(size_bytes: int | None) -> str:
    if not size_bytes:
        return "Unknown"
    for unit in ["B", "KB", "MB", "GB"]:
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


def _cleanup_dir(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def _clean_formats(formats: list[dict]) -> list[dict]:
    """Extract and clean only the useful format info."""
    seen = set()
    cleaned = []

    for f in formats:
        fmt_id = f.get("format_id", "")
        ext = f.get("ext", "")
        vcodec = f.get("vcodec", "none")
        acodec = f.get("acodec", "none")
        height = f.get("height")
        width = f.get("width")
        fps = f.get("fps")
        abr = f.get("abr")
        tbr = f.get("tbr")
        filesize = f.get("filesize") or f.get("filesize_approx")
        note = f.get("format_note", "")
        proto = f.get("protocol", "")

        # Skip non-standard protocols
        if proto not in ("https", "http", "m3u8_native", "m3u8"):
            continue

        is_video = vcodec != "none"
        is_audio = acodec != "none" and not is_video

        if is_video and height:
            label = f"{height}p"
            if fps and fps > 30:
                label += f" {int(fps)}fps"
        elif is_audio:
            label = f"Audio {abr:.0f}kbps" if abr else f"Audio {note}"
        else:
            label = note or fmt_id

        dedup_key = f"{label}_{ext}"
        if dedup_key in seen:
            continue
        seen.add(dedup_key)

        cleaned.append({
            "format_id": fmt_id,
            "ext": ext,
            "label": label,
            "quality": height or 0,
            "filesize": _format_size(filesize),
            "filesize_bytes": filesize or 0,
            "is_video": is_video,
            "is_audio": is_audio,
            "vcodec": vcodec if vcodec != "none" else None,
            "acodec": acodec if acodec != "none" else None,
            "fps": fps,
            "tbr": tbr,
        })

    # Sort: video by quality desc, audio by bitrate desc
    cleaned.sort(key=lambda x: (x["is_video"], x["quality"] or x["tbr"] or 0), reverse=True)
    return cleaned


@app.get("/health")
async def health():
    """Health check endpoint for keep-alive pings and Render health checks."""
    return JSONResponse({"status": "ok", "service": "yt-buzz"})


@app.get("/", response_class=HTMLResponse)
async def root():
    return FileResponse("static/index.html")


@app.get("/api/info")
async def video_info(url: str):
    """Fetch video metadata and available formats.
    
    Server-optimized strategy (cloud servers get bot-detected by YouTube):
    1. Try cookies + android clients first (most reliable on datacenter IPs)
    2. Fall back to web clients without cookies (for non-restricted videos)
    3. Last resort: Invidious proxy
    """
    if not url or not YOUTUBE_RE.search(url):
        raise HTTPException(status_code=400, detail="Invalid YouTube URL")

    url = _clean_url(url)
    last_error = None
    cookies_path = _get_cookies_path()

    def _info_response(info):
        formats = _clean_formats(info.get("formats", []))
        return {
            "title": info.get("title", "Unknown"),
            "thumbnail": info.get("thumbnail", ""),
            "duration": info.get("duration", 0),
            "uploader": info.get("uploader", "Unknown"),
            "view_count": info.get("view_count", 0),
            "upload_date": info.get("upload_date", ""),
            "description": (info.get("description", "") or "")[:300],
            "formats": formats,
        }

    # Run yt-dlp in thread pool to avoid blocking the event loop.
    # Fast strategy: try android first (bypasses SABR), then web clients.
    # Render free tier has ~30s request timeout. Use time checks internally
    # to bail out before Render kills the connection.
    MAX_TIME = 22  # seconds — leave margin for Render's ~30s proxy timeout
    po_tokens = _get_po_tokens()  # Load PO tokens if available
    def _try_extract():
        nonlocal last_error
        start = time.time()
        # Layer 1: android client WITHOUT cookies (bypasses SABR/PO Token)
        for clients in [["android"], ["ios"], ["web"], ["web_creator"]]:
            if time.time() - start > MAX_TIME:
                last_error = Exception("Request timed out")
                break
            try:
                ydl_opts = {
                    "quiet": True,
                    "no_warnings": True,
                    "skip_download": True,
                    "ignore_no_formats_error": True,
                    "extractor_args": {"youtube": {"player_client": clients}},
                }
                # Add PO token if available
                if po_tokens.get("po_token") and po_tokens.get("visitor_data"):
                    ydl_opts["extractor_args"]["youtube"]["po_token"] = po_tokens["po_token"]
                    ydl_opts["extractor_args"]["youtube"]["po_visitor_data"] = po_tokens["visitor_data"]
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                if info and info.get("formats"):
                    resp = _info_response(info)
                    if resp["formats"]:
                        return resp
                last_error = Exception(f"No formats available via {clients[0]} client")
            except Exception as e:
                last_error = e
                continue
        # Layer 2: cookies + android (for age-restricted content)
        if cookies_path and time.time() - start < MAX_TIME:
            for clients in [["android"], ["ios"]]:
                if time.time() - start > MAX_TIME:
                    break
                try:
                    ydl_opts = {
                        "quiet": True,
                        "no_warnings": True,
                        "skip_download": True,
                        "ignore_no_formats_error": True,
                        "extractor_args": {"youtube": {"player_client": clients}},
                        "cookiefile": cookies_path,
                    }
                    # Add PO token if available
                    if po_tokens.get("po_token") and po_tokens.get("visitor_data"):
                        ydl_opts["extractor_args"]["youtube"]["po_token"] = po_tokens["po_token"]
                        ydl_opts["extractor_args"]["youtube"]["po_visitor_data"] = po_tokens["visitor_data"]
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        info = ydl.extract_info(url, download=False)
                    if info and info.get("formats"):
                        resp = _info_response(info)
                        if resp["formats"]:
                            return resp
                except Exception as e:
                    last_error = e
                    continue
        return None

    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(_try_extract), timeout=28
        )
    except asyncio.TimeoutError:
        result = None
        last_error = Exception("Request timed out — YouTube may be rate-limiting this server")
    if result is not None:
        return result

    # Layer 3: Try Invidious proxy as last resort
    video_id_match = re.search(r'(?:v=|youtu\.be/|shorts/)([\w-]+)', url)
    if video_id_match and httpx:
        video_id = video_id_match.group(1)
        invidious_instances = [
            "https://inv.nadeko.net",
            "https://invidious.nerdvpn.de",
            "https://invidious.privacyredirect.com",
            "https://vid.puffyan.us",
        ]
        for instance in invidious_instances:
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.get(f"{instance}/api/v1/videos/{video_id}")
                    if resp.status_code == 200:
                        data = resp.json()
                        formats = []
                        seen = set()
                        for f in data.get("formatStreams", []):
                            label = f.get("resolution", "Unknown")
                            key = f"{label}_{f.get('container', 'mp4')}"
                            if key not in seen:
                                seen.add(key)
                                try:
                                    quality = int(re.sub(r'[^0-9]', '', label) or 0)
                                except ValueError:
                                    quality = 0
                                formats.append({
                                    "format_id": f.get("itag", ""),
                                    "ext": f.get("container", "mp4"),
                                    "label": label,
                                    "quality": quality,
                                    "filesize": "Unknown",
                                    "filesize_bytes": 0,
                                    "is_video": True,
                                    "is_audio": False,
                                    "vcodec": None,
                                    "acodec": None,
                                    "fps": None,
                                    "tbr": None,
                                })
                        for f in data.get("adaptiveFormats", []):
                            is_vid = "video" in f.get("type", "")
                            is_aud = "audio" in f.get("type", "")
                            label = f.get("resolution", "Audio") if is_vid else f"Audio {f.get('bitrate', '')}kbps"
                            key = f"{label}_{f.get('container', 'mp4')}"
                            if key not in seen:
                                seen.add(key)
                                try:
                                    quality = int(re.sub(r'[^0-9]', '', f.get("resolution", "") or "") or 0)
                                except ValueError:
                                    quality = 0
                                formats.append({
                                    "format_id": f.get("itag", ""),
                                    "ext": f.get("container", "mp4"),
                                    "label": label,
                                    "quality": quality,
                                    "filesize": "Unknown",
                                    "filesize_bytes": 0,
                                    "is_video": is_vid,
                                    "is_audio": is_aud,
                                    "vcodec": None,
                                    "acodec": None,
                                    "fps": None,
                                    "tbr": f.get("bitrate"),
                                })
                        formats.sort(key=lambda x: (x["is_video"], x["quality"]), reverse=True)
                        return {
                            "title": data.get("title", "Unknown"),
                            "thumbnail": data.get("thumbnail", ""),
                            "duration": data.get("lengthSeconds", 0),
                            "uploader": data.get("author", "Unknown"),
                            "view_count": data.get("viewCount", 0),
                            "upload_date": "",
                            "description": (data.get("description", "") or "")[:300],
                            "formats": formats,
                            "source": "invidious",
                            "video_id": video_id,
                        }
            except Exception:
                continue

    # All layers failed
    error_msg = str(last_error or "Unknown error")
    if "Video unavailable" in error_msg or "Private video" in error_msg:
        error_msg = "This video is unavailable, private, or has been removed."
    elif "confirm your age" in error_msg:
        error_msg = "This video is age-restricted and requires YouTube login to access."
    elif "Signature extraction failed" in error_msg:
        error_msg = "This video uses a protection that yt-dlp cannot currently bypass."
    elif "no longer supported" in error_msg:
        error_msg = "YouTube API changed. The server needs a yt-dlp update."
    elif "format" in error_msg.lower():
        error_msg = "This video has format restrictions that prevent downloading from a server."
    else:
        error_msg = error_msg[:300]
    raise HTTPException(status_code=400, detail=f"Could not fetch video info: {error_msg}")


def _do_download(url: str, format_id: str, ext: str, download_type: str,
                  raw_format: str, job_id: str) -> None:
    """Synchronous download worker — runs in a thread pool to avoid blocking.
    Updates download_jobs[job_id] with progress/completion.
    """
    output_dir = DOWNLOAD_DIR / job_id
    output_dir.mkdir(exist_ok=True)
    output_template = str(output_dir / "%(title)s.%(ext)s")
    job = download_jobs[job_id]

    try:
        if raw_format:
            fmt_selector = raw_format
        elif download_type == "audio":
            fmt_selector = f"{format_id}/bestaudio/best"
        else:
            fmt_selector = f"{format_id}+bestaudio/best"

        postprocessors = []
        if download_type == "audio" and ext == "mp3":
            postprocessors.append({
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            })

        cookies_path = _get_cookies_path()

        def _clean():
            for f in output_dir.iterdir():
                f.unlink(missing_ok=True)

        def _opts(fmt, clients, use_cookies=False):
            opts = {
                "format": fmt,
                "outtmpl": output_template,
                "quiet": True,
                "no_warnings": True,
                "no_cache_dir": True,
                "merge_output_format": ext if ext != "mp3" else None,
                "postprocessors": list(postprocessors),
                "extractor_args": {"youtube": {"player_client": clients}},
                "concurrent_fragment_downloads": 2,
                "http_chunk_size": 1048576,
                "socket_timeout": 60,
            }
            if use_cookies and cookies_path:
                opts["cookiefile"] = cookies_path
            return opts

        info = None
        job["progress"] = "Downloading..."
        po_tokens = _get_po_tokens()  # Load PO tokens if available

        def _add_po_opts(opts_dict):
            """Add PO token args to yt-dlp options if available."""
            if po_tokens.get("po_token") and po_tokens.get("visitor_data"):
                ea = opts_dict.setdefault("extractor_args", {"youtube": {}})
                ea["youtube"]["po_token"] = po_tokens["po_token"]
                ea["youtube"]["po_visitor_data"] = po_tokens["visitor_data"]

        # Layer 1: android without cookies (bypasses SABR, works for public videos)
        for clients in [["android"], ["ios"], ["web"], ["web_creator"]]:
            try:
                _clean()
                opts = _opts(fmt_selector, clients)
                _add_po_opts(opts)
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                if info:
                    break
            except Exception:
                continue

        # Layer 2: android+cookies (age-restricted bypass)
        if info is None and cookies_path:
            for clients in [["android"], ["ios"]]:
                try:
                    _clean()
                    opts = _opts(fmt_selector, clients, use_cookies=True)
                    _add_po_opts(opts)
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        info = ydl.extract_info(url, download=True)
                    if info:
                        break
                except Exception:
                    continue

        # Layer 3: best format, all clients (final fallback)
        if info is None:
            for clients in ALL_CLIENTS:
                try:
                    _clean()
                    opts = _opts("bestvideo+bestaudio/best", clients, use_cookies=bool(cookies_path))
                    _add_po_opts(opts)
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        info = ydl.extract_info(url, download=True)
                    if info:
                        break
                except Exception:
                    continue

        if info is None:
            job["status"] = "error"
            job["error"] = "Download failed: unable to fetch video"
            shutil.rmtree(output_dir, ignore_errors=True)
            return

        files = list(output_dir.iterdir())
        if not files:
            job["status"] = "error"
            job["error"] = "Download failed - no file produced"
            shutil.rmtree(output_dir, ignore_errors=True)
            return

        downloaded_file = files[0]
        job["status"] = "done"
        job["progress"] = "Complete"
        job["filename"] = downloaded_file.name
        job["filepath"] = str(downloaded_file)
        job["safe_filename"] = re.sub(r'[/\\:*?"<>|]', '_', downloaded_file.name)

    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)[:300]
        shutil.rmtree(output_dir, ignore_errors=True)


@app.post("/api/download-start")
async def download_start(request: Request):
    """Start a download job in the background. Returns job_id for polling."""
    body = await request.json()
    url = body.get("url", "")
    format_id = body.get("format_id", "")
    ext = body.get("ext", "mp4")
    download_type = body.get("download_type", "video")
    raw_format = body.get("raw_format", "")

    if not url or not YOUTUBE_RE.search(url):
        raise HTTPException(status_code=400, detail="Invalid YouTube URL")

    url = _clean_url(url)
    job_id = uuid.uuid4().hex[:12]
    download_jobs[job_id] = {
        "status": "downloading",
        "progress": "Starting...",
        "url": url,
        "created": datetime.now(timezone.utc).isoformat(),
    }

    # Run download in thread pool to avoid blocking the event loop
    _cleanup_old_jobs()  # Prevent memory leak
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, _do_download, url, format_id, ext, download_type, raw_format, job_id)

    return JSONResponse({"job_id": job_id, "status": "downloading"})


@app.get("/api/download-status/{job_id}")
async def download_status(job_id: str):
    """Poll download job status."""
    job = download_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return JSONResponse({
        "status": job.get("status", "unknown"),
        "progress": job.get("progress", ""),
        "error": job.get("error"),
    })


@app.get("/api/download-file/{job_id}")
async def download_file(job_id: str, background_tasks: BackgroundTasks = None):
    """Serve the completed download file."""
    job = download_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.get("status") != "done":
        raise HTTPException(status_code=400, detail=f"Job not ready (status: {job.get('status')})")

    filepath = job.get("filepath", "")
    if not filepath or not Path(filepath).exists():
        raise HTTPException(status_code=404, detail="File not found")

    if background_tasks:
        background_tasks.add_task(_cleanup_dir, Path(filepath).parent)
    # Clean up job entry after serving
    download_jobs.pop(job_id, None)

    return FileResponse(
        path=filepath,
        filename=job.get("safe_filename", "download.mp4"),
        media_type="application/octet-stream",
    )


@app.get("/api/download")
async def download_video(url: str, format_id: str, ext: str = "mp4", download_type: str = "video", raw_format: str = "", background_tasks: BackgroundTasks = None):
    """Download and serve a video file (synchronous fallback for backward compat).
    
    Prefer using /api/download-start + /api/download-status + /api/download-file
    for a non-blocking experience on Render.
    """
    if not url or not YOUTUBE_RE.search(url):
        raise HTTPException(status_code=400, detail="Invalid YouTube URL")

    url = _clean_url(url)

    job_id = uuid.uuid4().hex[:12]
    output_dir = DOWNLOAD_DIR / job_id
    output_dir.mkdir(exist_ok=True)
    output_template = str(output_dir / "%(title)s.%(ext)s")

    try:
        if raw_format:
            fmt_selector = raw_format
        elif download_type == "audio":
            fmt_selector = f"{format_id}/bestaudio/best"
        else:
            fmt_selector = f"{format_id}+bestaudio/best"

        postprocessors = []
        if download_type == "audio" and ext == "mp3":
            postprocessors.append({
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            })

        cookies_path = _get_cookies_path()

        def _clean():
            for f in output_dir.iterdir():
                f.unlink(missing_ok=True)

        def _opts(fmt, clients, use_cookies=False):
            opts = {
                "format": fmt,
                "outtmpl": output_template,
                "quiet": True,
                "no_warnings": True,
                "no_cache_dir": True,
                "merge_output_format": ext if ext != "mp3" else None,
                "postprocessors": list(postprocessors),
                "extractor_args": {"youtube": {"player_client": clients}},
                "concurrent_fragment_downloads": 2,
                "http_chunk_size": 1048576,
                "socket_timeout": 60,
            }
            if use_cookies and cookies_path:
                opts["cookiefile"] = cookies_path
            return opts

        # Run yt-dlp in thread pool to not block event loop
        def _run_download():
            po_tokens = _get_po_tokens()
            def _add_po(opts_dict):
                if po_tokens.get("po_token") and po_tokens.get("visitor_data"):
                    ea = opts_dict.setdefault("extractor_args", {"youtube": {}})
                    ea["youtube"]["po_token"] = po_tokens["po_token"]
                    ea["youtube"]["po_visitor_data"] = po_tokens["visitor_data"]
            # Layer 1: android without cookies (bypasses SABR)
            for clients in [["android"], ["ios"], ["web"], ["web_creator"]]:
                try:
                    _clean()
                    opts = _opts(fmt_selector, clients)
                    _add_po(opts)
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        info = ydl.extract_info(url, download=True)
                    if info:
                        return info
                except Exception:
                    continue
            # Layer 2: android+cookies (age-restricted)
            if cookies_path:
                for clients in [["android"], ["ios"]]:
                    try:
                        _clean()
                        opts = _opts(fmt_selector, clients, use_cookies=True)
                        _add_po(opts)
                        with yt_dlp.YoutubeDL(opts) as ydl:
                            info = ydl.extract_info(url, download=True)
                        if info:
                            return info
                    except Exception:
                        continue
            # Layer 3: best format, all clients
            for clients in ALL_CLIENTS:
                try:
                    _clean()
                    opts = _opts("bestvideo+bestaudio/best", clients, use_cookies=bool(cookies_path))
                    _add_po(opts)
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        info = ydl.extract_info(url, download=True)
                    if info:
                        return info
                except Exception:
                    continue
            return None

        info = await asyncio.to_thread(_run_download)

        if info is None:
            shutil.rmtree(output_dir, ignore_errors=True)
            raise HTTPException(status_code=500, detail="Download failed: unable to fetch video with any method")

        files = list(output_dir.iterdir())
        if not files:
            shutil.rmtree(output_dir, ignore_errors=True)
            raise HTTPException(status_code=500, detail="Download failed — no file produced")

        downloaded_file = files[0]
        safe_filename = re.sub(r'[/\\:*?"<>|]', '_', downloaded_file.name)
        if background_tasks:
            background_tasks.add_task(_cleanup_dir, output_dir)

        return FileResponse(
            path=str(downloaded_file),
            filename=safe_filename,
            media_type="application/octet-stream",
        )

    except HTTPException:
        shutil.rmtree(output_dir, ignore_errors=True)
        raise
    except Exception as e:
        shutil.rmtree(output_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=f"Download failed: {str(e)}")


@app.get("/api/download-invidious")
async def download_invidious(video_id: str, itag: str, ext: str = "mp4", background_tasks: BackgroundTasks = None):
    """Download video via Invidious proxy (fallback for age-restricted videos)."""
    if not httpx:
        raise HTTPException(status_code=500, detail="Invidious proxy not available (httpx not installed)")

    invidious_instances = [
        "https://inv.nadeko.net",
        "https://invidious.nerdvpn.de",
        "https://invidious.privacyredirect.com",
        "https://vid.puffyan.us",
    ]

    # Find working instance and download
    for instance in invidious_instances:
        try:
            download_url = f"{instance}/latest_version?id={video_id}&itag={itag}"
            async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
                resp = await client.get(download_url)
                if resp.status_code == 200 and len(resp.content) > 1000:
                    job_id = uuid.uuid4().hex[:12]
                    out_path = DOWNLOAD_DIR / job_id
                    out_path.mkdir(exist_ok=True)
                    # Get filename from Content-Disposition or default
                    disposition = resp.headers.get("content-disposition", "")
                    filename = f"video.{ext}"
                    match = re.search(r'filename\*?=utf-8\'\'([^;]+)', disposition)
                    if match:
                        filename = match[1]
                    elif 'filename="' in disposition:
                        match2 = re.search(r'filename="([^"]+)"', disposition)
                        if match2:
                            filename = match2[1]
                    filepath = out_path / filename
                    filepath.write_bytes(resp.content)
                    safe_filename = re.sub(r'[/\\:*?"<>|]', '_', filename)
                    background_tasks.add_task(_cleanup_dir, out_path)
                    return FileResponse(
                        path=str(filepath),
                        filename=safe_filename,
                        media_type="application/octet-stream",
                    )
        except Exception:
            continue

    raise HTTPException(status_code=500, detail="All Invidious instances failed. This video may be age-restricted. Try a different video.")


@app.get("/api/playlist")
async def playlist_info(url: str):
    """Fetch playlist metadata and all video entries."""
    if not url:
        raise HTTPException(status_code=400, detail="URL is required")

    has_yt = bool(YOUTUBE_RE.search(url)) or "youtu" in url
    has_list = "list=" in url

    if not has_yt and not has_list:
        raise HTTPException(status_code=400, detail="Invalid YouTube URL")

    try:
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "extract_flat": True,  # Don't extract each video fully — just get the list
            "playlistend": 100,    # Limit to 100 videos to avoid timeouts
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        # If it's a single video (not a playlist), return it as a 1-item playlist
        if info.get("_type") == "video" or not info.get("entries"):
            return {
                "is_playlist": False,
                "title": info.get("title", "Unknown"),
                "uploader": info.get("uploader", "Unknown"),
                "video_count": 1,
                "videos": [{
                    "id": info.get("id", ""),
                    "title": info.get("title", "Unknown"),
                    "url": info.get("webpage_url") or info.get("url", ""),
                    "thumbnail": info.get("thumbnail", ""),
                    "duration": info.get("duration", 0),
                }],
            }

        entries = []
        for entry in info.get("entries", []):
            if entry is None:
                continue
            entries.append({
                "id": entry.get("id", ""),
                "title": entry.get("title", "Unknown"),
                "url": f"https://www.youtube.com/watch?v={entry.get('id', '')}",
                "thumbnail": entry.get("thumbnail", ""),
                "duration": entry.get("duration", 0),
            })

        return {
            "is_playlist": True,
            "title": info.get("title", "Unknown Playlist"),
            "uploader": info.get("uploader", "Unknown"),
            "video_count": len(entries),
            "videos": entries,
        }
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=400, detail=f"Could not fetch playlist: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")

@app.post("/api/upload-cookies")
async def upload_cookies(request: Request):
    """Upload fresh cookies.txt content to the server.
    
    Protected by COOKIE_ADMIN_KEY env var.
    Send POST with: Content-Type: text/plain and cookies.txt content as body.
    Header: X-Admin-Key: <your-key>
    """
    # Check admin key if configured
    if COOKIE_ADMIN_KEY:
        admin_key = request.headers.get("x-admin-key", "")
        if admin_key != COOKIE_ADMIN_KEY:
            raise HTTPException(status_code=403, detail="Invalid admin key")

    body = await request.body()
    content = body.decode("utf-8", errors="ignore")

    if not content or "youtube.com" not in content:
        raise HTTPException(status_code=400, detail="Invalid cookies content — must contain YouTube cookies")

    # Save cookies
    cookie_file = COOKIES_DIR / "cookies.txt"
    cookie_file.write_text(content)

    # Update metadata
    auth_cookies = []
    for name in ["SID", "HSID", "SSID", "APISID", "SAPISID", "LOGIN_INFO"]:
        if name in content:
            auth_cookies.append(name)

    meta = {
        "last_refresh": datetime.now(timezone.utc).isoformat(),
        "last_upload": datetime.now(timezone.utc).isoformat(),
        "status": "active",
        "cookie_count": content.count("\n"),
        "auth_cookies": auth_cookies,
    }
    COOKIE_META.write_text(json.dumps(meta, indent=2))

    return JSONResponse({
        "success": True,
        "message": f"Cookies uploaded ({len(content):,} bytes, {len(auth_cookies)} auth cookies)",
        "cookie_count": meta["cookie_count"],
        "auth_cookies": auth_cookies,
    })


@app.get("/api/cookie-status")
async def cookie_status():
    """Check cookie freshness and browser profile status."""
    meta = {}
    if COOKIE_META.exists():
        try:
            meta = json.loads(COOKIE_META.read_text())
        except Exception:
            pass

    has_profile = PROFILE_DIR.exists()
    has_cookies = bool(list(COOKIES_DIR.glob("*.txt")))

    result = {
        "has_browser_profile": has_profile,
        "has_cookies": has_cookies,
        "status": meta.get("status", "unknown"),
        "last_refresh": meta.get("last_refresh"),
        "last_login": meta.get("last_login"),
        "cookie_count": meta.get("cookie_count", 0),
        "auth_cookies": meta.get("auth_cookies", []),
    }

    # Calculate age
    last_refresh = meta.get("last_refresh")
    if last_refresh:
        try:
            last_dt = datetime.fromisoformat(last_refresh)
            age_days = (datetime.now(timezone.utc) - last_dt).days
            result["age_days"] = age_days
            result["is_stale"] = age_days >= 14
            result["is_aging"] = age_days >= 7
        except Exception:
            pass

    return JSONResponse(result)


@app.get("/api/pot-status")
async def pot_status():
    """Check PO token status."""
    tokens = _get_po_tokens()
    return JSONResponse({
        "has_tokens": bool(tokens),
        "status": tokens.get("status", "none"),
        "generated_at": tokens.get("generated_at"),
        "has_po_token": bool(tokens.get("po_token")),
        "has_visitor_data": bool(tokens.get("visitor_data")),
    })


@app.post("/api/refresh-cookies")
async def refresh_cookies_endpoint():
    """Trigger a cookie refresh using Playwright."""
    if not PROFILE_DIR.exists():
        raise HTTPException(
            status_code=400,
            detail="No browser profile found. Run 'python cookie_refresher.py login' first."
        )

    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "cookie_refresher.py", "refresh",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        except asyncio.TimeoutError:
            proc.kill()
            raise HTTPException(status_code=500, detail="Cookie refresh timed out.")

        if proc.returncode == 0:
            meta = {}
            if COOKIE_META.exists():
                meta = json.loads(COOKIE_META.read_text())
            return JSONResponse({
                "success": True,
                "message": "Cookies refreshed successfully.",
                "status": meta.get("status", "unknown"),
                "cookie_count": meta.get("cookie_count", 0),
            })
        else:
            raise HTTPException(
                status_code=500,
                detail=f"Cookie refresh failed: {stderr.decode().strip()}",
            )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Cookie refresh error: {str(e)}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
