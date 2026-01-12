# bot.py
"""
Core bot application for Vercel / local use.

- Builds a python-telegram-bot Application without running pollers.
- Exposes `app` to be imported by webhook entrypoint.
- Contains a single async MessageHandler that accepts text messages only from ADMIN_USER_ID.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Dict, Optional, Sequence

import requests
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError
from telegram import Update, Bot, constants
from telegram.ext import Application, ContextTypes, MessageHandler, filters

# Configuration via environment variables (no hard-coded secrets)
TELEGRAM_BOT_TOKEN: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ADMIN_USER_ID: Optional[int] = None
_ADMIN_ENV = os.environ.get("ADMIN_USER_ID")
if _ADMIN_ENV:
    try:
        ADMIN_USER_ID = int(_ADMIN_ENV)
    except ValueError:
        ADMIN_USER_ID = None

LULU_KEY: str = os.environ.get("LULU_KEY", "")
# Optional: base URL for logs (not required)
BASE_URL: str = os.environ.get("BASE_URL", "")

# Constants
YT_DLP_TIMEOUT = 30  # seconds for yt_dlp extraction
HTTP_TIMEOUT = 15  # seconds for requests to external services
LULU_UPLOAD_ENDPOINT = "https://lulustream.com/api/upload/url?key={key}&url={url}"
LULU_INFO_ENDPOINT = "https://lulustream.com/api/file/info?key={key}&file_code={file_code}"

# Basic validation of required envs is intentionally deferred to runtime to allow local testing.


def _escape_markdown_v2(text: str) -> str:
    """
    Escape text for Telegram MarkdownV2.
    See https://core.telegram.org/bots/api#markdownv2-style
    """
    replace_chars = r'_*[]()~`>#+-=|{}.!'
    escaped = []
    for ch in text:
        if ch in replace_chars:
            escaped.append(f"\\{ch}")
        else:
            escaped.append(ch)
    return "".join(escaped)


def select_best_direct_format(formats: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    From yt-dlp formats[], pick the best format that:
      - has a 'url' key
      - vcodec != "none" (video present)
    Sorting preference: prefer highest height, then tbr, then filesize.
    """
    candidates = []
    for f in formats:
        if not isinstance(f, dict):
            continue
        url = f.get("url")
        vcodec = f.get("vcodec")
        if url and vcodec and vcodec != "none":
            # normalize numeric keys
            height = f.get("height") or 0
            tbr = f.get("tbr") or 0
            filesize = f.get("filesize") or f.get("filesize_approx") or 0
            candidates.append((height, tbr, filesize, f))
    if not candidates:
        return None
    # sort descending by height, tbr, filesize
    candidates.sort(key=lambda tup: (tup[0] or 0, tup[1] or 0, tup[2] or 0), reverse=True)
    return candidates[0][3]


def extract_metadata_with_ytdlp(url: str, timeout: int = YT_DLP_TIMEOUT) -> Dict[str, Any]:
    """
    Use yt_dlp Python API to extract info (equivalent to `yt-dlp -J` JSON).
    download=False to avoid any file downloads.
    Raises RuntimeError on failure with a clean message.
    """
    ydl_opts = {
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
    }
    try:
        with YoutubeDL(ydl_opts) as ydl:
            # extract_info with download=False returns the info_dict
            info = ydl.extract_info(url, download=False)
            if not isinstance(info, dict):
                raise RuntimeError("yt-dlp returned invalid metadata.")
            return info
    except DownloadError as e:
        raise RuntimeError(f"yt-dlp failed to extract info: {str(e)}")
    except Exception as e:
        raise RuntimeError(f"yt-dlp extraction error: {str(e)}")


def upload_url_to_lulustream(video_url: str, lulu_key: str, timeout: int = HTTP_TIMEOUT) -> str:
    """
    Perform the LuluStream URL upload (URL-based upload).
    Returns file_code (string) on success.
    Raises RuntimeError on failure with a clean message.
    """
    if not lulu_key:
        raise RuntimeError("LuluStream API key is not configured.")
    endpoint = LULU_UPLOAD_ENDPOINT.format(key=lulu_key, url=video_url)
    try:
        resp = requests.get(endpoint, timeout=timeout)
    except requests.RequestException as e:
        raise RuntimeError(f"Failed to reach LuluStream upload endpoint: {e}")
    try:
        data = resp.json()
    except ValueError:
        raise RuntimeError("LuluStream upload response was not valid JSON.")
    # Expecting result.filecode
    result = data.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("LuluStream upload response missing 'result'.")
    filecode = result.get("filecode") or result.get("file_code") or result.get("fileCode")
    if not filecode:
        raise RuntimeError("LuluStream upload response missing filecode.")
    return str(filecode)


