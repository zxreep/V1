# api/webhook.py
"""
Vercel-compatible webhook entrypoint.

- Uses Flask to accept POST requests with Telegram update payloads.
- Converts JSON to telegram.Update via Update.de_json
- Calls Application.process_update(update) to handle the update.
- Uses asyncio.run to execute the coroutine since this entrypoint is synchronous.

Notes:
- Place this file under /api/webhook.py when deploying to Vercel (the framework will map /api/webhook).
- Ensure TELEGRAM_BOT_TOKEN, ADMIN_USER_ID and LULU_KEY are set in the environment.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Dict

from flask import Flask, request, Response
from telegram import Update

# Import the app built in bot.py
from bot import app  # Application instance

# Create Flask app — Vercel will use this WSGI app as the serverless function entrypoint.
flask_app = Flask(__name__)


@flask_app.route("/", methods=["GET"])
def health_check() -> Response:
    # Simple health check for the function
    return Response("OK", status=200)


@flask_app.route("/", methods=["POST"])
def webhook_entry() -> Response:
    """
    Webhook handler for Telegram updates.

    Steps:
      - parse JSON body
      - create Update via Update.de_json
      - process update via app.process_update(update)
    """
    try:
        body: Dict[str, Any] = request.get_json(force=True)
    except Exception:
        return Response("Invalid JSON", status=400)

    if not body:
        return Response("Empty body", status=400)

    try:
        update = Update.de_json(body, app.bot)
    except Exception:
        return Response("Failed to parse Telegram Update", status=400)

    # app.process_update is a coroutine. Execute deterministically.
    try:
        # Running the coroutine synchronously in serverless context.
        asyncio.run(app.process_update(update))
    except Exception:
        # Do not expose stacktrace to Telegram or client; just return 200 to acknowledge.
        # Log details to stdout/stderr (server logs) for debugging.
        import traceback
        traceback.print_exc()
        return Response("Processed with internal errors", status=200)

    # Always return 200 to Telegram on success.
    return Response("OK", status=200)
