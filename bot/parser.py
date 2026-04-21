"""
bot/parser.py
~~~~~~~~~~~~~
URL parsing and Yandex Music track metadata fetching.

Uses **direct HTTP requests** (aiohttp) to the Yandex Music web-player
handler endpoint (``/handlers/track.jsx``) which returns rich JSON
including title, artists, and duration — without authentication.

This endpoint works reliably from any IP (including Vercel serverless)
because it is the same endpoint the web frontend uses for rendering
track pages.

A fallback to the ``api.music.yandex.net/tracks`` internal API is
provided in case the handler endpoint becomes unavailable.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# URL regex — extracts the numeric track ID from any Yandex Music URL
# ---------------------------------------------------------------------------
YANDEX_TRACK_PATTERN = re.compile(r"track/(\d+)")

# ---------------------------------------------------------------------------
# HTTP configuration
# ---------------------------------------------------------------------------
_HANDLER_URL = "https://music.yandex.ru/handlers/track.jsx"

_API_URL = "https://api.music.yandex.net/tracks"

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 14; Pixel 7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Mobile Safari/537.36"
    ),
    "Accept": "application/json, text/javascript, */*",
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Referer": "https://music.yandex.ru/",
    "X-Retpath-Y": "https://music.yandex.ru/",
}

_API_HEADERS = {
    "User-Agent": "YandexMusicAndroid/2024.09.2",
    "Accept": "application/json",
    "Accept-Language": "ru",
    "X-Yandex-Music-Client": "YandexMusicAndroid/2024.09.2",
}

_TIMEOUT = aiohttp.ClientTimeout(total=8)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_track_id(url: str) -> Optional[str]:
    """Return the numeric track ID from a Yandex Music URL.

    Works with any URL shape (album/track, direct /track, mobile share links
    with query parameters and UTM tags).  Returns ``None`` when no track ID
    can be found.
    """
    match = YANDEX_TRACK_PATTERN.search(url)
    return match.group(1) if match else None


async def fetch_track_info(track_id: str) -> Optional[dict]:
    """Fetch track metadata.

    Returns ``{"title": str, "artist": str, "duration": "MM:SS"}`` on
    success, or ``None`` when every retrieval strategy fails.
    """
    # Strategy 1 — handlers/track.jsx (web-player endpoint, most reliable)
    info = await _fetch_via_handler(track_id)
    if info:
        return info

    logger.info(
        "Handler endpoint failed for track_id=%s, trying internal API",
        track_id,
    )

    # Strategy 2 — api.music.yandex.net/tracks (mobile API fallback)
    return await _fetch_via_api(track_id)


# ---------------------------------------------------------------------------
# Strategy 1: Web-player handler endpoint (primary)
# ---------------------------------------------------------------------------

async def _fetch_via_handler(track_id: str) -> Optional[dict]:
    """GET /handlers/track.jsx — the endpoint the web frontend uses.

    Returns full JSON with the track object nested under the ``"track"`` key.
    Works without authentication from any IP.
    """
    params = {
        "track": track_id,
        "lang": "ru",
        "external-domain": "music.yandex.ru",
        "overembed": "false",
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                _HANDLER_URL,
                params=params,
                headers=_BROWSER_HEADERS,
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status != 200:
                    logger.warning(
                        "Handler returned HTTP %d for track_id=%s",
                        resp.status,
                        track_id,
                    )
                    return None

                data = await resp.json(content_type=None)

                # The track object lives under the "track" key
                track = data.get("track")
                if not track:
                    logger.warning(
                        "Handler response has no 'track' key for track_id=%s",
                        track_id,
                    )
                    return None

                return _parse_track(track)

    except (json.JSONDecodeError, aiohttp.ContentTypeError) as exc:
        logger.warning("Handler JSON decode error for track_id=%s: %s", track_id, exc)
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("Handler request failed for track_id=%s: %s", track_id, exc)
        return None


# ---------------------------------------------------------------------------
# Strategy 2: Internal mobile API (fallback)
# ---------------------------------------------------------------------------

async def _fetch_via_api(track_id: str) -> Optional[dict]:
    """POST to the Yandex Music internal API (same endpoint the mobile app uses)."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                _API_URL,
                data={"track-ids": track_id},
                headers=_API_HEADERS,
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status != 200:
                    logger.warning(
                        "API returned HTTP %d for track_id=%s",
                        resp.status,
                        track_id,
                    )
                    return None

                data = await resp.json()
                result = data.get("result", [])
                if not result:
                    return None

                return _parse_track(result[0])

    except Exception as exc:  # noqa: BLE001
        logger.warning("API request failed for track_id=%s: %s", track_id, exc)
        return None


# ---------------------------------------------------------------------------
# Shared track parser
# ---------------------------------------------------------------------------

def _parse_track(track: dict) -> dict:
    """Extract title, artist, and duration from a raw track JSON object.

    Works with both handler and API response shapes — both use the same
    field names (``title``, ``artists``, ``durationMs``).
    """
    # Duration: ms → MM:SS
    duration_ms: int = track.get("durationMs", 0)
    duration_sec = duration_ms // 1000
    minutes, seconds = divmod(duration_sec, 60)

    # Artists — may be a list of objects with a "name" field
    artists_raw = track.get("artists", [])
    artists = ", ".join(
        a.get("name", "") for a in artists_raw if a.get("name")
    )

    return {
        "title": track.get("title") or "Unknown title",
        "artist": artists or "Unknown artist",
        "duration": f"{minutes:02d}:{seconds:02d}",
    }
