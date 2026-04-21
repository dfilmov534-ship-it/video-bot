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

# ─── Исправление для Python 3.10+ ───
try:
    loop = asyncio.get_event_loop()
except RuntimeError:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

# ─── Логирование ───
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── Токены ───
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

client = OpenAI(api_key=OPENAI_API_KEY)

USER_PROMPTS = {}
ACTIVE_REQUESTS = {}


# ── /start ──
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome = (
        "🎬 Привет! Я бот для создания видео!\n\n"
        "📝 Как пользоваться:\n"
        "Просто отправь мне описание видео, "
        "и я создам его для тебя.\n\n"
        "💡 Советы:\n"
        "• Описывай подробно что должно быть в видео\n"
        "• Укажи стиль (реалистичный, мультфильм, кинематографичный)\n"
        "• Опиши освещение, движение камеры\n\n"
        "📌 Пример описания:\n"
        "Красивый закат над океаном, волны бьются о берег, "
        "кинематографичная съёмка, замедленное движение, 4K\n\n"
        "⏱ Генерация занимает 2-5 минут\n\n"
        "📋 Команды:\n"
        "/start — Главное меню\n"
        "/help — Помощь и советы"
    )
    await update.message.reply_text(welcome)


# ── /help ──
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🆘 Помощь по использованию бота\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        "📖 Как создать видео:\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        "1. Отправь текстовое описание видео\n"
        "2. Выбери длительность (5-60 секунд)\n"
        "3. Подожди 2-5 минут\n"
        "4. Получи готовое видео!\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🎯 Как написать хорошее описание:\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "1️⃣ Кто или что в кадре:\n"
        "   Девушка, собака, космический корабль\n\n"
        "2️⃣ Что происходит:\n"
        "   Идёт по улице, летит над горами\n\n"
        "3️⃣ Где это происходит:\n"
        "   В лесу, на Марсе, под водой\n\n"
        "4️⃣ Стиль видео:\n"
        "   Реалистичный, аниме, кинематографичный\n\n"
        "5️⃣ Камера и эффекты:\n"
        "   Замедленная съёмка, вид с дрона, крупный план"
    )
    await update.message.reply_text(text)


# ── Получение текста ──
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    prompt = update.message.text.strip()

    if len(prompt) > 1500:
        await update.message.reply_text(
            "❌ Слишком длинный текст!\n"
            f"Ты написал {len(prompt)} символов.\n"
            "Максимум: 1500 символов."
        )
        return

    if user_id in ACTIVE_REQUESTS:
        await update.message.reply_text(
            "⏳ Подожди!\n"
            "Твоё предыдущее видео ещё создаётся."
        )
        return

    USER_PROMPTS[user_id] = prompt

    keyboard = [
        [
            InlineKeyboardButton("⚡ 5 секунд", callback_data="dur_5"),
            InlineKeyboardButton("10 секунд", callback_data="dur_10"),
        ],
        [
            InlineKeyboardButton("20 секунд", callback_data="dur_20"),
            InlineKeyboardButton("30 секунд", callback_data="dur_30"),
        ],
        [
            InlineKeyboardButton("🎬 60 секунд", callback_data="dur_60"),
        ],
    ]

    short_prompt = prompt[:200] + ("..." if len(prompt) > 200 else "")

    await update.message.reply_text(
        f"📝 Твоё описание:\n{short_prompt}\n\n"
        "⏱ Выбери длительность видео:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# ── Генерация после выбора длительности ──
async def handle_duration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    duration = int(query.data.split("_")[1])

    prompt = USER_PROMPTS.get(user_id)
    if not prompt:
        await query.edit_message_text(
            "❌ Описание не найдено.\n"
            "Отправь описание видео заново."
        )
        return

    if user_id in ACTIVE_REQUESTS:
        return

    ACTIVE_REQUESTS[user_id] = True

    await query.edit_message_text(
        f"🎬 Создаю видео ({duration} сек)...\n\n"
        "⏳ Пожалуйста, подожди. Я отправлю видео "
        "когда оно будет готово!"
    )

    try:
        video_url = await asyncio.to_thread(
            generate_video_sora, prompt, duration
        )

        if not video_url:
            await query.edit_message_text(
                "❌ Не удалось создать видео.\n"
                "Попробуй изменить описание."
            )
            return

        await query.edit_message_text("📥 Скачиваю готовое видео...")
        video_data = req.get(video_url, timeout=300)

        if video_data.status_code != 200:
            await query.edit_message_text("❌ Ошибка при скачивании видео.")
            return

        await query.edit_message_text("📤 Отправляю видео...")

        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
            tmp.write(video_data.content)
            tmp_path = tmp.name

        with open(tmp_path, "rb") as vf:
            await context.bot.send_video(
                chat_id=query.message.chat_id,
                video=vf,
                caption=(
                    f"✅ Видео готово! ({duration} сек)\n\n"
                    f"📝 Описание:\n{prompt[:200]}"
                ),
                supports_streaming=True,
            )

        await query.delete_message()
        os.unlink(tmp_path)

    except Exception as e:
        logger.error(f"Ошибка: {e}")
        await query.edit_message_text(
            f"❌ Произошла ошибка:\n{str(e)[:300]}\n\n"
            "Попробуй позже или измени описание."
        )
    finally:
        ACTIVE_REQUESTS.pop(user_id, None)
        USER_PROMPTS.pop(user_id, None)


def generate_video_sora(prompt: str, duration: int) -> str:
    response = client.images.generate(
        model="sora",
        prompt=prompt,
        n=1,
        size="1080x1920",
        response_format="url",
        extra_body={"duration": duration},
    )
    return response.data[0].url


# ── Мини веб-сервер для Render ──
web_app = Flask(__name__)

@web_app.route("/")
def home():
    return "Бот работает!"

def run_web():
    port = int(os.environ.get("PORT", 10000))
    web_app.run(host="0.0.0.0", port=port)


# ── Запуск ──
if __name__ == "__main__":
    print("Бот запускается...")
    Thread(target=run_web, daemon=True).start()

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CallbackQueryHandler(handle_duration, pattern=r"^dur_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print("Бот работает!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)
