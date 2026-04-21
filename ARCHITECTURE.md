# ARCHITECTURE.md — Upsound Telegram Bot

> Music label bot for track recognition via Yandex Music links.  
> Stack: Python 3.9+ · aiogram v3 · yandex-music · Supabase · Vercel

---

## 1. Architecture Overview

The system operates as a serverless webhook pipeline with four core components:

```
┌──────────┐       ┌───────────────┐       ┌────────────────┐       ┌───────────┐
│ Telegram │──────▶│ Vercel        │──────▶│ Yandex Music   │       │ Supabase  │
│ Bot API  │◀──────│ Serverless Fn │       │ Public API     │       │ PostgreSQL│
└──────────┘       └───────┬───────┘       └────────────────┘       └─────▲─────┘
                           │                                              │
                           └──────────────────────────────────────────────┘
                                        async logging
```

**Data flow:**

1. A user sends a Yandex Music track URL to the Telegram bot.
2. Telegram forwards the update to the Vercel webhook endpoint via HTTPS POST.
3. The serverless function extracts the track ID from the URL using regex.
4. The function queries the Yandex Music public API (anonymous, no auth) to retrieve track metadata.
5. A formatted response (title, artist, duration) is returned to the user in Telegram.
6. The request is asynchronously logged to the Supabase `requests` table for analytics.

**Key constraints:**

- Vercel functions have a **10 s** execution timeout on the Hobby plan.
- Yandex Music public API does not require authentication for basic track metadata.
- All Supabase writes are fire-and-forget to avoid blocking the response.

---

## 2. Database Schema

### Table: `requests`

| Column       | Type                     | Constraints                          | Description                              |
|:-------------|:-------------------------|:-------------------------------------|:-----------------------------------------|
| `id`         | `uuid`                   | `PRIMARY KEY`, `DEFAULT gen_random_uuid()` | Unique request identifier                |
| `created_at` | `timestamptz`            | `DEFAULT now()`, `NOT NULL`          | Timestamp of the request                 |
| `user_id`    | `bigint`                 | `NOT NULL`                           | Telegram user ID                         |
| `username`   | `text`                   | `NULLABLE`                           | Telegram username (may be absent)        |
| `track_url`  | `text`                   | `NOT NULL`                           | Original Yandex Music URL sent by user   |
| `track_info` | `jsonb`                  | `NULLABLE`                           | Parsed track metadata (title, artist, duration) |

### SQL migration

```sql
CREATE TABLE IF NOT EXISTS requests (
    id          uuid           PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at  timestamptz    NOT NULL    DEFAULT now(),
    user_id     bigint         NOT NULL,
    username    text,
    track_url   text           NOT NULL,
    track_info  jsonb
);

CREATE INDEX idx_requests_user_id    ON requests (user_id);
CREATE INDEX idx_requests_created_at ON requests (created_at DESC);
```

---

## 3. Step-by-Step Implementation Plan

### Step 1 — Environment Setup

#### 3.1.1 Project structure

```
upsound-bot/
├── api/
│   └── webhook.py            # Vercel serverless entry point
├── bot/
│   ├── __init__.py
│   ├── handlers.py           # aiogram message handlers
│   ├── keyboards.py          # inline keyboard builders
│   ├── parser.py             # URL parsing & Yandex Music client
│   └── supabase_client.py    # Supabase async logger
├── .env                      # local secrets (not committed)
├── .env.example              # template for collaborators
├── requirements.txt
├── vercel.json
└── README.md
```

#### 3.1.2 `requirements.txt`

```
aiogram>=3.0,<4.0
yandex-music>=2.0.0
supabase>=2.0.0
python-dotenv>=1.0.0
```

#### 3.1.3 `.env`

```env
BOT_TOKEN=123456:ABC-DEF...
SUPABASE_URL=https://<project-ref>.supabase.co
SUPABASE_KEY=eyJhbGciOiJIUzI1NiIs...
```

> **Note:** `SUPABASE_KEY` must be the **anon** (public) key, not the service role key, unless row-level security is disabled for the `requests` table.

---

### Step 2 — Parsing Logic

#### 3.2.1 URL parsing (`bot/parser.py`)

Extract the numeric track ID from all known Yandex Music URL formats:

```python
import re

YANDEX_TRACK_PATTERN = re.compile(
    r"music\.yandex\.(?:ru|com)/album/\d+/track/(\d+)"
    r"|"
    r"music\.yandex\.(?:ru|com)/track/(\d+)"
)


def extract_track_id(url: str) -> str | None:
    """Return the track ID from a Yandex Music URL, or None if invalid."""
    match = YANDEX_TRACK_PATTERN.search(url)
    if not match:
        return None
    return match.group(1) or match.group(2)
```

#### 3.2.2 Yandex Music interaction

```python
from yandex_music import ClientAsync


async def fetch_track_info(track_id: str) -> dict | None:
    """Fetch track metadata via anonymous public API."""
    client = ClientAsync()
    await client.init()

    tracks = await client.tracks(track_id)
    if not tracks:
        return None

    track = tracks[0]
    duration_sec = (track.duration_ms or 0) // 1000
    minutes, seconds = divmod(duration_sec, 60)

    return {
        "title": track.title,
        "artist": ", ".join(a.name for a in (track.artists or [])),
        "duration": f"{minutes:02d}:{seconds:02d}",
    }
```

