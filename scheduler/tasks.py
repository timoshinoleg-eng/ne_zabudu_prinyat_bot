"""
Фоновые задачи для APScheduler
"""
import asyncio
from datetime import datetime
from aiogram import Bot
from aiogram.types import BufferedInputFile

from database.db import db
from ai.drug_advisor import generate_morning_motivation
from ai.image_gen import generate_motivational_image

async def send_morning_motivation(bot: Bot):
    """Утренняя мотивация с AI"""
    users = db.get_all_users()
    
    for user in users:
        if not user.get("onboarding_completed"):
            continue
        
        # Генерируем промпт
        prompt = f"""Create a motivational morning health card.

Elements:
- Greeting: "Доброе утро, {user['name']}! ☀️"
- Streak: {user.get('streak', 0)} days
- Medicine emoji 💊
- Bright, uplifting colors
- Minimalist design

Style: Professional health app, encouraging"""
        
        try:
            # Генерируем картинку
            image_bytes = await generate_motivational_image(prompt)
            
            # Генерируем текст
            text = await generate_morning_text(user)
            
            # Отправляем
            await bot.send_photo(
                chat_id=user["user_id"],
                photo=BufferedInputFile(image_bytes, "motivation.png"),
                caption=text
            )
            
            await asyncio.sleep(2)  # Rate limiting
        except Exception as e:
            logger.error(f"Error sending morning motivation to {user['user_id']}: {e}")

def setup_scheduler(scheduler, bot: Bot):
    """Настройка всех задач"""
    # Утренняя мотивация в 8:00
    scheduler.add_job(
        send_morning_motivation,
        'cron',
        hour=8,
        minute=0,
        args=[bot],
        id='morning_motivation'
    )
    
    # Напоминания каждую минуту
    scheduler.add_job(
        send_reminders,
        'interval',
        minutes=1,
        args=[bot],
        id='reminders'
    )
