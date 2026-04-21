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

# ─── Логирование ───
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── Токены ───
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
PROXY_BASE = "https://api.proxyapi.ru/openai/v1"

# ─── Состояние ───
ACTIVE_REQUESTS = {}

# ══════════════════════════════════════
#  Проверка доступных моделей
# ══════════════════════════════════════
def check_available_models():
    """Проверяет какие модели доступны на ProxyAPI"""
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
    try:
        resp = req.get(f"{PROXY_BASE}/models", headers=headers, timeout=30)
        logger.info(f"📋 Доступные модели: {resp.text[:2000]}")
    except Exception as e:
        logger.error(f"❌ Ошибка проверки моделей: {e}")

# ══════════════════════════════════════
#  Команда /start
# ══════════════════════════════════════
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome = (
        "🎬 Привет! Я бот для создания видео!\n"
        "📝 Отправь мне описание видео и я создам его для тебя.\n"
        "📌 Пример: Красивый закат над океаном, волны, кинематографичная съёмка\n"
        "⏱ Генерация: 2-5 минут\n"
        "💰 ~27 руб за видео\n"
        "/start - Меню\n"
        "/help - Помощь"
    )
    await update.message.reply_text(welcome)

# ══════════════════════════════════════
#  Команда /help
# ══════════════════════════════════════
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🆘 Помощь\n"
        "1. Отправь описание видео\n"
        "2. Подожди 2-5 минут\n"
        "3. Получи видео!\n"
        "\n"
        "Советы:\n"
        "- Описывай подробно\n"
        "- Укажи стиль\n"
        "- Опиши камеру и свет"
    )
    await update.message.reply_text(text)

# ══════════════════════════════════════
#  Обработка текста
# ══════════════════════════════════════
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    prompt = update.message.text.strip()
    
    if len(prompt) > 1500:
        await update.message.reply_text("❌ Максимум 1500 символов.")
        return
    
    if user_id in ACTIVE_REQUESTS:
        await update.message.reply_text("⏳ Подожди! Предыдущее видео ещё создаётся.")
        return
    
    ACTIVE_REQUESTS[user_id] = True
    status_msg = await update.message.reply_text(
        "🎬 Создаю видео...\n⏳ Подожди 2-5 минут!"
    )
    
    try:
        video_url = await asyncio.to_thread(generate_video, prompt)
        if not video_url:
            await status_msg.edit_text("❌ Не удалось создать видео.")
            return
        
        await status_msg.edit_text("📥 Скачиваю видео...")
        video_data = req.get(video_url, timeout=300)
        if video_data.status_code != 200:
            await status_msg.edit_text("❌ Ошибка скачивания.")
            return
        
        await status_msg.edit_text("📤 Отправляю...")
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
            tmp.write(video_data.content)
            tmp_path = tmp.name
        
        with open(tmp_path, "rb") as vf:
            await update.message.reply_video(
                video=vf,
                caption=f"✅ Видео готово!\n📝 {prompt[:200]}",
                supports_streaming=True,
            )
        await status_msg.delete()
        os.unlink(tmp_path)
        
    except Exception as e:
        logger.error(f"Error: {e}")
        await status_msg.edit_text(f"❌ Ошибка:\n{str(e)[:500]}")
    finally:
        ACTIVE_REQUESTS.pop(user_id, None)

