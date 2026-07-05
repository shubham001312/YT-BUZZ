#!/usr/bin/env python3
"""Playwright-based YouTube cookie refresher.

Uses a persistent browser profile so you only need to login ONCE.
After that, Playwright reuses the session automatically to refresh cookies.

Usage:
    python cookie_refresher.py login       # Open browser for first-time login
    python cookie_refresher.py refresh     # Refresh cookies from saved session
    python cookie_refresher.py status      # Check cookie freshness
    python cookie_refresher.py export      # Export cookies to Netscape format
"""

import os
import sys
import json
from pathlib import Path
from datetime import datetime, timezone

# Directories
PROFILE_DIR = Path("browser_profile")
COOKIES_DIR = Path("cookies")
COOKIE_FILE = COOKIES_DIR / "cookies.txt"
COOKIE_META = COOKIES_DIR / "cookie_meta.json"


def _ensure_dirs():
    PROFILE_DIR.mkdir(exist_ok=True)
    COOKIES_DIR.mkdir(exist_ok=True)


def _save_meta(info: dict):
    """Save cookie metadata (last refresh time, status, etc.)."""
    COOKIE_META.write_text(json.dumps(info, indent=2))


def _load_meta() -> dict:
    """Load cookie metadata."""
    if COOKIE_META.exists():
        return json.loads(COOKIE_META.read_text())
    return {}


def login():
    """Open a visible browser for first-time YouTube login.
    
    After logging in, the browser profile is saved automatically.
    You only need to do this ONCE — subsequent refreshes reuse the profile.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Error: playwright not installed.")
        print("Run: pip install playwright && playwright install chromium")
        return False

    _ensure_dirs()

    print("=" * 60)
    print("  YouTube Cookie Login")
    print("=" * 60)
    print()
    print("A browser window will open. Steps:")
    print("  1. Go to YouTube (already loaded)")
    print("  2. Click 'Sign in' and login with your Google account")
    print("  3. Make sure you're fully logged in (see your avatar)")
    print("  4. Come back here and press Enter")
    print()

    with sync_playwright() as p:
        # Launch with persistent profile — saves cookies, localStorage, etc.
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,  # Visible browser for login
            args=["--disable-blink-features=AutomationControlled"],
            viewport={"width": 1280, "height": 800},
        )

        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://www.youtube.com", wait_until="domcontentloaded")

        input("Press Enter after you've logged into YouTube...")

        # Check if logged in
        logged_in = False
        try:
            # Look for the avatar/settings button which only appears when logged in
            page.wait_for_selector('button#avatar-btn, yt-img-shadow.ytd-topbar-menu-button-renderer img', timeout=5000)
            logged_in = True
        except Exception:
            # Try alternative check
            try:
                content = page.content()
                logged_in = "Sign out" in content or "avatar-btn" in content
            except Exception:
                pass

        if logged_in:
            print("Login detected! Saving session...")
            _save_meta({
                "last_login": datetime.now(timezone.utc).isoformat(),
                "last_refresh": datetime.now(timezone.utc).isoformat(),
                "status": "active",
            })
        else:
            print("Warning: Could not confirm login. The session may still work.")

        context.close()

    print(f"Browser profile saved to: {PROFILE_DIR}/")
    print("You can now use 'python cookie_refresher.py refresh' to export cookies.")
    return True


def refresh():
    """Refresh cookies using the saved browser profile.
    
    Navigates to YouTube using the persistent profile (no login needed),
    extracts all cookies, and saves them in Netscape format for yt-dlp.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Error: playwright not installed.")
        print("Run: pip install playwright && playwright install chromium")
        return False

    if not PROFILE_DIR.exists():
        print("Error: No browser profile found.")
        print("Run 'python cookie_refresher.py login' first.")
        return False

    _ensure_dirs()
    print("Refreshing cookies from saved session...")

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=True,  # No need to see the browser
            args=["--disable-blink-features=AutomationControlled"],
        )

        page = context.pages[0] if context.pages else context.new_page()

        # Navigate to YouTube to ensure cookies are fresh
        page.goto("https://www.youtube.com", wait_until="domcontentloaded")
        page.wait_for_timeout(3000)  # Let page fully load

        # Get all cookies from the browser
        cookies = context.cookies("https://www.youtube.com")
        google_cookies = context.cookies("https://www.google.com")

        all_cookies = cookies + google_cookies

        if not all_cookies:
            print("Error: No cookies extracted. Session may have expired.")
            print("Run 'python cookie_refresher.py login' to re-login.")
            _save_meta({
                "last_refresh": datetime.now(timezone.utc).isoformat(),
                "status": "expired",
                "error": "No cookies extracted",
            })
            context.close()
            return False

        # Convert to Netscape format
        netscape_lines = ["# Netscape HTTP Cookie File", "# Generated by YT Buzz Cookie Refresher", ""]
        for c in all_cookies:
            domain = c.get("domain", "")
            flag = "TRUE" if domain.startswith(".") else "FALSE"
            path = c.get("path", "/")
            secure = "TRUE" if c.get("secure", False) else "FALSE"
            expires = int(c.get("expires", 0))
            if expires < 0:
                expires = 0
            name = c.get("name", "")
            value = c.get("value", "")
            netscape_lines.append(f"{domain}\t{flag}\t{path}\t{secure}\t{expires}\t{name}\t{value}")

        # Write cookies file
        COOKIE_FILE.write_text("\n".join(netscape_lines) + "\n")

        # Check for essential auth cookies
        cookie_names = {c["name"] for c in all_cookies}
        essential = {"SID", "HSID", "SSID", "APISID", "SAPISID", "LOGIN_INFO"}
        has_auth = essential.intersection(cookie_names)

        cookie_status = "active" if len(has_auth) >= 3 else "partial"
        _save_meta({
            "last_refresh": datetime.now(timezone.utc).isoformat(),
            "last_login": _load_meta().get("last_login"),
            "status": cookie_status,
            "cookie_count": len(all_cookies),
            "auth_cookies": sorted(list(has_auth)),
        })

        context.close()

    print(f"Cookies exported: {COOKIE_FILE} ({len(all_cookies)} cookies)")
    print(f"Auth cookies found: {', '.join(sorted(has_auth)) if has_auth else 'None'}")
    print(f"Status: {cookie_status}")
    return True


