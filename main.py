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


def check_models():
    """Проверить доступные модели"""
    headers = {
        "Authorization": "Bearer " + OPENAI_API_KEY,
    }
    try:
        resp = req.get(
            PROXY_BASE + "/models",
            headers=headers,
            timeout=30,
        )
        logger.info("Available models: " + resp.text[:3000])
    except Exception as e:
        logger.error("Models check error: " + str(e))


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
    """Генерация видео через Sora API"""
    headers = {
        "Authorization": "Bearer " + OPENAI_API_KEY,
        "Content-Type": "application/json",
    }

    # Эндпоинт для видео, НЕ для картинок
    url = PROXY_BASE + "/videos/generations"

    payload = {
        "model": "sora",
        "prompt": prompt,
        "size": "1920x1080",
        "n": 1,
    }

    logger.info(
        "Sending video request: "
        + json.dumps(payload, ensure_ascii=False)[:300]
    )

    resp = req.post(
        url,
        json=payload,
        headers=headers,
        timeout=60,
    )

    logger.info(
        "Response status: " + str(resp.status_code)
        + " body: " + resp.text[:1000]
    )

    if resp.status_code not in (200, 201):
        raise Exception(
            "API error " + str(resp.status_code)
            + ": " + resp.text[:500]
        )

    data = resp.json()

    # Если сразу вернулся URL
    video_url = extract_url(data)
    if video_url:
        return video_url

    # Если вернулся ID задачи — поллим результат
    generation_id = data.get("id")
    if not generation_id:
        raise Exception(
            "No video URL and no generation ID in response: "
            + str(data)[:500]
        )

    logger.info(
        "Got generation ID: " + generation_id
        + ", polling for result..."
    )

    poll_url = PROXY_BASE + "/videos/generations/" + generation_id

    for attempt in range(60):  # макс ~5 минут
        time.sleep(5)

        poll_resp = req.get(
            poll_url,
            headers=headers,
            timeout=30,
        )

        logger.info(
            "Poll attempt " + str(attempt + 1)
            + " status: " + str(poll_resp.status_code)
            + " body: " + poll_resp.text[:500]
        )

        if poll_resp.status_code != 200:
            continue

        poll_data = poll_resp.json()
        status = poll_data.get("status", "")

        if status == "failed":
            raise Exception(
                "Video generation failed: "
                + str(poll_data)[:500]
            )

        if status == "completed":
            video_url = extract_url(poll_data)
            if video_url:
                return video_url
            raise Exception(
                "Completed but no URL: "
                + str(poll_data)[:500]
            )

    raise Exception(
        "Timeout: video not ready after 5 minutes"
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
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,  # игнорировать старые сообщения
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
    check_models()  # проверяем доступные модели
    Thread(target=run_web, daemon=True).start()
    asyncio.run(run_bot())
