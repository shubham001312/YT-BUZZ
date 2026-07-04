import os
import re
import uuid
import random
import shutil
import asyncio
import yt_dlp
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, BackgroundTasks, UploadFile, File
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
try:
    import httpx
except ImportError:
    httpx = None

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

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start keep-alive background task on startup
    task = asyncio.create_task(keep_alive())
    yield
    # Cancel on shutdown
    task.cancel()

app = FastAPI(title="YT Buzz Downloader", lifespan=lifespan)

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)


# Preset cookies for age-restricted videos (Netscape format)
# Place multiple cookies.txt files in the cookies/ directory
# The server randomly picks one for each request to distribute load
COOKIES_DIR = Path("cookies")
COOKIES_DIR.mkdir(exist_ok=True)
COOKIES_FILE = os.environ.get("COOKIES_FILE", "")  # Legacy env var support

def _get_cookies_path() -> str | None:
    """Return a randomly selected cookies file path.
    
    Priority:
    1. COOKIES_FILE env var (if set)
    2. Random pick from cookies/*.txt files
    3. Uploaded cookies (legacy: cookies/youtube_cookies.txt)
    """
    if COOKIES_FILE and Path(COOKIES_FILE).exists():
        return COOKIES_FILE
    # Find all .txt files in cookies/ directory
    cookie_files = list(COOKIES_DIR.glob("*.txt"))
    if not cookie_files:
        return None
    # Randomly pick one to distribute load across accounts
    chosen = random.choice(cookie_files)
    return str(chosen)

app.mount("/static", StaticFiles(directory="static"), name="static")


YOUTUBE_RE = re.compile(r'(youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/)')

# Player clients to try in order for bypassing restrictions
PLAYER_CLIENTS = [
    ["web"],
    ["web_creator"],
    ["mweb"],
    ["tv_embedded"],
    ["android"],
]

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


@app.get("/api/cookies")
async def cookies_status():
    """Check if cookies file is uploaded."""
    path = _get_cookies_path()
    return JSONResponse({"has_cookies": path is not None, "path": path or ""})


@app.post("/api/upload-cookies")
async def upload_cookies(file: UploadFile = File(...)):
    """Upload a Netscape-format cookies.txt file for age-restricted videos."""
    content = await file.read()
    text = content.decode("utf-8", errors="ignore")
    # Basic validation — Netscape cookies start with # Netscape HTTP Cookie File
    if "# Netscape" not in text and ".youtube.com" not in text and "youtube.com" not in text:
        raise HTTPException(status_code=400, detail="Invalid cookies file. Please upload a Netscape-format cookies.txt exported from your browser.")
    dest = COOKIES_DIR / "youtube_cookies.txt"
    dest.write_text(text, encoding="utf-8")
    return JSONResponse({"status": "ok", "message": "Cookies uploaded successfully. Age-restricted videos should now work."})


@app.get("/api/info")
async def video_info(url: str):
    """Fetch video metadata and available formats.
    
    Multi-layered bypass strategy:
    1. Try player clients (web, web_creator, mweb, tv_embedded, android)
    2. If age-restricted, try with preset cookies
    3. If still failing, try Invidious proxy as last resort
    """
    if not url or not YOUTUBE_RE.search(url):
        raise HTTPException(status_code=400, detail="Invalid YouTube URL")

    url = _clean_url(url)
    last_error = None

    # Layer 1: Try multiple player clients
    for clients in PLAYER_CLIENTS:
        try:
            ydl_opts = {
                "quiet": True,
                "no_warnings": True,
                "skip_download": True,
                "extractor_args": {"youtube": {"player_client": clients}},
            }
            cookies_path = _get_cookies_path()
            if cookies_path:
                ydl_opts["cookiefile"] = cookies_path
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)

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
        except yt_dlp.utils.DownloadError as e:
            last_error = e
            continue

    # Layer 2: Try with cookies if age-restricted or any error
    if last_error is not None:
        cookies_path = _get_cookies_path()
        if cookies_path:
            try:
                ydl_opts = {
                    "quiet": True,
                    "no_warnings": True,
                    "skip_download": True,
                    "extractor_args": {"youtube": {"player_client": ["web"]}},
                    "cookiefile": cookies_path,
                }
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=False)
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
            except Exception:
                pass

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
    elif "Sign in" in error_msg or "confirm your age" in error_msg or "age" in error_msg.lower():
        error_msg = "This video is age-restricted. Upload cookies.txt or try a different video."
    elif "Signature extraction failed" in error_msg:
        error_msg = "This video uses a protection that yt-dlp cannot currently bypass."
    else:
        error_msg = error_msg[:200]
    raise HTTPException(status_code=400, detail=f"Could not fetch video info: {error_msg}")


