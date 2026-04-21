"""
bot/supabase_client.py
~~~~~~~~~~~~~~~~~~~~~~
Analytics logging to Supabase for every track request.

Design for Vercel serverless:
- supabase-py is fully SYNCHRONOUS. We call it directly — no executor,
  no asyncio bridging.
- In our serverless setup, the entire async graph runs under asyncio.run()
  which blocks the Vercel worker thread until all coroutines complete,
  so calling a synchronous function inside an async function is safe and
  correct here (the event loop is not shared with any other work).
- The Supabase client is created lazily on first call (module-level
  singleton) so it survives warm container reuse.
- Every insert is wrapped in its own try/except with explicit structured
  logging so failures are always visible in Vercel logs.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

from supabase import Client, create_client

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Singleton client — initialised lazily on first use
# ---------------------------------------------------------------------------
_client: Optional[Client] = None


def _get_client() -> Client:
    """Return (or create) the shared Supabase client.

    Raises ``RuntimeError`` if the required env vars are absent.
    """
    global _client  # noqa: PLW0603
    if _client is None:
        url = os.environ.get("SUPABASE_URL", "").strip()
        key = os.environ.get("SUPABASE_KEY", "").strip()

        if not url or not key:
            raise RuntimeError(
                "SUPABASE_URL and SUPABASE_KEY must be set in the environment."
            )

        _client = create_client(url, key)
        logger.info("Supabase client initialised (url=%s...)", url[:30])

    return _client


async def log_request(
    user_id: int,
    username: Optional[str],
    track_url: str,
    track_info: dict,
) -> None:
    """Insert one analytics row into the ``requests`` table.

    Payload matches the schema exactly:
      - user_id   (int8)   — Telegram user ID
      - username  (text)   — nullable Telegram @handle
      - track_url (text)   — the raw URL the user sent
      - track_info (jsonb) — {"title": ..., "artist": ..., "duration": ...}

    The call is synchronous (supabase-py) and runs directly inside the
    coroutine — safe because we execute under asyncio.run() on a dedicated
    Vercel worker thread, so there is no shared long-running event loop to
    block.
    """
    try:
        client = _get_client()
    except RuntimeError as exc:
        logger.error("Supabase not configured, skipping log_request: %s", exc)
        return

    payload = {
        "user_id": int(user_id),          # ensure int, not numpy/etc
        "username": username or None,
        "track_url": track_url,
        "track_info": track_info,         # dict → jsonb via supabase-py
    }

    logger.info(
        "Supabase insert starting: user_id=%s track_url=%s",
        user_id,
        track_url,
    )

    try:
        response = (
            client.table("requests")
            .insert(payload)
            .execute()
        )
        # supabase-py v2 returns a PostgrestAPIResponse with .data
        rows = getattr(response, "data", None)
        if rows:
            logger.info(
                "Supabase insert OK: inserted row id=%s",
                rows[0].get("id", "?"),
            )
        else:
            # Successful HTTP call but no rows returned — log the full response
            logger.warning(
                "Supabase insert returned no data. Full response: %s",
                json.dumps(
                    {"data": rows, "count": getattr(response, "count", None)},
                    default=str,
                ),
            )

    except Exception as exc:  # noqa: BLE001
        logger.error(
            "Supabase insert FAILED for user_id=%s track_url=%s: %s",
            user_id,
            track_url,
            exc,
            exc_info=True,
        )
