"""
bot/keyboards.py
~~~~~~~~~~~~~~~~
Inline keyboard builders for bot responses.
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def track_keyboard(url: str) -> InlineKeyboardMarkup:
    """Return an inline keyboard with a single "Open in Yandex Music" button."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔗 Open in Yandex Music",
                    url=url,
                )
            ],
        ]
    )
