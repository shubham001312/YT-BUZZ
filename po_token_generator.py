#!/usr/bin/env python3
"""Lightweight PO Token generator using Playwright.

Extracts YouTube BotGuard/PO tokens by loading YouTube in a headless browser.
These tokens allow yt-dlp to bypass SABR streaming restrictions.

Usage:
    python po_token_generator.py generate     # Generate fresh tokens
    python po_token_generator.py get          # Get cached tokens (generate if stale)
    python po_token_generator.py status       # Check token freshness
"""

import os
import sys
import json
import time
from pathlib import Path
from datetime import datetime, timezone

TOKEN_FILE = Path("cookies") / "po_tokens.json"
MAX_AGE = 3600  # Tokens valid for ~1 hour


def _save_tokens(tokens: dict):
    """Save PO tokens to disk."""
    TOKEN_FILE.parent.mkdir(exist_ok=True)
    TOKEN_FILE.write_text(json.dumps(tokens, indent=2))


def _load_tokens() -> dict:
    """Load cached PO tokens from disk."""
    if TOKEN_FILE.exists():
        try:
            return json.loads(TOKEN_FILE.read_text())
        except Exception:
            pass
    return {}


def generate() -> bool:
    """Generate fresh PO tokens using Playwright."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Error: playwright not installed.")
        print("Run: pip install playwright && playwright install chromium")
        return False

    print("Generating PO tokens via Playwright...")
    
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ]
            )
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                viewport={"width": 1920, "height": 1080},
            )
            
            # Remove webdriver detection
            context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
                Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            """)
            
            page = context.new_page()
            
            # Navigate to YouTube to trigger BotGuard
            print("Loading YouTube page...")
            page.goto("https://www.youtube.com", wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(3000)
            
            # Extract visitor data and PO token from YouTube's cookies/localStorage
            cookies = context.cookies("https://www.youtube.com")
            
            # Look for VISITOR_INFO1_LIVE cookie (contains visitor data)
            visitor_data = None
            for cookie in cookies:
                if cookie["name"] == "VISITOR_INFO1_LIVE":
                    visitor_data = cookie["value"]
                    break
            
            # Try to extract PO token from page JavaScript
            po_token = None
            try:
                # YouTube stores BotGuard token in various places
                result = page.evaluate("""() => {
                    // Try to find ytcfg which contains visitor data
                    if (typeof ytcfg !== 'undefined') {
                        return {
                            visitorData: ytcfg.get('VISITOR_DATA') || null,
                            poToken: ytcfg.get('PO_TOKEN') || null,
                            innertube_context: ytcfg.get('INNERTUBE_CONTEXT') || null,
                        };
                    }
                    return null;
                }""")
                if result:
                    visitor_data = visitor_data or result.get("visitorData")
                    po_token = result.get("poToken")
            except Exception as e:
                print(f"Note: Could not extract from ytcfg: {e}")
            
            # If no PO token from ytcfg, try to get it from the page source
            if not po_token:
                try:
                    page_content = page.content()
                    # Look for poToken in page source
                    import re
                    token_match = re.search(r'"poToken"\s*:\s*"([^"]+)"', page_content)
                    if token_match:
                        po_token = token_match.group(1)
                except Exception:
                    pass
            
            browser.close()
            
            if visitor_data and po_token:
                tokens = {
                    "visitor_data": visitor_data,
                    "po_token": po_token,
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "cookies": {c["name"]: c["value"] for c in cookies if "youtube" in c.get("domain", "")},
                    "status": "active",
                }
                _save_tokens(tokens)
                print(f"Tokens generated successfully!")
                print(f"  Visitor data: {visitor_data[:20]}...")
                print(f"  PO token: Yes")
                print(f"  Status: active")
                return True
            elif visitor_data:
                tokens = {
                    "visitor_data": visitor_data,
                    "po_token": None,
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "cookies": {c["name"]: c["value"] for c in cookies if "youtube" in c.get("domain", "")},
                    "status": "partial",
                }
                _save_tokens(tokens)
                print(f"Partial tokens generated (visitor data only, no PO token).")
                print(f"  Visitor data: {visitor_data[:20]}...")
                print(f"  Note: PO tokens may not be extractable on datacenter IPs.")
                return False
            else:
                print("Warning: Could not extract visitor data.")
                return False
                
    except Exception as e:
        print(f"Error generating tokens: {e}")
        return False


def get_tokens() -> dict:
    """Get cached tokens, generating new ones if stale."""
    tokens = _load_tokens()
    
    if tokens:
        try:
            generated = datetime.fromisoformat(tokens.get("generated_at", "2000-01-01"))
            age = (datetime.now(timezone.utc) - generated).total_seconds()
            if age < MAX_AGE:
                return tokens
        except Exception:
            pass
    
    # Generate fresh tokens
    if generate():
        return _load_tokens()
    return {}


def status():
    """Check token freshness and status."""
    tokens = _load_tokens()
    
    if not tokens:
        print("No PO tokens found.")
        print("Run 'python po_token_generator.py generate' to create them.")
        return
    
    print("PO Token Status:")
    print(f"  Status:    {tokens.get('status', 'unknown')}")
    print(f"  Generated: {tokens.get('generated_at', 'unknown')}")
    print(f"  Visitor:   {tokens.get('visitor_data', 'none')[:30]}...")
    print(f"  PO Token:  {'Present' if tokens.get('po_token') else 'Not present'}")
    
    # Check age
    try:
        generated = datetime.fromisoformat(tokens.get("generated_at", "2000-01-01"))
        age = (datetime.now(timezone.utc) - generated).total_seconds()
        age_min = int(age / 60)
        if age > MAX_AGE:
            print(f"  Age: {age_min} min (STALE - regenerate)")
        else:
            print(f"  Age: {age_min} min (fresh)")
    except Exception:
        pass


COMMANDS = {
    "generate": generate,
    "get": lambda: bool(get_tokens()),
    "status": status,
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        print("Commands:", ", ".join(COMMANDS.keys()))
        return

    cmd = sys.argv[1]
    if cmd == "status":
        COMMANDS[cmd]()
    else:
        success = COMMANDS[cmd]()
        sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
