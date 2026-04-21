"""
bot/handlers.py
~~~~~~~~~~~~~~~
aiogram v3 message handlers.

Registers a single Router that catches any message containing a Yandex Music
URL, fetches track metadata, replies with a formatted card, and then fires an
analytics log to Supabase (best-effort, non-blocking).
"""

from __future__ import annotations

import logging
from typing import Optional

from aiogram import F, Router, types
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart

from bot.keyboards import track_keyboard
from bot.parser import extract_track_id, fetch_track_info
from bot.supabase_client import log_request

logger = logging.getLogger(__name__)

router = Router(name="main")

# ---------------------------------------------------------------------------
# /start — welcome message
# ---------------------------------------------------------------------------

@router.message(CommandStart())
async def handle_start(message: types.Message) -> None:
    await message.answer(
        "👋 <b>Upsound Bot</b>\n\n"
        "Send me a <b>Yandex Music</b> track link and I'll show you its details.\n\n"
        "Supported formats:\n"
        "• <code>https://music.yandex.ru/album/&lt;id&gt;/track/&lt;id&gt;</code>\n"
        "• <code>https://music.yandex.ru/track/&lt;id&gt;</code>",
        parse_mode=ParseMode.HTML,
    )


# ---------------------------------------------------------------------------
# Yandex Music track link handler
# ---------------------------------------------------------------------------

@router.message(F.text.contains("music.yandex"))
async def handle_yandex_link(message: types.Message) -> None:
    """Detect a Yandex Music URL, fetch metadata, and reply with a track card."""
    text: str = message.text or ""

    # ------------------------------------------------------------------
    # Block 1: fetch track metadata and reply to user
    # ------------------------------------------------------------------
    track_id: Optional[str] = None
    info: Optional[dict] = None
    user = message.from_user

    try:
        # Step 1 — extract track ID from URL
        track_id = extract_track_id(text)
        if not track_id:
            await message.reply(
                "⚠️ Could not parse the track URL.\n"
                "Make sure you're sending a full Yandex Music track link."
            )
            return

        # Step 2 — fetch metadata from Yandex Music
        info = await fetch_track_info(track_id)
        if not info:
            await message.reply(
                "⚠️ Track not found on Yandex Music.\n"
                "The track may be unavailable in your region, or the link is broken."
            )
            return

        # Step 3 — build and send the response card
        reply_text = (
            f"🎵 <b>{info['title']}</b>\n"
            f"🎤 {info['artist']}\n"
            f"⏱ {info['duration']}"
        )

        await message.reply(
            reply_text,
            parse_mode=ParseMode.HTML,
            reply_markup=track_keyboard(text),
        )

        logger.info(
            "Served track_id=%s to user_id=%s (@%s)",
            track_id,
            user.id if user else "unknown",
            user.username if user else "unknown",
        )

    except Exception as exc:  # noqa: BLE001
        logger.error("Error handling Yandex link: %s", exc, exc_info=True)
        await message.reply("⚠️ Ошибка при поиске трека")
        return  # skip analytics if the main flow failed

    # ------------------------------------------------------------------
    # Block 2: log analytics to Supabase (isolated — never affects user)
    # ------------------------------------------------------------------
    try:
        await log_request(
            user_id=user.id if user else 0,
            username=user.username if user else None,
            track_url=text,
            track_info=info,
        )
    except Exception as exc:  # noqa: BLE001
        # Should never reach here — log_request has its own internal guard.
        logger.error("Unexpected error in log_request: %s", exc, exc_info=True)


# ---------------------------------------------------------------------------
# Fallback — any other message
# ---------------------------------------------------------------------------

@router.message()
async def handle_unknown(message: types.Message) -> None:
    await message.reply(
        "Please send me a Yandex Music track link. "
        "Use /start to see supported formats."
    )
