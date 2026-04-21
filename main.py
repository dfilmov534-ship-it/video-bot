import os
import logging
import tempfile
import asyncio
import json
import time
import requests as req
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
from flask import Flask
from threading import Thread

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
PROXY_BASE = "https://api.proxyapi.ru/openai/v1"

USER_PROMPTS = {}
ACTIVE_REQUESTS = {}


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome = (
        "🎬 Привет! Я бот для создания видео!\n\n"
        "📝 Отправь мне описание видео "
        "и я создам его для тебя.\n\n"
        "📌 Пример:\n"
        "Красивый закат над океаном, волны, "
        "кинематографичная съёмка\n\n"
        "⏱ Генерация: 2-5 минут\n"
        "💰 ~27 руб за видео\n\n"
        "/start - Меню\n"
        "/help - Помощь"
    )
    await update.message.reply_text(welcome)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🆘 Помощь\n\n"
        "1. Отправь описание видео\n"
        "2. Подожди 2-5 минут\n"
        "3. Получи видео!\n\n"
        "Советы:\n"
        "- Описывай подробно\n"
        "- Укажи стиль\n"
        "- Опиши камеру и свет"
    )
    await update.message.reply_text(text)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    prompt = update.message.text.strip()

    if len(prompt) > 1500:
        await update.message.reply_text(
            "❌ Максимум 1500 символов."
        )
        return

    if user_id in ACTIVE_REQUESTS:
        await update.message.reply_text(
            "⏳ Подожди! Предыдущее видео ещё создаётся."
        )
        return

    ACTIVE_REQUESTS[user_id] = True

    status_msg = await update.message.reply_text(
        "🎬 Создаю видео...\n"
        "⏳ Подожди 2-5 минут!"
    )

    try:
        video_url = await asyncio.to_thread(
            generate_video, prompt
        )

        if not video_url:
            await status_msg.edit_text(
                "❌ Не удалось создать видео."
            )
            return

        await status_msg.edit_text("📥 Скачиваю видео...")
        video_data = req.get(video_url, timeout=300)

        if video_data.status_code != 200:
            await status_msg.edit_text(
                "❌ Ошибка скачивания."
            )
            return

        await status_msg.edit_text("📤 Отправляю...")

        with tempfile.NamedTemporaryFile(
            suffix=".mp4", delete=False
        ) as tmp:
            tmp.write(video_data.content)
            tmp_path = tmp.name

        with open(tmp_path, "rb") as vf:
            await update.message.reply_video(
                video=vf,
                caption="✅ Видео готово!\n📝 " + prompt[:200],
                supports_streaming=True,
            )

        await status_msg.delete()
        os.unlink(tmp_path)

    except Exception as e:
        logger.error("Error: %s", e)
        await status_msg.edit_text(
            "❌ Ошибка:\n" + str(e)[:500]
        )
    finally:
        ACTIVE_REQUESTS.pop(user_id, None)


def generate_video(prompt):
    """Try multiple request formats to find the right one"""
    headers = {
        "Authorization": "Bearer " + OPENAI_API_KEY,
        "Content-Type": "application/json",
    }

    url = PROXY_BASE + "/images/generations"

    attempts = [
        {
            "model": "sora-2",
            "prompt": prompt,
        },
        {
            "model": "sora-2",
            "prompt": prompt,
            "response_format": "url",
        },
        {
            "model": "sora-2",
            "prompt": prompt,
            "size": "1280x720",
            "response_format": "url",
        },
        {
            "model": "sora-2",
            "prompt": prompt,
            "n": 1,
            "size": "1280x720",
        },
        {
            "model": "sora-2",
            "prompt": prompt,
            "n": 1,
            "size": "1920x1080",
            "response_format": "url",
        },
        {
            "model": "sora-2",
            "prompt": prompt,
            "quality": "standard",
            "size": "1280x720",
            "n": 1,
            "response_format": "url",
        },
    ]

    for i, payload in enumerate(attempts):
        num = str(i + 1)
        logger.info(
            "Attempt " + num + ": "
            + json.dumps(payload, ensure_ascii=False)[:300]
        )
        try:
            resp = req.post(
                url,
                json=payload,
                headers=headers,
                timeout=600,
            )
            logger.info(
                "Attempt " + num
                + " status: " + str(resp.status_code)
                + " response: " + resp.text[:500]
            )

            if resp.status_code == 200:
                data = resp.json()
                video_url = extract_url(data)
                if video_url:
                    logger.info(
                        "SUCCESS with attempt " + num
                    )
                    return video_url
        except Exception as e:
            logger.error(
                "Attempt " + num + " error: " + str(e)
            )
            continue

    raise Exception(
        "All 6 format attempts failed. "
        "Check Render logs for details."
    )


def extract_url(data):
    """Extract video URL from various response formats"""
    logger.info("Extracting URL from: " + str(data)[:500])

    if isinstance(data, str):
        if data.startswith("http"):
            return data

    if isinstance(data, dict):
        if "data" in data and len(data["data"]) > 0:
            item = data["data"][0]
            for key in ["url", "video", "video_url"]:
                if key in item and item[key]:
                    return str(item[key])

        for key in ["url", "video", "video_url", "output"]:
            if key in data and data[key]:
                return str(data[key])

    return None


web_app = Flask(__name__)


@web_app.route("/")
def home():
    return "Bot is running!"


def run_web():
    port = int(os.environ.get("PORT", 10000))
    web_app.run(host="0.0.0.0", port=port)


async def run_bot():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND, handle_text
        )
    )

    await app.initialize()
    await app.start()
    await app.updater.start_polling(
        allowed_updates=Update.ALL_TYPES
    )
    logger.info("Bot is running!")

    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


if __name__ == "__main__":
    print("Bot starting...")
    Thread(target=run_web, daemon=True).start()
    asyncio.run(run_bot())
