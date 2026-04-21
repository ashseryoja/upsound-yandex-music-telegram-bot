"""
api/webhook.py
~~~~~~~~~~~~~~
Vercel serverless entry point for Telegram webhook updates.

Vercel invokes this module as a Python WSGI/HTTP handler.  The class **must**
be named ``handler`` (lowercase) for the @vercel/python runtime to pick it up.

Design notes:
- Bot and Dispatcher are module-level singletons so they are reused across
  warm invocations (avoids re-creating them on every request).
- asyncio.new_event_loop() is used instead of get_event_loop() because Vercel
  serverless workers may not have a running loop in the thread that handles
  the request.
- All exceptions are caught at the outermost level; a 500 is returned so
  Telegram retries the update later rather than silently dropping it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from http.server import BaseHTTPRequestHandler

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import Update
from dotenv import load_dotenv

# Load .env when running locally; on Vercel the vars are set in the dashboard.
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Bot / Dispatcher singletons
# ---------------------------------------------------------------------------
_BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

if not _BOT_TOKEN:
    # Fail loudly at import time so Vercel build logs show a clear error.
    raise RuntimeError("BOT_TOKEN environment variable is not set.")

bot = Bot(
    token=_BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)

dp = Dispatcher()

# Register all application routers
from bot.handlers import router as main_router  # noqa: E402 (import after setup)

dp.include_router(main_router)


# ---------------------------------------------------------------------------
# Vercel handler class
# ---------------------------------------------------------------------------

class handler(BaseHTTPRequestHandler):
    """HTTP request handler recognised by the @vercel/python runtime."""

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        """Redirect BaseHTTPRequestHandler access logs to the Python logger."""
        logger.debug(format, *args)

    # ------------------------------------------------------------------
    # Telegram only sends POST requests to the webhook endpoint
    # ------------------------------------------------------------------

    def do_POST(self) -> None:
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)

            if not body:
                self._respond(400, b"empty body")
                return

            raw: dict = json.loads(body)
            update = Update.model_validate(raw)

            # Run the async dispatcher in a fresh event loop.
            # We must create a NEW loop every time because Vercel reuses
            # the same process across warm invocations, and asyncio.run()
            # closes the loop after completion — a subsequent call would
            # hit "Event loop is closed".
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(dp.feed_update(bot=bot, update=update))
            finally:
                loop.close()

            self._respond(200, b"ok")

        except json.JSONDecodeError as exc:
            logger.error("Failed to decode JSON body: %s", exc)
            self._respond(400, b"invalid json")

        except Exception as exc:  # noqa: BLE001
            # Return 500 so Telegram retries this update.
            logger.exception("Unhandled error processing update: %s", exc)
            self._respond(500, b"internal error")

    # ------------------------------------------------------------------
    # Telegram may send HEAD/GET to verify the endpoint is reachable
    # ------------------------------------------------------------------

    def do_GET(self) -> None:
        self._respond(200, b"Upsound webhook is alive.")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _respond(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
