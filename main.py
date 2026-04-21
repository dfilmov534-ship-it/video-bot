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
        "📝 Как пользоваться:\n"
        "Просто отправь мне описание видео, "
        "и я создам его для тебя.\n\n"
        "💡 Советы:\n"
        "- Описывай подробно что должно быть в видео\n"
        "- Укажи стиль (реалистичный, мультфильм)\n"
        "- Опиши освещение, движение камеры\n\n"
        "📌 Пример описания:\n"
        "Красивый закат над океаном, волны бьются о берег, "
        "кинематографичная съёмка, замедленное движение\n\n"
        "⏱ Генерация занимает 2-5 минут\n"
        "💰 Стоимость: ~27 руб за видео\n\n"
        "/start - Главное меню\n"
        "/help - Помощь"
    )
    await update.message.reply_text(welcome)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🆘 Помощь\n\n"
        "1. Отправь описание видео\n"
        "2. Выбери длительность\n"
        "3. Подожди 2-5 минут\n"
        "4. Получи видео!\n\n"
        "🎯 Советы по описанию:\n"
        "- Кто или что в кадре\n"
        "- Что происходит\n"
        "- Где происходит\n"
        "- Стиль и камера"
    )
    await update.message.reply_text(text)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    prompt = update.message.text.strip()

    if len(prompt) > 1500:
        await update.message.reply_text(
            "❌ Слишком длинный текст! Максимум 1500 символов."
        )
        return

    if user_id in ACTIVE_REQUESTS:
        await update.message.reply_text(
            "⏳ Подожди! Предыдущее видео ещё создаётся."
        )
        return

    USER_PROMPTS[user_id] = prompt

    keyboard = [
        [
            InlineKeyboardButton("⚡ 5 сек", callback_data="dur_5"),
            InlineKeyboardButton("10 сек", callback_data="dur_10"),
        ],
        [
            InlineKeyboardButton("20 сек", callback_data="dur_20"),
        ],
    ]

    short = prompt[:200] + ("..." if len(prompt) > 200 else "")
    await update.message.reply_text(
        "📝 Описание:\n" + short + "\n\n⏱ Выбери длительность:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def handle_duration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    duration = int(query.data.split("_")[1])

    prompt = USER_PROMPTS.get(user_id)
    if not prompt:
        await query.edit_message_text(
            "❌ Описание не найдено. Отправь заново."
        )
        return

    if user_id in ACTIVE_REQUESTS:
        return

    ACTIVE_REQUESTS[user_id] = True

    await query.edit_message_text(
        "🎬 Создаю видео (" + str(duration) + " сек)...\n"
        "⏳ Подожди 2-5 минут!\n"
        "Я отправлю когда будет готово."
    )

    try:
        video_url = await asyncio.to_thread(
            generate_video, prompt, duration
        )

        if not video_url:
            await query.edit_message_text(
                "❌ Не удалось создать видео."
            )
            return

        await query.edit_message_text("📥 Скачиваю видео...")
        video_data = req.get(video_url, timeout=300)

        if video_data.status_code != 200:
            await query.edit_message_text(
                "❌ Ошибка скачивания."
            )
            return

        await query.edit_message_text("📤 Отправляю...")

        with tempfile.NamedTemporaryFile(
            suffix=".mp4", delete=False
        ) as tmp:
            tmp.write(video_data.content)
            tmp_path = tmp.name

        with open(tmp_path, "rb") as vf:
            await context.bot.send_video(
                chat_id=query.message.chat_id,
                video=vf,
                caption=(
                    "✅ Видео готово! ("
                    + str(duration)
                    + " сек)\n📝 "
                    + prompt[:200]
                ),
                supports_streaming=True,
            )

        await query.delete_message()
        os.unlink(tmp_path)

    except Exception as e:
        logger.error("Error: %s", e)
        await query.edit_message_text(
            "❌ Ошибка:\n" + str(e)[:500]
        )
    finally:
        ACTIVE_REQUESTS.pop(user_id, None)
        USER_PROMPTS.pop(user_id, None)


def generate_video(prompt, duration):
    """Generate video via ProxyAPI using direct HTTP"""
    headers = {
        "Authorization": "Bearer " + OPENAI_API_KEY,
        "Content-Type": "application/json",
    }

    payload = {
        "model": "sora-2",
        "prompt": prompt,
        "n": 1,
        "size": "720x1280",
        "duration": duration,
    }

    logger.info("Sending request to ProxyAPI...")
    logger.info("Payload: %s", json.dumps(payload))

    # Try images endpoint
    resp = req.post(
        PROXY_BASE + "/images/generations",
        json=payload,
        headers=headers,
        timeout=600,
    )

    logger.info("Response status: %s", resp.status_code)
    logger.info("Response: %s", resp.text[:1000])

    # If images endpoint fails, try videos endpoint
    if resp.status_code != 200:
        logger.info("Trying /videos/generations endpoint...")
        payload2 = {
            "model": "sora-2",
            "prompt": prompt,
            "size": "720x1280",
            "duration": duration,
        }
        resp = req.post(
            PROXY_BASE + "/videos/generations",
            json=payload2,
            headers=headers,
            timeout=600,
        )
        logger.info("Videos endpoint status: %s", resp.status_code)
        logger.info("Videos response: %s", resp.text[:1000])

    if resp.status_code != 200:
        raise Exception(
            "API error " + str(resp.status_code)
            + ": " + resp.text[:300]
        )

    data = resp.json()

    # Try different response formats
    if "data" in data and len(data["data"]) > 0:
        item = data["data"][0]
        if "url" in item:
            return item["url"]
        if "video" in item:
            return item["video"]
        if "b64_json" in item:
            return item["b64_json"]

    # If response has direct url
    if "url" in data:
        return data["url"]
    if "video" in data:
        return data["video"]
    if "output" in data:
        return data["output"]

    # If async task - poll for result
    if "id" in data:
        return poll_for_result(data["id"], headers)

    raise Exception("Unknown response format: " + resp.text[:300])


def poll_for_result(task_id, headers):
    """Poll for async task completion"""
    logger.info("Polling for task: %s", task_id)
    for i in range(120):
        time.sleep(5)
        resp = req.get(
            PROXY_BASE + "/videos/generations/" + str(task_id),
            headers=headers,
            timeout=30,
        )
        if resp.status_code == 200:
            data = resp.json()
            status = data.get("status", "")
            if status in ("completed", "succeeded", "complete"):
                if "data" in data and len(data["data"]) > 0:
                    return data["data"][0].get("url", "")
                return data.get("url", data.get("output", ""))
            if status in ("failed", "error"):
                raise Exception("Generation failed: " + resp.text[:200])
        logger.info("Poll %d: status=%s", i, resp.status_code)
    raise Exception("Timeout: generation took too long")


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
        CallbackQueryHandler(handle_duration, pattern=r"^dur_")
    )
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
