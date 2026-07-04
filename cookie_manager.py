#!/usr/bin/env python3
"""Simple cookie manager for YT Buzz.

Usage:
    python cookie_manager.py list          # List all cookie files
    python cookie_manager.py add <file>    # Add a cookie file
    python cookie_manager.py remove <name> # Remove a cookie file
    python cookie_manager.py test          # Test if cookies work with yt-dlp
"""

import os
import sys
import shutil
import random
from pathlib import Path

COOKIES_DIR = Path("cookies")


def list_cookies():
    """List all cookie files in the cookies directory."""
    COOKIES_DIR.mkdir(exist_ok=True)
    files = list(COOKIES_DIR.glob("*.txt"))
    if not files:
        print("No cookie files found in cookies/")
        print("Add a cookies.txt file exported from your browser.")
        return

    print(f"Found {len(files)} cookie file(s) in cookies/:")
    for f in sorted(files):
        size = f.stat().st_size
        lines = len(f.read_text(errors="ignore").splitlines())
        print(f"  {f.name:30s}  {size:>8,} bytes  {lines:>5,} lines")


def add_cookie(file_path: str):
    """Add a cookie file to the cookies directory."""
    src = Path(file_path)
    if not src.exists():
        print(f"Error: File not found: {file_path}")
        return

    content = src.read_text(errors="ignore")
    if "youtube.com" not in content:
        print("Warning: File doesn't seem to contain YouTube cookies.")
        print("Make sure this is a Netscape-format cookies.txt file.")
        confirm = input("Continue anyway? (y/N): ")
        if confirm.lower() != "y":
            return

    dest = COOKIES_DIR / "cookies.txt"
    shutil.copy2(src, dest)
    print(f"Cookies added: {dest}")


def remove_cookie(name: str):
    """Remove a cookie file from the cookies directory."""
    target = COOKIES_DIR / name
    if not target.exists():
        print(f"Error: File not found: {target}")
        return

    target.unlink()
    print(f"Removed: {target}")


def test_cookies():
    """Test if cookies work with yt-dlp."""
    try:
        import yt_dlp
    except ImportError:
        print("Error: yt-dlp not installed. Run: pip install yt-dlp")
        return

    cookie_files = list(COOKIES_DIR.glob("*.txt"))
    if not cookie_files:
        print("No cookie files found in cookies/")
        return

    cookie_path = str(random.choice(cookie_files))
    print(f"Testing with: {cookie_path}")

    # Try a simple age-restricted video
    test_url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "cookiefile": cookie_path,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(test_url, download=False)
            print(f"SUCCESS: {info.get('title', 'Unknown')}")
            print(f"Formats available: {len(info.get('formats', []))}")
    except Exception as e:
        print(f"FAILED: {e}")
        print("Try re-exporting cookies from YouTube.")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return

    cmd = sys.argv[1]

    if cmd == "list":
        list_cookies()
    elif cmd == "add":
        if len(sys.argv) < 3:
            print("Usage: python cookie_manager.py add <file>")
            return
        add_cookie(sys.argv[2])
    elif cmd == "remove":
        if len(sys.argv) < 3:
            print("Usage: python cookie_manager.py remove <filename>")
            return
        remove_cookie(sys.argv[2])
    elif cmd == "test":
        test_cookies()
    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)


if __name__ == "__main__":
    main()
