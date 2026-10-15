import asyncio
import html
import json
import logging
import os
import re
from pathlib import Path
from urllib.parse import urlparse

import feedparser
from telegram import Bot
from telegram.constants import ParseMode

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHANNEL = os.getenv("TELEGRAM_CHANNEL", "@ternopil_live_news")
CHECK_EVERY_SECONDS = int(os.getenv("CHECK_EVERY_SECONDS", "300"))
MAX_PER_CHECK = int(os.getenv("MAX_PER_CHECK", "5"))
STATE_FILE = Path(os.getenv("STATE_FILE", "seen_items.json"))

# Додавай лише RSS-стрічки, доступні публічно. Перевіряй правила й умови кожного сайту.
FEEDS = [
    ("Новини Тернополя", "https://ternopil.cx.ua/feed"),
    # Приклад додаткової стрічки: ("Назва джерела", "https://example.com/feed"),
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("ternopil-news")


def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"initialized": False, "seen": []}


def save_state(state):
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def clean_text(value):
    value = re.sub(r"<[^>]+>", " ", value or "")
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def item_key(entry):
    return (entry.get("id") or entry.get("guid") or entry.get("link") or entry.get("title", "")).strip()


def format_post(source_name, entry):
    title = clean_text(entry.get("title", "Новина"))
    link = (entry.get("link") or "").strip()
    summary = clean_text(entry.get("summary", entry.get("description", "")))
    if len(summary) > 450:
        summary = summary[:447].rsplit(" ", 1)[0] + "…"

    # Не публікуємо елемент без посилання на оригінал.
    if not link or urlparse(link).scheme not in ("http", "https"):
        return None

    lines = [f"📰 <b>{html.escape(title)}</b>"]
    if summary:
        lines.append(html.escape(summary))
    lines.extend([
        "",
        f"📍 Джерело: {html.escape(source_name)}",
        f'🔗 <a href="{html.escape(link, quote=True)}">Читати оригінал</a>',
    ])
    return "\n".join(lines)


async def fetch_feed(url):
    # feedparser performs the HTTP request; run it in a thread so the bot loop stays responsive.
    return await asyncio.to_thread(feedparser.parse, url)


async def main():
    state = load_state()
    seen = set(state.get("seen", []))
    async with Bot(TOKEN) as bot:
        # On the very first start, mark currently available articles as seen to avoid flooding
        # the channel with old posts. New articles will be posted on later checks.
        if not state.get("initialized"):
            for source_name, url in FEEDS:
                try:
                    feed = await fetch_feed(url)
                    for entry in feed.entries:
                        key = item_key(entry)
                        if key:
                            seen.add(key)
                    log.info("Початкова синхронізація: %s (%d записів)", source_name, len(feed.entries))
                except Exception:
                    log.exception("Не вдалося прочитати стрічку %s", url)
            state = {"initialized": True, "seen": list(seen)[-5000:]}
            save_state(state)
            log.info("Початкову синхронізацію завершено. Очікуємо нові публікації.")

        while True:
            new_items = []
            for source_name, url in FEEDS:
                try:
                    feed = await fetch_feed(url)
                    if getattr(feed, "bozo", False):
                        log.warning("RSS може бути некоректним: %s", url)
                    for entry in feed.entries:
                        key = item_key(entry)
                        if key and key not in seen:
                            new_items.append((source_name, entry, key))
                except Exception:
                    log.exception("Помилка читання джерела %s", url)

            # Oldest first if feed provides published timestamps; otherwise preserve feed order.
            new_items.sort(key=lambda item: item[1].get("published_parsed") or item[1].get("updated_parsed") or (9999, 12, 31, 23, 59, 59, 0, 0, 0))

            for source_name, entry, key in new_items[:MAX_PER_CHECK]:
                message = format_post(source_name, entry)
                try:
                    if message:
                        await bot.send_message(
                            chat_id=CHANNEL,
                            text=message,
                            parse_mode=ParseMode.HTML,
                            disable_web_page_preview=False,
                        )
                        log.info("Опубліковано: %s", clean_text(entry.get("title", "")))
                    seen.add(key)
                except Exception:
                    # Leave failed items unseen so the bot can retry on the next cycle.
                    log.exception("Не вдалося опублікувати: %s", key)

            state = {"initialized": True, "seen": list(seen)[-5000:]}
            save_state(state)
            await asyncio.sleep(CHECK_EVERY_SECONDS)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Бота зупинено.")