def status():
    """Check cookie freshness and status."""
    meta = _load_meta()

    if not meta:
        print("No cookie metadata found.")
        if COOKIE_FILE.exists():
            print(f"Cookies file exists: {COOKIE_FILE} ({COOKIE_FILE.stat().st_size:,} bytes)")
            print("Run 'python cookie_refresher.py refresh' to update.")
        else:
            print("No cookies found. Run 'python cookie_refresher.py login' first.")
        return True

    print("Cookie Status:")
    print(f"  Status:       {meta.get('status', 'unknown')}")
    print(f"  Last login:   {meta.get('last_login', 'never')}")
    print(f"  Last refresh: {meta.get('last_refresh', 'never')}")
    print(f"  Cookie count: {meta.get('cookie_count', 'unknown')}")
    print(f"  Auth cookies: {', '.join(meta.get('auth_cookies', [])) or 'none'}")

    # Check age
    last_refresh = meta.get("last_refresh")
    if last_refresh:
        try:
            last = datetime.fromisoformat(last_refresh)
            age = datetime.now(timezone.utc) - last
            days = age.days
            print(f"  Age:          {days} day(s)")
            if days >= 14:
                print("  [!] Cookies may be expired. Run 'python cookie_refresher.py refresh'")
            elif days >= 7:
                print("  [~] Cookies are aging. Consider refreshing soon.")
            else:
                print("  [OK] Cookies look fresh.")
        except Exception:
            pass

    if COOKIE_FILE.exists():
        print(f"  File:         {COOKIE_FILE} ({COOKIE_FILE.stat().st_size:,} bytes)")


def push():
    """Refresh cookies locally and push them to the server via API.
    
    Usage: python cookie_refresher.py push <server-url> [admin-key]
    """
    # Read args from sys.argv since main() doesn't pass them
    server_url = sys.argv[2] if len(sys.argv) > 2 else ""
    admin_key = sys.argv[3] if len(sys.argv) > 3 else ""

    if not server_url:
        print("Usage: python cookie_refresher.py push <server-url> [admin-key]")
        print("Example: python cookie_refresher.py push https://yt-buzz.onrender.com")
        return False
    # Step 1: Refresh cookies locally
    print("Step 1: Refreshing cookies locally...")
    if not refresh():
        print("Failed to refresh cookies. Aborting push.")
        return False

    # Step 2: Read the cookies file
    if not COOKIE_FILE.exists():
        print("Error: No cookies.txt file found after refresh.")
        return False

    content = COOKIE_FILE.read_text()
    print(f"Step 2: Read {len(content):,} bytes from {COOKIE_FILE}")

    # Step 3: Push to server
    print(f"Step 3: Pushing to {server_url}/api/upload-cookies...")
    try:
        import urllib.request
        import urllib.error

        url = f"{server_url.rstrip('/')}/api/upload-cookies"
        headers = {
            "Content-Type": "text/plain",
        }
        if admin_key:
            headers["X-Admin-Key"] = admin_key

        req = urllib.request.Request(url, data=content.encode("utf-8"), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
            print(f"Success: {result.get('message', 'OK')}")
            print(f"Auth cookies: {', '.join(result.get('auth_cookies', []))}")
            return True
    except urllib.error.HTTPError as e:
        error_body = e.read().decode() if e.fp else ""
        print(f"Server error ({e.code}): {error_body[:200]}")
        return False
    except Exception as e:
        print(f"Connection error: {e}")
        print(f"Make sure the server is running at {server_url}")
        return False


def export():
    """Just export cookies (same as refresh, but named for clarity)."""
    return refresh()


COMMANDS = {
    "login": login,
    "refresh": refresh,
    "status": status,
    "push": push,
    "export": export,
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        print("Commands:", ", ".join(COMMANDS.keys()))
        return

    cmd = sys.argv[1]
    success = COMMANDS[cmd]()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
