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

# ─── Логирование ───
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ══════════════════════════════════════
# ⚠️ ВСТАВЬ СВОИ ТОКЕНЫ СЮДА ⚠️
# ══════════════════════════════════════
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
# ══════════════════════════════════════

client = OpenAI(api_key=OPENAI_API_KEY)

USER_PROMPTS = {}
ACTIVE_REQUESTS = {}


# ── /start ──
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome = (
        "🎬 *Привет! Я генерирую видео по описанию!*\n\n"
        "📝 Отправь мне текст — описание видео\n"
        "⏱ Максимум: 60 секунд\n\n"
        "💡 *Пример:*\n"
        "_A cat astronaut floating in space, "
        "Earth visible through the helmet, "
        "cinematic lighting, 4K_\n\n"
        "🇬🇧 Пиши на английском для лучшего качества!\n\n"
        "/start — начало\n"
        "/help — советы"
    )
    await update.message.reply_text(welcome, parse_mode="Markdown")


# ── /help ──
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🎯 *Как написать хороший промпт:*\n\n"
        "1️⃣ *Кто/что в кадре:*\n"
        "   _A young woman, a robot, a mountain_\n\n"
        "2️⃣ *Что происходит:*\n"
        "   _walking through, flying over, dancing_\n\n"
        "3️⃣ *Где:*\n"
        "   _in a forest, on Mars, underwater_\n\n"
        "4️⃣ *Стиль:*\n"
        "   _cinematic, anime, photorealistic, 4K_\n\n"
        "5️⃣ *Камера:*\n"
        "   _slow motion, drone shot, close-up_\n\n"
        "📌 *Полный пример:*\n"
        "_A majestic eagle soaring over mountains "
        "during golden hour, cinematic drone shot, "
        "slow motion, photorealistic, 4K_"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


# ── Получение текста → выбор длительности ──
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    prompt = update.message.text.strip()

    if len(prompt) > 1500:
        await update.message.reply_text(
            "❌ Слишком длинный текст. Максимум 1500 символов."
        )
        return

    if user_id in ACTIVE_REQUESTS:
        await update.message.reply_text(
            "⏳ Подожди, предыдущее видео ещё генерируется!"
        )
        return

    USER_PROMPTS[user_id] = prompt

    keyboard = [
        [
            InlineKeyboardButton("5 сек ⚡", callback_data="dur_5"),
            InlineKeyboardButton("10 сек", callback_data="dur_10"),
        ],
        [
            InlineKeyboardButton("20 сек", callback_data="dur_20"),
            InlineKeyboardButton("30 сек", callback_data="dur_30"),
        ],
        [
            InlineKeyboardButton("60 сек 🎬", callback_data="dur_60"),
        ],
    ]

    await update.message.reply_text(
        f"📝 *Промпт:*\n_{prompt[:200]}"
        f"{'...' if len(prompt) > 200 else ''}_\n\n"
        "⏱ Выбери длительность:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
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
            "❌ Промпт не найден. Отправь описание заново."
        )
        return

    if user_id in ACTIVE_REQUESTS:
        return

    ACTIVE_REQUESTS[user_id] = True

    await query.edit_message_text(
        f"🎬 Генерирую видео ({duration} сек)...\n"
        f"⏱ Ожидание: ~{max(duration * 3, 30)}-"
        f"{max(duration * 6, 120)} сек\n\n"
        f"Промпт: _{prompt[:150]}_",
        parse_mode="Markdown",
    )

    try:
        # Генерация через Sora
        video_url = await asyncio.to_thread(
            generate_video_sora, prompt, duration
        )

        if not video_url:
            await query.edit_message_text(
                "❌ Ошибка генерации. Попробуй другой промпт."
            )
            return

        await query.edit_message_text("📥 Скачиваю видео...")
        video_data = req.get(video_url, timeout=300)

        if video_data.status_code != 200:
            await query.edit_message_text("❌ Ошибка скачивания.")
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
                    f"🎬 Видео ({duration} сек)\n"
                    f"📝 _{prompt[:200]}_"
                ),
                parse_mode="Markdown",
                supports_streaming=True,
            )

        await query.delete_message()
        os.unlink(tmp_path)

    except Exception as e:
        logger.error(f"Ошибка: {e}")
        await query.edit_message_text(
            f"❌ Ошибка:\n`{str(e)[:300]}`",
            parse_mode="Markdown",
        )
    finally:
        ACTIVE_REQUESTS.pop(user_id, None)
        USER_PROMPTS.pop(user_id, None)


def generate_video_sora(prompt: str, duration: int) -> str:
    """Генерация видео через OpenAI Sora"""
    response = client.images.generate(
        model="sora",
        prompt=prompt,
        n=1,
        size="1080x1920",
        response_format="url",
        extra_body={
            "duration": duration,
        },
    )
    return response.data[0].url


# ── Запуск ──
def main():
    print("🤖 Бот запускается...")
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(
        CallbackQueryHandler(handle_duration, pattern=r"^dur_")
    )
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text)
    )

    print("✅ Бот работает! Для остановки нажми Ctrl+C")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
