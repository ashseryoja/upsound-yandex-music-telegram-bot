"""
bot/parser.py
~~~~~~~~~~~~~
URL parsing and Yandex Music track metadata fetching.

Uses **direct HTTP requests** (aiohttp) instead of the ``yandex-music``
library to avoid session / event-loop issues in serverless environments
and to bypass geo-restrictions that affect the library's ``init()`` call
from non-Russian IPs (e.g. Vercel).

Two retrieval strategies are tried in order:
1. **Yandex Music internal API** (``POST /tracks``) — fast, returns full
   metadata including duration.
2. **Page scraping** (OG meta-tags from the track page) — fallback when
   the API is unreachable or geo-blocked.  Duration may not be available.
"""

from __future__ import annotations

import html
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
_API_URL = "https://api.music.yandex.net/tracks"

_API_HEADERS = {
    "User-Agent": "YandexMusicAndroid/2024.09.2",
    "Accept": "application/json",
    "Accept-Language": "ru",
    "X-Yandex-Music-Client": "YandexMusicAndroid/2024.09.2",
}

_PAGE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 14; Pixel 7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Mobile Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "ru-RU,ru;q=0.9",
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
    # Strategy 1 — Yandex Music internal API (fast, full metadata)
    info = await _fetch_via_api(track_id)
    if info:
        return info

    logger.info(
        "API unavailable for track_id=%s, falling back to page scraping",
        track_id,
    )

    # Strategy 2 — scrape OG tags from the track page (slower, no duration)
    return await _fetch_via_page(track_id)


# ---------------------------------------------------------------------------
# Strategy 1: Direct API request
# ---------------------------------------------------------------------------

async def _fetch_via_api(track_id: str) -> Optional[dict]:
    """POST to the Yandex Music internal API to fetch track metadata.

    This is the same endpoint the mobile app uses.  It works without
    authentication for basic track info.
    """
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
                        "Yandex API returned HTTP %d for track_id=%s",
                        resp.status,
                        track_id,
                    )
                    return None

                data = await resp.json()
                result = data.get("result", [])
                if not result:
                    return None

                return _parse_api_track(result[0])

    except Exception as exc:  # noqa: BLE001
        logger.warning("Yandex API request failed for track_id=%s: %s", track_id, exc)
        return None


def _parse_api_track(track: dict) -> dict:
    """Extract title, artist, and duration from a raw API track object."""
    duration_ms: int = track.get("durationMs", 0)
    duration_sec = duration_ms // 1000
    minutes, seconds = divmod(duration_sec, 60)

    artists = ", ".join(
        a.get("name", "")
        for a in track.get("artists", [])
        if a.get("name")
    )

    return {
        "title": track.get("title") or "Unknown title",
        "artist": artists or "Unknown artist",
        "duration": f"{minutes:02d}:{seconds:02d}",
    }


# ---------------------------------------------------------------------------
# Strategy 2: Page scraping (OG meta tags)
# ---------------------------------------------------------------------------

async def _fetch_via_page(track_id: str) -> Optional[dict]:
    """GET the track page and extract metadata from OG meta tags."""
    url = f"https://music.yandex.ru/track/{track_id}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                headers=_PAGE_HEADERS,
                timeout=_TIMEOUT,
                allow_redirects=True,
            ) as resp:
                if resp.status != 200:
                    logger.warning(
                        "Track page returned HTTP %d for %s", resp.status, url
                    )
                    return None

                page = await resp.text()
                return _parse_page(page)

    except Exception as exc:  # noqa: BLE001
        logger.warning("Page scraping failed for track_id=%s: %s", track_id, exc)
        return None


def _parse_page(page: str) -> Optional[dict]:
    """Extract track info from OG meta tags or the ``<title>`` element."""
    og = _extract_og_tags(page)

    title = og.get("title")
    artist = og.get("description")

    # Fallback: parse <title> — usually "Song — Artist слушать ..."
    if not title:
        m = re.search(r"<title>(.+?)</title>", page, re.I | re.S)
        if m:
            raw = html.unescape(m.group(1).strip())
            parts = raw.split("—", 1)
            title = parts[0].strip()
            if len(parts) >= 2:
                # Remove "слушать онлайн ..." suffix
                artist = re.split(r"\s+слушать", parts[1], maxsplit=1)[0].strip()

    if not title:
        return None

    return {
        "title": html.unescape(title),
        "artist": html.unescape(artist) if artist else "Unknown artist",
        "duration": "—",  # not available from page HTML
    }


def _extract_og_tags(page: str) -> dict[str, str]:
    """Return a dict of ``{property: content}`` for all ``og:*`` meta tags."""
    tags: dict[str, str] = {}

    # <meta property="og:title" content="...">  (property first)
    for prop, content in re.findall(
        r'<meta[^>]+property="og:([^"]+)"[^>]+content="([^"]*)"', page, re.I
    ):
        tags[prop] = content

    # <meta content="..." property="og:title">  (content first)
    for content, prop in re.findall(
        r'<meta[^>]+content="([^"]*)"[^>]+property="og:([^"]+)"', page, re.I
    ):
        tags.setdefault(prop, content)

    return tags
