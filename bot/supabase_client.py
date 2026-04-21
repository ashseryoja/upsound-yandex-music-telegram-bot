"""
bot/supabase_client.py
~~~~~~~~~~~~~~~~~~~~~~
Thin async wrapper around supabase-py for fire-and-forget analytics logging.

The client is created once (module-level singleton) and reused across
invocations within the same Vercel function instance.

All writes are best-effort: any exception is caught and logged so that a
Supabase outage can never crash or delay the bot response.
"""

from __future__ import annotations

import asyncio
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
    """Return (or create) the shared Supabase client."""
    global _client  # noqa: PLW0603
    if _client is None:
        url = os.environ.get("SUPABASE_URL", "")
        key = os.environ.get("SUPABASE_KEY", "")

        if not url or not key:
            raise RuntimeError(
                "SUPABASE_URL and SUPABASE_KEY must be set in the environment."
            )

        _client = create_client(url, key)
    return _client


async def log_request(
    user_id: int,
    username: Optional[str],
    track_url: str,
    track_info: dict,
) -> None:
    """Insert one analytics row into the ``requests`` table.

    Runs the synchronous supabase-py call in a thread pool so it does not
    block the event loop.  The whole operation is wrapped in a try/except so
    that any failure (network, schema mismatch, bad credentials, …) is only
    logged — never re-raised.
    """
    try:
        client = _get_client()

        payload = {
            "user_id": user_id,
            "username": username,
            "track_url": track_url,
            "track_info": track_info,
        }

        # supabase-py execute() is synchronous — offload to a thread pool
        # so the event loop stays unblocked.
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: client.table("requests").insert(payload).execute(),
        )

    except RuntimeError as exc:
        # Missing env vars — log once and give up
        logger.error("Supabase client not configured: %s", exc)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Supabase log_request failed (non-fatal): %s", exc)
