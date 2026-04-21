import os
import logging
import tempfile
import asyncio
import requests as req
from openai import OpenAI
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

client = OpenAI(
    api_key=OPENAI_API_KEY,
    base_url="https://api.proxyapi.ru/openai/v1"
)

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
        "- Стиль и камера\n\n"
        "📌 Пример:\n"
        "Величественный орёл парит над горами "
        "на закате, съёмка с дрона, замедленное движение"
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
            InlineKeyboardButton("30 сек", callback_data="dur_30"),
        ],
        [
            InlineKeyboardButton("🎬 60 сек", callback_data="dur_60"),
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
        "⏳ Подожди, я отправлю когда будет готово!\n"
        "Обычно это занимает 2-5 минут."
    )

    try:
        video_url = await asyncio.to_thread(
            generate_video_sora, prompt, duration
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
            "❌ Ошибка:\n" + str(e)[:300]
        )
    finally:
        ACTIVE_REQUESTS.pop(user_id, None)
        USER_PROMPTS.pop(user_id, None)


def generate_video_sora(prompt, duration):
    """Generate video using OpenAI Sora-2 via ProxyAPI"""
    response = client.images.generate(
        model="sora-2",
        prompt=prompt,
        n=1,
        size="1080x1920",
        response_format="url",
        extra_body={"duration": duration},
    )
    return response.data[0].url


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
