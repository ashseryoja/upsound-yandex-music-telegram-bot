"""
bot/parser.py
~~~~~~~~~~~~~
URL parsing and Yandex Music track metadata fetching.

Supports two canonical Yandex Music URL formats:
  - https://music.yandex.ru/album/<album_id>/track/<track_id>
  - https://music.yandex.ru/track/<track_id>   (direct link)
Both .ru and .com TLDs are accepted.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from yandex_music import ClientAsync
from yandex_music.exceptions import YandexMusicError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Regex — extracts the numeric track ID from any Yandex Music URL
# ---------------------------------------------------------------------------
YANDEX_TRACK_PATTERN = re.compile(r"track/(\d+)")


def extract_track_id(url: str) -> Optional[str]:
    """Return the numeric track ID from a Yandex Music URL.

    Works with any URL shape (album/track, direct /track, mobile share links
    with query parameters and UTM tags).  Returns ``None`` when no track ID
    can be found.
    """
    match = YANDEX_TRACK_PATTERN.search(url)
    if not match:
        return None
    return match.group(1)


async def fetch_track_info(track_id: str) -> Optional[dict]:
    """Fetch track metadata via the Yandex Music public (anonymous) API.

    Returns a dict ``{"title": str, "artist": str, "duration": "MM:SS"}`` on
    success, or ``None`` when the track cannot be found or the request fails.
    """
    try:
        # Anonymous client — no token required for public track metadata
        client = ClientAsync()
        await client.init()

        tracks = await client.tracks([track_id])
        if not tracks:
            logger.warning("Yandex Music returned empty list for track_id=%s", track_id)
            return None

        track = tracks[0]

        # Guard against tracks that come back without full metadata
        if track is None:
            return None

        # Duration conversion: ms → MM:SS
        duration_ms: int = track.duration_ms or 0
        duration_sec = duration_ms // 1000
        minutes, seconds = divmod(duration_sec, 60)
        duration_str = f"{minutes:02d}:{seconds:02d}"

        # Collect artist names (a track may have multiple artists)
        artists = ", ".join(a.name for a in (track.artists or []) if a.name)

        return {
            "title": track.title or "Unknown title",
            "artist": artists or "Unknown artist",
            "duration": duration_str,
        }

    except YandexMusicError as exc:
        logger.error("Yandex Music API error for track_id=%s: %s", track_id, exc)
        return None
    except Exception as exc:  # noqa: BLE001
        logger.error("Unexpected error fetching track_id=%s: %s", track_id, exc)
        return None
