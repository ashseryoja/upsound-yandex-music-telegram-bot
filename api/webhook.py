"""
api/webhook.py
~~~~~~~~~~~~~~
Vercel serverless entry point for Telegram webhook updates.

Vercel invokes this module as a Python WSGI/HTTP handler.  The class **must**
be named ``handler`` (lowercase) for the @vercel/python runtime to pick it up.

Design notes:
- Dispatcher is a module-level singleton — routers can be safely reused.
- Bot is created per-request inside an ``async with`` block so the underlying
  aiohttp session is always bound to the *current* event loop and closed
  automatically.  This avoids the classic "Event loop is closed" error that
  occurs when a global Bot outlives the loop that created it.
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
# Configuration
# ---------------------------------------------------------------------------
_BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

if not _BOT_TOKEN:
    # Fail loudly at import time so Vercel build logs show a clear error.
    raise RuntimeError("BOT_TOKEN environment variable is not set.")

# Dispatcher is a global singleton — routers are stateless and reusable.
dp = Dispatcher()

# Register all application routers
from bot.handlers import router as main_router  # noqa: E402 (import after setup)

dp.include_router(main_router)


# ---------------------------------------------------------------------------
# Per-request handler — Bot is created and destroyed within a single update
# ---------------------------------------------------------------------------

async def _handle_update(update: Update) -> None:
    """Create a short-lived Bot, process the update, then close the session."""
    async with Bot(
        token=_BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    ) as bot:
        await dp.feed_update(bot=bot, update=update)


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

            # asyncio.run() creates a fresh loop, runs the coroutine,
            # then closes the loop.  Because the Bot is created *inside*
            # _handle_update via ``async with``, its aiohttp session is
            # always bound to this new loop — no stale-session issues.
            asyncio.run(_handle_update(update))

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
