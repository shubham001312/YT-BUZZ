# YT Buzz — YouTube Downloader

A minimal, clean web app to download any YouTube video in any quality.

## Stack

- **Backend:** Python + FastAPI + yt-dlp
- **Frontend:** HTML + CSS + JS
- **Prerequisites:** Python 3.10+, ffmpeg

## Setup

```bash
# 1. Clone / navigate to the project
cd yt-buzz

# 2. Create virtual environment (recommended)
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Run the server
python main.py
```

The app will be live at **http://localhost:8000**.

## How It Works

1. Paste a YouTube URL into the input box
2. Click **Get Formats** — the app fetches video metadata and lists all available qualities
3. Choose a video quality or audio-only format
4. Click to download — the server streams the file to your browser

## Deployment

### Railway

```bash
# Install Railway CLI
npm i -g @railway/cli

# Login and init
railway login
railway init

# Add ffmpeg buildpack (or use a Dockerfile)
# Create a Dockerfile:
# FROM python:3.11-slim
# RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*
# WORKDIR /app
# COPY requirements.txt .
# RUN pip install -r requirements.txt
# COPY . .
# CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "$PORT"]

railway up
```

### Render

- Create a new **Web Service**
- Point to this repo
- Build command: `pip install -r requirements.txt`
- Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
- Make sure ffmpeg is installed in the build (add a `render.yaml` or use a Dockerfile)

### Docker (any platform)

```bash
docker build -t yt-buzz .
docker run -p 8000:8000 yt-buzz
```

## Notes

- Downloads are temporary — files are deleted from the server after being sent
- The app requires **ffmpeg** installed on the server for high-quality video+audio merging
- For personal use only — respect YouTube's Terms of Service