# ══════════════════════════════════════
#  Генерация видео
# ══════════════════════════════════════
def generate_video(prompt):
    """Генерация видео через ProxyAPI Sora"""
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    
    # Пробуем разные варианты модели
    models_to_try = ["sora-2", "sora"]
    
    for model in models_to_try:
        try:
            logger.info(f"🚀 Пробую модель: {model}")
            
            # Отправляем задачу на генерацию
            payload = {
                "model": model,
                "prompt": prompt,
                "size": "720x1280",
                "response_format": "url",
            }
            
            logger.info(f"📤 Запрос: {json.dumps(payload, ensure_ascii=False)[:300]}")
            
            resp = req.post(
                f"{PROXY_BASE}/images/generations",
                json=payload,
                headers=headers,
                timeout=60,
            )
            
            logger.info(f"📥 Ответ ({resp.status_code}): {resp.text[:1000]}")
            
            if resp.status_code == 200:
                data = resp.json()
                video_url = extract_url(data)
                if video_url:
                    logger.info(f"✅ Видео готово: {video_url[:100]}")
                    return video_url
                
                # Если асинхронная задача
                task_id = data.get("id") or data.get("task_id")
                if task_id:
                    logger.info(f"⏳ Задача создана. ID: {task_id}. Ожидаю...")
                    return poll_for_result(task_id, headers)
            
            elif resp.status_code == 400:
                error_data = resp.json()
                if "Model not supported" in str(error_data):
                    logger.warning(f"❌ Модель {model} не поддерживается, пробую следующую...")
                    continue
                else:
                    raise Exception(f"API ошибка 400: {error_data}")
            else:
                raise Exception(f"API ошибка {resp.status_code}: {resp.text[:500]}")
                
        except Exception as e:
            logger.error(f"Ошибка с моделью {model}: {e}")
            continue
    
    raise Exception("Ни одна из моделей не сработала. Проверьте баланс и доступность API.")

# ══════════════════════════════════════
#  Опрос результата
# ══════════════════════════════════════
def poll_for_result(task_id, headers):
    """Ожидание готовности видео"""
    for attempt in range(90):  # макс 7.5 минут
        time.sleep(5)
        try:
            poll_resp = req.get(
                f"{PROXY_BASE}/images/generations/{task_id}",
                headers=headers,
                timeout=30,
            )
            
            if poll_resp.status_code == 200:
                poll_data = poll_resp.json()
                status = poll_data.get("status", "").lower()
                
                logger.info(f"🔄 Статус задачи ({attempt + 1}/90): {status}")
                
                if status in ("completed", "succeeded", "ready"):
                    video_url = extract_url(poll_data)
                    if video_url:
                        return video_url
                    raise Exception("Задача завершена, но URL не найден")
                
                if status in ("failed", "error"):
                    raise Exception(f"Генерация не удалась: {poll_data}")
            else:
                logger.warning(f"⚠️ Ошибка опроса: {poll_resp.status_code}")
                
        except Exception as e:
            logger.error(f"Ошибка при опросе: {e}")
            continue
    
    raise Exception("⏱ Таймаут: видео не создалось за 7.5 минут")

# ══════════════════════════════════════
#  Извлечение URL
# ══════════════════════════════════════
def extract_url(data):
    """Извлекает URL видео из ответа API"""
    logger.info(f"🔍 Извлекаю URL из: {str(data)[:500]}")
    
    if isinstance(data, str) and data.startswith("http"):
        return data
    
    if isinstance(data, dict):
        # Проверяем data[0]
        if "data" in data and len(data["data"]) > 0:
            item = data["data"][0]
            if isinstance(item, dict):
                for key in ["url", "video", "video_url"]:
                    if key in item and item[key]:
                        return str(item[key])
            elif isinstance(item, str) and item.startswith("http"):
                return item
        
        # Проверяем корневой уровень
        for key in ["url", "video", "video_url", "output"]:
            if key in data and data[key]:
                value = data[key]
                if isinstance(value, str) and value.startswith("http"):
                    return value
    
    return None

# ══════════════════════════════════════
#  Веб-сервер для Render
# ══════════════════════════════════════
web_app = Flask(__name__)

@web_app.route("/")
def home():
    return "✅ Video Bot is running!"

def run_web():
    port = int(os.environ.get("PORT", 10000))
    web_app.run(host="0.0.0.0", port=port)

# ══════════════════════════════════════
#  Запуск бота
# ══════════════════════════════════════
async def run_bot():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    
    await app.initialize()
    await app.start()
    await app.updater.start_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,  # Игнорирует старые сообщения
    )
    logger.info("🤖 Бот запущен и работает!")
    
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()

if __name__ == "__main__":
    print("🚀 Запуск Video Bot...")
    check_available_models()  # Проверяем модели при старте
    Thread(target=run_web, daemon=True).start()
    asyncio.run(run_bot())
