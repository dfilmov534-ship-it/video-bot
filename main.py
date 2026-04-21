import os
import logging
import tempfile
import asyncio
import json
import time
import requests as req
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from flask import Flask
from threading import Thread

# ─── Настройка логирования ───
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── Переменные окружения ───
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
API_KEY = os.environ.get("OPENAI_API_KEY")
PROXY_BASE = "https://api.proxyapi.ru/openai/v1"

# ─── Состояние ───
ACTIVE_REQUESTS = set()

# ═══════════════════════════════════════
#  ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ═══════════════════════════════════════
def check_available_models():
    """Выводит доступные модели в логи для отладки"""
    headers = {"Authorization": f"Bearer {API_KEY}"}
    try:
        resp = req.get(f"{PROXY_BASE}/models", headers=headers, timeout=15)
        logger.info("🔍 Доступные модели: %s", resp.text[:1000])
    except Exception as e:
        logger.error("❌ Ошибка получения моделей: %s", e)

def generate_video(prompt: str) -> str:
    """Генерация видео через ProxyAPI (асинхронный процесс)"""
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }
    
    # 1. Отправляем задачу
    payload = {
        "model": "sora-2",
        "prompt": prompt,
        "response_format": "url"
    }
    logger.info("🚀 Отправляю задачу: %s", json.dumps(payload, ensure_ascii=False))
    
    resp = req.post(f"{PROXY_BASE}/images/generations", json=payload, headers=headers, timeout=30)
    logger.info("📥 Ответ API (статус %d): %s", resp.status_code, resp.text[:500])
    
    if resp.status_code != 200:
        raise Exception(f"API вернул ошибку {resp.status_code}: {resp.text[:300]}")
        
    data = resp.json()
    
    # Если сразу вернулся URL
    if isinstance(data, dict) and "data" in data:
        url = data["data"][0].get("url") or data["data"][0].get("video_url")
        if url:
            logger.info("✅ Видео готово сразу!")
            return url

    # Если вернулся ID задачи (асинхронно)
    task_id = data.get("id") or data.get("task_id")
    if not task_id:
        raise Exception(f"Не получил ID задачи или URL. Ответ: {data}")
        
    logger.info("⏳ Задача создана. ID: %s. Ожидаю готовности...", task_id)
    
    # 2. Опрос статуса (polling)
    for attempt in range(90):  # максимум 7.5 минут (5 сек * 90)
        time.sleep(5)
        poll = req.get(f"{PROXY_BASE}/images/generations/{task_id}", headers=headers, timeout=15)
        if poll.status_code != 200:
            continue
            
        p_data = poll.json()
        status = p_data.get("status", "").lower()
        logger.info("🔄 Статус задачи: %s", status)
        
        if status in ("completed", "succeeded", "ready"):
            url = p_data.get("url") or p_data.get("video_url")
            if "data" in p_data and len(p_data["data"]) > 0:
                url = p_data["data"][0].get("url") or p_data["data"][0].get("video_url")
            if url:
                return url
            raise Exception("Задача завершена, но URL не найден в ответе.")
            
        if status in ("failed", "error"):
            raise Exception(f"Генерация упала: {p_data.get('error', 'unknown')}")
            
    raise Exception("⏱ Таймаут: видео не создалось за отведённое время.")

# ═══════════════════════════════════════
#  ОБРАБОТЧИКИ TELEGRAM
# ═══════════════════════════════════════
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🎬 Привет! Я создаю видео по текстовому описанию.\n"
        "📝 Просто отправь мне промпт, и я сгенерирую ролик.\n"
        "⏱ Ожидание: 2-5 минут\n"
        "💡 Пример: *Закат над океаном, волны бьются о скалы, кинематографичная съёмка, замедленная съёмка, 4K*"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def handle_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    prompt = update.message.text.strip()
    
    if len(prompt) > 1000:
        await update.message.reply_text("❌ Слишком длинное описание. Максимум 1000 символов.")
        return
    if user_id in ACTIVE_REQUESTS:
        await update.message.reply_text("⏳ Подожди! Предыдущее видео ещё генерируется.")
        return
        
    ACTIVE_REQUESTS.add(user_id)
    status_msg = await update.message.reply_text("🎬 Начинаю генерацию... Это займёт 2-5 минут. ⏳")
    
    try:
        video_url = await asyncio.to_thread(generate_video, prompt)
        await status_msg.edit_text("📥 Скачиваю готовое видео...")
        
        resp = req.get(video_url, timeout=300)
        resp.raise_for_status()
        
        await status_msg.edit_text("📤 Отправляю...")
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
            tmp.write(resp.content)
            tmp.flush()
            with open(tmp.name, "rb") as f:
                await update.message.reply_video(
                    video=f,
                    caption=f"✅ Видео готово!\n📝 *{prompt[:150]}*",
                    parse_mode="Markdown",
                    supports_streaming=True
                )
        await status_msg.delete()
        os.unlink(tmp.name)
    except Exception as e:
        logger.error("Ошибка генерации: %s", e)
        await status_msg.edit_text(f"❌ Ошибка: {str(e)[:300]}")
    finally:
        ACTIVE_REQUESTS.discard(user_id)

# ═══════════════════════════════════════
#  ЗАПУСК (Flask + Telegram Bot)
# ═══════════════════════════════════════
web_app = Flask(__name__)
@web_app.route("/")
def home():
    return "✅ Video Bot is running!"

def run_web():
    port = int(os.environ.get("PORT", 10000))
    web_app.run(host="0.0.0.0", port=port)

async def run_bot():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_prompt))
    
    await app.initialize()
    await app.start()
    await app.updater.start_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True  # ⚡ Игнорирует старые сообщения и решает Conflict
    )
    logger.info("🤖 Бот запущен и ожидает сообщения...")
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()

if __name__ == "__main__":
    logger.info("🚀 Запуск Video Bot...")
    check_available_models()  # Проверит модели при старте
    Thread(target=run_web, daemon=True).start()
    asyncio.run(run_bot())
    