def get_file_info_from_lulustream(file_code: str, lulu_key: str, timeout: int = HTTP_TIMEOUT) -> Dict[str, Any]:
    """
    Retrieve file info from LuluStream using exact URL format specified.
    Returns a dict containing at least: file_title, player_img, file_code
    """
    if not lulu_key:
        raise RuntimeError("LuluStream API key is not configured.")
    endpoint = LULU_INFO_ENDPOINT.format(key=lulu_key, file_code=file_code)
    try:
        resp = requests.get(endpoint, timeout=timeout)
    except requests.RequestException as e:
        raise RuntimeError(f"Failed to reach LuluStream file/info endpoint: {e}")
    try:
        data = resp.json()
    except ValueError:
        raise RuntimeError("LuluStream file/info response was not valid JSON.")
    # Validate and extract required fields
    # The exact shape may vary; defensively try multiple keys
    file_title = data.get("file_title") or data.get("title") or (data.get("result") or {}).get("file_title")
    player_img = data.get("player_img") or data.get("thumbnail") or (data.get("result") or {}).get("player_img")
    file_code_resp = data.get("file_code") or data.get("filecode") or (data.get("result") or {}).get("file_code")
    if not (file_title and player_img and file_code_resp):
        raise RuntimeError("LuluStream file/info response missing required fields.")
    return {"file_title": str(file_title), "player_img": str(player_img), "file_code": str(file_code_resp)}


async def _handle_admin_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Main handler for text messages from admin.
    Steps:
      1. Validate sender is ADMIN_USER_ID
      2. Extract URL from message text
      3. Use yt-dlp to get metadata (no downloads)
      4. Select best direct video format
      5. Call LuluStream URL upload endpoint
      6. Call LuluStream file/info and send photo + caption to admin
    """
    try:
        if update.message is None or update.message.text is None:
            return  # ignore non-text messages silently

        sender = update.effective_user
        if sender is None or ADMIN_USER_ID is None or sender.id != ADMIN_USER_ID:
            # silent ignore for non-admins
            return

        text = update.message.text.strip()
        if not text:
            await update.message.reply_text("Please send a valid video page URL or a direct video URL.")
            return

        await update.message.reply_text("Processing URL — extracting metadata (no download).")

        # Extract metadata via yt-dlp
        try:
            info = await asyncio.get_event_loop().run_in_executor(None, extract_metadata_with_ytdlp, text)
        except RuntimeError as e:
            await update.message.reply_text(f"Metadata extraction failed: {e}")
            return

        formats = info.get("formats") or []
        best = select_best_direct_format(formats)
        if not best:
            await update.message.reply_text("No valid direct video URL (format with video codec) found in metadata.")
            return

        direct_url = best.get("url")
        if not direct_url:
            await update.message.reply_text("Selected format did not contain a direct URL.")
            return

        # Upload via LuluStream (URL upload)
        try:
            file_code = await asyncio.get_event_loop().run_in_executor(None, upload_url_to_lulustream, direct_url, LULU_KEY)
        except RuntimeError as e:
            await update.message.reply_text(f"LuluStream upload failed: {e}")
            return

        # Retrieve file info
        try:
            info_resp = await asyncio.get_event_loop().run_in_executor(None, get_file_info_from_lulustream, file_code, LULU_KEY)
        except RuntimeError as e:
            await update.message.reply_text(f"Failed to retrieve file info from LuluStream: {e}")
            return

        title = info_resp["file_title"]
        player_img = info_resp["player_img"]
        file_code_resp = info_resp["file_code"]

        # Caption (Markdown). Escape user-provided title safely.
        safe_title = _escape_markdown_v2(title)
        caption = f"🎬 {safe_title}\n\n▶️ Watch: https://lulustream.com/{file_code_resp}"
        # Send photo
        try:
            await context.bot.send_photo(
                chat_id=ADMIN_USER_ID,
                photo=player_img,
                caption=caption,
                parse_mode=constants.ParseMode.MARKDOWN_V2,
            )
        except Exception as e:
            # Don't leak stack traces; give admin a readable message
            await update.message.reply_text(f"Failed to send result photo: {str(e)}")
            return

    except Exception:
        # Last-resort safety: never crash or expose tracebacks to Telegram
        if update.message and update.effective_user and ADMIN_USER_ID and update.effective_user.id == ADMIN_USER_ID:
            await update.message.reply_text("An unexpected error occurred. Check server logs for details.")
        return


def build_application() -> Application:
    """
    Construct and return the Application instance (no run_polling).
    """
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN environment variable is not set.")
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Register only one handler: messages (text only)
    handler = MessageHandler(filters.TEXT & (~filters.COMMAND), _handle_admin_message)
    app.add_handler(handler)
    return app


# Build the shared application instance to be imported by webhook entrypoint.
# This is stateless (no background tasks, no polling). It's safe for serverless.
app: Application = build_application()
bot: Bot = app.bot  # convenience