**Duration conversion rule:** `duration_ms` → integer division by 1000 → `divmod(sec, 60)` → format as `MM:SS`.

---

### Step 3 — Telegram Bot Logic

#### 3.3.1 Router setup (`bot/handlers.py`)

```python
from aiogram import Router, types, F
from aiogram.enums import ParseMode
from bot.parser import extract_track_id, fetch_track_info
from bot.keyboards import track_keyboard
from bot.supabase_client import log_request

router = Router()


@router.message(F.text.regexp(r"music\.yandex\.(ru|com)"))
async def handle_yandex_link(message: types.Message) -> None:
    track_id = extract_track_id(message.text)
    if not track_id:
        await message.reply("⚠️ Could not parse the track URL.")
        return

    info = await fetch_track_info(track_id)
    if not info:
        await message.reply("⚠️ Track not found on Yandex Music.")
        return

    text = (
        f"🎵 <b>{info['title']}</b>\n"
        f"🎤 {info['artist']}\n"
        f"⏱ {info['duration']}"
    )

    await message.reply(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=track_keyboard(message.text),
    )

    # fire-and-forget analytics
    await log_request(
        user_id=message.from_user.id,
        username=message.from_user.username,
        track_url=message.text,
        track_info=info,
    )
```

#### 3.3.2 Inline keyboard (`bot/keyboards.py`)

```python
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton


def track_keyboard(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔗 Open in Yandex Music", url=url)],
        ]
    )
```

---

### Step 4 — Supabase Integration

#### 3.4.1 Async logger (`bot/supabase_client.py`)

```python
import os
import logging
from supabase import create_client, Client

logger = logging.getLogger(__name__)

_client: Client | None = None


def _get_client() -> Client:
    global _client
    if _client is None:
        _client = create_client(
            os.environ["SUPABASE_URL"],
            os.environ["SUPABASE_KEY"],
        )
    return _client


async def log_request(
    user_id: int,
    username: str | None,
    track_url: str,
    track_info: dict,
) -> None:
    """Insert a row into the requests table. Non-blocking best-effort."""
    try:
        _get_client().table("requests").insert({
            "user_id": user_id,
            "username": username,
            "track_url": track_url,
            "track_info": track_info,
        }).execute()
    except Exception as exc:
        logger.warning("Supabase log failed: %s", exc)
```

> All writes are wrapped in a try/except to guarantee the bot response is never blocked by a database failure.

---

### Step 5 — Vercel Preparation

#### 3.5.1 Webhook entry point (`api/webhook.py`)

```python
import os
import json
import logging
from http.server import BaseHTTPRequestHandler

from aiogram import Bot, Dispatcher
from aiogram.types import Update
from dotenv import load_dotenv

from bot.handlers import router

load_dotenv()
logging.basicConfig(level=logging.INFO)

bot = Bot(token=os.environ["BOT_TOKEN"])
dp = Dispatcher()
dp.include_router(router)


class handler(BaseHTTPRequestHandler):
    """Vercel serverless handler for Telegram webhook updates."""

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)
        update = Update.model_validate(json.loads(body))

        import asyncio
        asyncio.get_event_loop().run_until_complete(
            dp.feed_update(bot=bot, update=update)
        )

        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")
```

#### 3.5.2 `vercel.json`

```json
{
  "version": 2,
  "builds": [
    {
      "src": "api/webhook.py",
      "use": "@vercel/python",
      "config": {
        "maxLambdaSize": "15mb",
        "runtime": "python3.9"
      }
    }
  ],
  "routes": [
    {
      "src": "/api/webhook",
      "dest": "/api/webhook.py",
      "methods": ["POST"]
    }
  ]
}
```

#### 3.5.3 Setting the webhook

After deploying to Vercel, register the webhook with Telegram:

```bash
curl -X POST "https://api.telegram.org/bot<BOT_TOKEN>/setWebhook" \
     -d "url=https://<your-project>.vercel.app/api/webhook"
```

---

## 4. Readiness Checklist

| #  | Item                                              | Status |
|:---|:--------------------------------------------------|:------:|
| 1  | `requirements.txt` lists all dependencies         |   ☐    |
| 2  | `.env` contains `BOT_TOKEN`, `SUPABASE_URL`, `SUPABASE_KEY` |   ☐    |
| 3  | Regex correctly extracts track ID from both URL formats |   ☐    |
| 4  | `fetch_track_info` returns `title`, `artist`, `duration` (MM:SS) |   ☐    |
| 5  | aiogram router handles Yandex Music links          |   ☐    |
| 6  | Bot response includes formatted text + inline button |   ☐    |
| 7  | Supabase `requests` table created with correct schema |   ☐    |
| 8  | `log_request` writes to Supabase without blocking response |   ☐    |
| 9  | `api/webhook.py` correctly parses Telegram updates |   ☐    |
| 10 | `vercel.json` routes POST `/api/webhook` to handler |   ☐    |
| 11 | Webhook registered with Telegram Bot API            |   ☐    |
| 12 | End-to-end test: URL → bot reply + Supabase row     |   ☐    |

---

*Document generated for the Upsound project. Last updated: 2026-04-21.*
