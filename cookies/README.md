# YouTube Cookies for Age-Restricted Videos

This directory stores YouTube cookies used by yt-dlp to download age-restricted videos.

## How to Get Cookies

1. Install the **[Get cookies.txt LOCALLY](https://chrome.google.com/webstore/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc)** Chrome extension
2. Go to [YouTube](https://youtube.com) and make sure you're **logged in**
3. Click the extension icon → **Export**
4. Save the file as `cookies.txt` in this directory

## How It Works

- The server picks a random `.txt` file from this directory for each request
- You can place **multiple cookie files** (e.g., `account1.txt`, `account2.txt`) to distribute load
- Cookies typically expire after **2-3 weeks** — re-export when downloads fail

## Quick Refresh

```bash
# Replace the cookies file on the server
scp cookies.txt user@server:/path/to/YT-BUZZ-DOWNLOADER/cookies/cookies.txt

# Or on Render, deploy a new version with the updated cookies file
```

## Troubleshooting

- If age-restricted videos still fail, your cookies may have expired
- Re-export from YouTube and replace the file
- Use a **dedicated Google account** for this (not your primary)