@app.get("/api/download")
async def download_video(url: str, format_id: str, ext: str = "mp4", download_type: str = "video", raw_format: str = "", background_tasks: BackgroundTasks = None):
    """Download and serve a video file.

    If raw_format is provided, use it directly as the yt-dlp format selector
    (for playlist downloads that build their own complex format strings).
    Otherwise, construct the format selector from format_id + download_type.
    """
    if not url or not YOUTUBE_RE.search(url):
        raise HTTPException(status_code=400, detail="Invalid YouTube URL")

    url = _clean_url(url)

    job_id = uuid.uuid4().hex[:12]
    output_dir = DOWNLOAD_DIR / job_id
    output_dir.mkdir(exist_ok=True)

    output_template = str(output_dir / "%(title)s.%(ext)s")

    try:
        # Build the yt-dlp format selector:
        # If raw_format is provided, use it directly (playlist mode)
        # Otherwise, construct from format_id + download_type with robust fallbacks
        if raw_format:
            fmt_selector = raw_format
        elif download_type == "audio":
            fmt_selector = f"{format_id}/bestaudio/best"
        else:
            # video+bestaudio ensures DASH streams get audio merged; /best is final fallback
            fmt_selector = f"{format_id}+bestaudio/best"

        ydl_opts = {
            "format": fmt_selector,
            "outtmpl": output_template,
            "quiet": True,
            "no_warnings": True,
            "no_cache_dir": True,
            "merge_output_format": ext if ext != "mp3" else None,
            "postprocessors": [],
            "extractor_args": {"youtube": {"player_client": ["web", "web_creator", "mweb"]}},
            # Speed optimizations
            "concurrent_fragment_downloads": 4,
            "http_chunk_size": 1048576,  # 1MB chunks for better throughput
            "socket_timeout": 30,
        }
        cookies_path = _get_cookies_path()
        if cookies_path:
            ydl_opts["cookiefile"] = cookies_path

        # Handle audio-only downloads
        if download_type == "audio" and ext == "mp3":
            ydl_opts["postprocessors"].append({
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            })

        # Try with the requested format first; if it fails, fall back to best available
        info = None
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
        except yt_dlp.utils.DownloadError:
            # Format not available — clean up partial files and retry with best
            for f in output_dir.iterdir():
                f.unlink(missing_ok=True)

        if info is None:
            ydl_opts["format"] = "bestvideo+bestaudio/best"
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=True)
            except yt_dlp.utils.DownloadError as e:
                shutil.rmtree(output_dir, ignore_errors=True)
                error_msg = str(e)
                if "Signature extraction failed" in error_msg:
                    error_msg = "This video uses a protection that yt-dlp cannot currently bypass."
                raise HTTPException(status_code=500, detail=f"Download failed: {error_msg}")

        # Find the downloaded file
        files = list(output_dir.iterdir())
        if not files:
            shutil.rmtree(output_dir, ignore_errors=True)
            raise HTTPException(status_code=500, detail="Download failed — no file produced")

        downloaded_file = files[0]
        filename = downloaded_file.name

        # Sanitize filename for Content-Disposition header
        # Remove path separators and other unsafe chars for HTTP header
        safe_filename = re.sub(r'[/\\:*?"<>|]', '_', filename)

        # Schedule cleanup AFTER the response is fully sent
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

    raise HTTPException(status_code=500, detail="All Invidious instances failed. Try uploading cookies.txt or a different video.")


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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
